"""Build the cp2077 SQLite dataset from an installed game.

Inputs (all read directly from the game install):
  * r6/cache/tweakdb.bin + tweakdb_ep1.bin (TweakDB blobs)
  * archive/pc/content/lang_<lang>_text.archive + archive/pc/ep1/lang_<lang>_text.archive
    (onscreens LocKey table + subtitle files)
  * archive/pc/{content,ep1} gamedata archives: base\\journal\\cooked_journal.journal
    + ep1\\journal\\cooked_journal.journal (shards, codex, emails, ...)
  * WolvenKit hash tables (usedhashes.kark, tweakdbstr.kark) vendored under
    data/ for path and TweakDBID resolution

Output: one SQLite file with typed tables, curated views, and FTS5 full-text
search over all readable text. The output is deterministic: identical inputs
produce a byte-identical file: no timestamps, stable row ordering, a fixed
page size, and no ANALYZE statistics.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from .cr2w import read_cr2w
from .engine import Archive, ArchiveError, fnv1a64, kark_decompress
from .tweakdb import parse as parse_tweakdb, tweakdbid_hash, murmur3

DATA_DIR = Path(__file__).resolve().parent / "data"

#: Game language codes -> internal archive folder (folder name uses -).
GAME_LANGS = ["ar", "cs", "de", "en", "es-es", "es-mx", "fr", "hu", "it", "ja",
              "ko", "pl", "pt", "ru", "th", "tr", "ua", "zh-cn", "zh-tw"]

SOURCES = (("base", "content"), ("ep1", "ep1"))


class BuildError(RuntimeError):
    """Raised when the game install does not contain the required inputs."""


def read_vlq(buf: bytes, pos: int) -> tuple[int, int]:
    b = buf[pos]
    pos += 1
    neg = b & 0x80
    val = b & 0x3F
    if b & 0x40:
        shift = 6
        while True:
            b = buf[pos]
            pos += 1
            val |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                break
    return (-val if neg else val), pos


def load_tweak_names() -> dict[int, str]:
    """tweakdbstr.kark -> {tweakdbid_hash(name): name}."""
    blob = kark_decompress((DATA_DIR / "tweakdbstr.kark").read_bytes())
    id2name: dict[int, str] = {}
    pos = 20
    while pos < len(blob):
        n, pos = read_vlq(blob, pos)
        if n > 0:
            s = blob[pos : pos + n * 2].decode("utf-16-le")
            pos += n * 2
        elif n < 0:
            s = blob[pos : pos - n].decode("utf-8")
            pos += -n
        else:
            s = ""
        if s:
            id2name.setdefault(tweakdbid_hash(s), s)
    return id2name


def load_record_types() -> dict[int, str]:
    """murmur3(typeName) -> typeName, from the vendored record class list."""
    types: dict[int, str] = {}
    for line in (DATA_DIR / "record_types.txt").read_text().splitlines():
        name = line.strip()
        if name:
            types[murmur3(name.encode("ascii"), 0x5EEDBA5E)] = name
    return types


def load_used_hashes() -> dict[int, str]:
    """usedhashes.kark -> {fnv1a64(path): path} for archive entry resolution."""
    blob = kark_decompress((DATA_DIR / "usedhashes.kark").read_bytes())
    return {
        fnv1a64(line.encode("utf-8")): line
        for line in blob.decode("utf-8", errors="replace").splitlines()
    }


class LocResolver:
    """LocKey -> text across base + ep1 onscreens for one language."""

    def __init__(self, lang: str, game_dir: Path, used_hashes: dict[int, str]):
        self.lang = lang
        self.entries: dict[int, dict] = {}
        self._load(game_dir, used_hashes)

    def _load(self, game_dir: Path, used_hashes: dict[int, str]) -> None:
        pc = game_dir / "archive" / "pc"
        for prefix, folder in SOURCES:
            arch = pc / folder / f"lang_{self.lang}_text.archive"
            if not arch.exists():
                raise BuildError(
                    f"missing language archive {arch}\n"
                    f"available languages: {' '.join(GAME_LANGS)}"
                )
            ar = Archive(arch)
            # The language folder is discovered from the archive contents:
            # the entry whose path ends with onscreens\\onscreens_final.json
            # under this prefix (folder names are e.g. en-us).
            path = next(
                (
                    p
                    for h in ar.files
                    for p in (used_hashes.get(h) or "",)
                    if p.startswith(prefix + "\\localization\\")
                    and p.endswith("\\onscreens\\onscreens_final.json")
                ),
                None,
            )
            if path is None:
                raise BuildError(f"no onscreens found in {arch.name}")
            f = read_cr2w(ar.read_entry(fnv1a64(path.encode("utf-8"))))
            if f.root is None:
                raise BuildError(f"could not decode {path} in {arch.name}")
            inner = f.root.get("root")
            if not isinstance(inner, dict):
                raise BuildError(f"unexpected structure in {path} in {arch.name}")
            for e in inner.get("entries") or []:
                pk = e.get("primaryKey")
                if isinstance(pk, int):
                    self.entries[pk] = e

    def text(self, lockey: int | None) -> str:
        if lockey is None:
            return ""
        e = self.entries.get(lockey)
        if not e:
            return ""
        return (e.get("femaleVariant") or e.get("maleVariant") or "").replace(
            "\\n", "\n"
        )


def locstr_key(v: object) -> int | None:
    """LocalizationString {'$locstr':True,'value':'LocKey#N'} -> N."""
    if isinstance(v, dict) and v.get("$locstr"):
        val = v.get("value")
        if isinstance(val, str) and val.startswith("LocKey#"):
            return int(val[7:])
    return None


FULL_HASH_LIMIT = 64 << 20
EDGE_HASH_SIZE = 4 << 20


def file_identity(path: Path) -> bytes:
    """SHA-256 of a file, or of its first and last 4 MiB when it is huge.

    The multi-gigabyte archives are identified by their edges plus size: enough
    to notice a patch or a mod swap without re-reading 20 GB on every build.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        f.seek(0)
        if size <= FULL_HASH_LIMIT:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        else:
            h.update(f.read(EDGE_HASH_SIZE))
            f.seek(size - EDGE_HASH_SIZE)
            h.update(f.read(EDGE_HASH_SIZE))
    return h.digest()


class DatasetBuilder:
    def __init__(self, game_dir: Path, db_path: Path, lang: str = "en"):
        self.game_dir = Path(game_dir)
        self.db_path = Path(db_path)
        if lang not in GAME_LANGS:
            raise BuildError(
                f"unknown language {lang!r}; one of: {' '.join(GAME_LANGS)}"
            )
        self.lang = lang
        self._require_inputs()
        self.used_hashes = load_used_hashes()
        self.tweak_names = load_tweak_names()
        self.record_types = load_record_types()
        self.loc = LocResolver(lang, self.game_dir, self.used_hashes)

    # ------------------------------------------------------------------ build

    def input_files(self) -> list[Path]:
        """Every file this build reads, for the meta fingerprint."""
        pc = self.game_dir / "archive" / "pc"
        files = [
            self.game_dir / "r6" / "cache" / "tweakdb.bin",
            self.game_dir / "r6" / "cache" / "tweakdb_ep1.bin",
        ]
        for prefix, folder in SOURCES:
            files.append(pc / folder / f"lang_{self.lang}_text.archive")
        files.append(pc / "content" / "basegame_4_gamedata.archive")
        files.append(pc / "ep1" / "ep1_2_gamedata.archive")
        return files

    def _require_inputs(self) -> None:
        """Fail before any work if the install lacks a file the build reads."""
        missing = [p for p in self.input_files() if not p.is_file()]
        if missing:
            listed = "\n  ".join(str(p) for p in missing)
            raise BuildError(
                f"{self.game_dir} is missing required game files:\n  {listed}\n"
                "expected a standard Steam/GOG Cyberpunk 2077 install "
                f"(language {self.lang!r})"
            )

    def _fingerprint(self) -> str:
        """SHA-256 over the content identity of every input file."""
        h = hashlib.sha256()
        h.update(f"cpdb-1\nlang={self.lang}\n".encode())
        for p in self.input_files():
            h.update(f"{p.name}:{p.stat().st_size}:".encode())
            h.update(file_identity(p))
            h.update(b"\n")
        return h.hexdigest()

    def build(self) -> None:
        t0 = time.time()
        out = self.db_path
        tmp = out.with_suffix(out.suffix + ".tmp")
        if tmp.exists():
            tmp.unlink()
        con = sqlite3.connect(tmp)
        try:
            # Deterministic output: fixed page size (must be set before any
            # table is created), no run-dependent state.
            con.execute("PRAGMA page_size = 4096")
            con.executescript(SCHEMA)
            self._build_meta(con)
            self._build_strings(con)
            self._build_tweakdb(con)
            self._build_journal(con)
            self._build_subtitles(con)
            self._build_fts(con)
            con.commit()
            con.execute("VACUUM")
            con.close()
            os.replace(tmp, out)
        except Exception:
            con.close()
            if tmp.exists():
                tmp.unlink()
            raise
        print(f"dataset built in {time.time()-t0:.1f}s -> {out}")

    def _build_meta(self, con: sqlite3.Connection) -> None:
        for key, value in (("lang", self.lang),
                           ("input_fingerprint", self._fingerprint())):
            con.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)", (key, value)
            )

    # --------------------------------------------------------------- strings

    def _build_strings(self, con: sqlite3.Connection) -> None:
        """lockeys table: every LocKey with secondaryKey and resolved text."""
        rows = []
        for pk, e in sorted(self.loc.entries.items()):
            rows.append(
                (
                    str(pk),
                    e.get("secondaryKey") or "",
                    (e.get("femaleVariant") or "").replace("\\n", "\n"),
                    (e.get("maleVariant") or "").replace("\\n", "\n"),
                )
            )
        con.executemany(
            "INSERT INTO lockeys (loc_key, secondary_key, female_variant, male_variant)"
            " VALUES (?, ?, ?, ?)",
            rows,
        )
        print(f"lockeys: {len(rows)}")

    # --------------------------------------------------------------- tweakdb

    def _build_tweakdb(self, con: sqlite3.Connection) -> None:
        blobs = [
            ("base", self.game_dir / "r6" / "cache" / "tweakdb.bin"),
            ("ep1", self.game_dir / "r6" / "cache" / "tweakdb_ep1.bin"),
        ]
        for source, path in blobs:
            if not path.exists():
                raise BuildError(f"missing TweakDB blob {path}")
            db = parse_tweakdb(path.read_bytes())
            flats = []
            flat_texts = []
            for fid in sorted(db.flats):
                val = db.flats[fid]
                name = self.tweak_names.get(fid)
                if name is None:
                    continue
                red_type = db.flat_type_by_id.get(fid, "")
                flats.append((source, fid, name, red_type, flat_value_json(val)))
                if red_type == "gamedataLocKeyWrapper" and isinstance(val, int):
                    flat_texts.append((source, fid, name, str(val),
                                       self.loc.text(val)))
                elif (red_type == "String" and isinstance(val, str)
                      and val.startswith("LocKey#")):
                    try:
                        key = int(val[7:])
                    except ValueError:
                        key = None
                    if key is not None:
                        flat_texts.append((source, fid, name, str(key),
                                           self.loc.text(key)))
            records = []
            for rid in sorted(db.records):
                tkey = db.records[rid]
                name = self.tweak_names.get(rid)
                tname = self.record_types.get(tkey)
                if name is None:
                    continue
                records.append((source, rid, name, tname))
            queries = []
            for qid in sorted(db.queries):
                ents = db.queries[qid]
                queries.append(
                    (source, qid,
                     json.dumps([self.tweak_names.get(e) or hex(e) for e in ents]))
                )
            con.executemany(
                "INSERT INTO tweak_flats (source, flat_id, name, flat_type, value)"
                " VALUES (?, ?, ?, ?, ?)",
                flats,
            )
            con.executemany(
                "INSERT INTO tweak_flat_texts (source, flat_id, name, loc_key, text)"
                " VALUES (?, ?, ?, ?, ?)",
                flat_texts,
            )
            con.executemany(
                "INSERT INTO tweak_records (source, record_id, name, type)"
                " VALUES (?, ?, ?, ?)",
                records,
            )
            con.executemany(
                "INSERT INTO tweak_queries (source, query_id, entries)"
                " VALUES (?, ?, ?)",
                queries,
            )
            print(
                f"{source}: {len(flats)} flats ({len(flat_texts)} localized),"
                f" {len(records)} records, {len(queries)} queries"
            )

    # --------------------------------------------------------------- journal

    def _build_journal(self, con: sqlite3.Connection) -> None:
        """Extract every readable journal entry with its category path."""
        pc = self.game_dir / "archive" / "pc"
        targets = [
            ("base", pc / "content" / "basegame_4_gamedata.archive",
             "base\\journal\\cooked_journal.journal"),
            ("ep1", pc / "ep1" / "ep1_2_gamedata.archive",
             "ep1\\journal\\cooked_journal.journal"),
        ]
        n = 0
        for source, arch_path, entry_path in targets:
            if not arch_path.exists():
                raise BuildError(f"missing gamedata archive {arch_path}")
            ar = Archive(arch_path)
            h = fnv1a64(entry_path.encode("utf-8"))
            if h not in ar.files:
                raise BuildError(
                    f"{entry_path} not found in {arch_path.name} — is this a "
                    "modded or non-vanilla install?"
                )
            f = read_cr2w(ar.read_entry(h))
            if f.root is None:
                raise BuildError(f"could not decode {entry_path}")
            rows = self._journal_rows(f.root, source)
            con.executemany(JOURNAL_INSERT, rows)
            n += len(rows)
        print(f"journal: {n} rows")

    def _journal_rows(self, root: dict, source: str) -> list[tuple]:
        """Walk the journal tree producing typed rows per entry class."""
        rows: list[tuple] = []

        def add(kind: str, node: dict, parent_path: str, title: str, body: str,
                extra: dict | None = None):
            rid = node.get("id") or ""
            rows.append(
                (
                    source,
                    kind,
                    rid,
                    parent_path,
                    title,
                    body,
                    json.dumps(extra, ensure_ascii=False) if extra else None,
                )
            )

        def walk_folder(node: dict, path: str, seen: set) -> None:
            for e in node.get("entries") or []:
                if isinstance(e, dict):
                    walk_entry(e, path, seen)

        def walk_entry(node: dict, path: str, seen: set) -> None:
            key = id(node)
            if key in seen:
                return
            seen.add(key)
            t = node.get("$type")
            nid = node.get("id")
            if nid:
                path = f"{path}/{nid}" if path else str(nid)

            if t in ("gameJournalRootFolderEntry", "gameJournalPrimaryFolderEntry",
                     "gameJournalFolderEntry"):
                add("folder", node, path, str(nid or ""), "")
                walk_folder(node, path, seen)
            elif t == "gameJournalQuest":
                title = self.loc.text(locstr_key(node.get("title")))
                add("quest", node, path, title, "")
                walk_folder(node, path, seen)
            elif t == "gameJournalQuestPhase":
                add("quest_phase", node, path, "", "")
                walk_folder(node, path, seen)
            elif t == "gameJournalQuestObjective":
                desc = self.loc.text(locstr_key(node.get("description")))
                add("quest_objective", node, path, str(nid or ""), desc)
                walk_folder(node, path, seen)
            elif t == "gameJournalQuestDescription":
                desc = self.loc.text(locstr_key(node.get("description")))
                add("quest_description", node, path, str(nid or ""), desc)
            elif t == "gameJournalInternetSite":
                name = self.loc.text(locstr_key(node.get("shortName")))
                add("internet_site", node, path, name, "")
                walk_folder(node, path, seen)
            elif t == "gameJournalInternetPage":
                add("internet_page", node, path, str(nid or ""), "",
                    {"address": node.get("address") or ""})
                # texts are child gameJournalInternetText in a `texts` array;
                # walk both it and entries for structure
                for child in node.get("texts") or []:
                    if isinstance(child, dict):
                        walk_entry(child, path, seen)
                walk_folder(node, path, seen)
            elif t == "gameJournalInternetText":
                body = self.loc.text(locstr_key(node.get("text")))
                nm = node.get("name")
                nm = str(nm) if not isinstance(nm, dict) else ""
                add("shard_text", node, path, nm, body)
            elif t == "gameJournalInternetImage":
                add("internet_image", node, path, str(nid or ""), "")
            elif t == "gameJournalEmailGroup":
                add("email_group", node, path, str(nid or ""), "")
                walk_folder(node, path, seen)
            elif t == "gameJournalEmail":
                title = self.loc.text(locstr_key(node.get("title")))
                sender = self.loc.text(locstr_key(node.get("sender")))
                addressee = self.loc.text(locstr_key(node.get("addressee")))
                body = self.loc.text(locstr_key(node.get("content")))
                add("email", node, path, title, body,
                    {"sender": sender, "addressee": addressee})
            elif t == "gameJournalPhoneConversation":
                add("phone_conversation", node, path, str(nid or ""), "")
                walk_folder(node, path, seen)
            elif t == "gameJournalPhoneMessage":
                body = self.loc.text(locstr_key(node.get("text")))
                sender = node.get("sender") or ""
                add("phone_message", node, path, "", body, {"sender": str(sender)})
            elif t == "gameJournalPhoneChoiceGroup":
                add("phone_choice_group", node, path, str(nid or ""), "")
                walk_folder(node, path, seen)
            elif t == "gameJournalPhoneChoiceEntry":
                body = self.loc.text(locstr_key(node.get("text")))
                add("phone_choice", node, path, "", body)
            elif t in ("gameJournalCodexCategory", "gameJournalCodexGroup"):
                name = self.loc.text(locstr_key(
                    node.get("categoryName") or node.get("groupName")))
                add("codex_section", node, path, name, "")
                walk_folder(node, path, seen)
            elif t == "gameJournalCodexEntry":
                title = self.loc.text(locstr_key(node.get("title")))
                add("codex_entry", node, path, title, "")
                walk_folder(node, path, seen)
            elif t == "gameJournalCodexDescription":
                sub = self.loc.text(locstr_key(node.get("subTitle")))
                body = self.loc.text(locstr_key(node.get("textContent")))
                add("codex_description", node, path, sub, body)
            elif t == "gameJournalTarot":
                name = self.loc.text(locstr_key(node.get("name")))
                body = self.loc.text(locstr_key(node.get("description")))
                add("tarot", node, path, name, body)
            elif t == "gameJournalContact":
                add("contact", node, path, str(nid or ""), "")
                walk_folder(node, path, seen)
            elif t == "gameJournalFileGroup":
                add("file_group", node, path, str(nid or ""), "")
                walk_folder(node, path, seen)
            elif t == "gameJournalFile":
                title = self.loc.text(locstr_key(node.get("title")))
                body = self.loc.text(locstr_key(node.get("content")))
                add("file", node, path, title, body)
            elif t == "gameJournalOnscreenGroup":
                add("onscreen_group", node, path, str(nid or ""), "")
                walk_folder(node, path, seen)
            elif t == "gameJournalOnscreen":
                title = self.loc.text(locstr_key(node.get("title")))
                desc = self.loc.text(locstr_key(node.get("description")))
                add("onscreen", node, path, title, desc)
            elif t == "gameJournalBriefing":
                add("briefing", node, path, str(nid or ""), "")
                walk_folder(node, path, seen)
            elif t == "gameJournalBriefingVideoSection":
                add("briefing_video", node, path, str(nid or ""), "")
                walk_folder(node, path, seen)
            elif t == "gameJournalImageEntry":
                add("image", node, path, str(nid or ""), "")
            elif t == "gameJournalPointOfInterestGroup":
                add("poi_group", node, path, str(nid or ""), "")
                walk_folder(node, path, seen)
            elif t == "gameJournalPointOfInterestMappin":
                add("poi", node, path, str(nid or ""), "")
            elif t == "gameJournalQuestCodexLink":
                add("quest_codex_link", node, path, str(nid or ""), "")
            elif t == "gameJournalQuestMapPin":
                add("map_pin", node, path, str(nid or ""), "")
            elif t == "gameJournalPath":
                pass
            else:
                # Unknown entry class: descend so nothing readable is lost.
                walk_folder(node, path, seen)

        root_node = root.get("entry") or {}
        walk_folder(root_node, "", set())
        return rows

    # ------------------------------------------------------------- subtitles

    def _build_subtitles(self, con: sqlite3.Connection) -> None:
        pc = self.game_dir / "archive" / "pc"
        n = 0
        for prefix, folder in SOURCES:
            arch = pc / folder / f"lang_{self.lang}_text.archive"
            if not arch.exists():
                raise BuildError(f"missing language archive {arch}")
            ar = Archive(arch)
            for h in sorted(ar.files):
                path = self.used_hashes.get(h)
                if not path or "\\subtitles\\" not in path:
                    continue
                f = read_cr2w(ar.read_entry(h))
                inner = f.root.get("root") if f.root else None
                if not isinstance(inner, dict):
                    continue
                for e in inner.get("entries") or []:
                    if not isinstance(e, dict):
                        continue
                    body = (e.get("femaleVariant")
                            or e.get("maleVariant") or "").replace("\\n", "\n")
                    con.execute(
                        "INSERT INTO subtitles (source, file_path, string_id, line)"
                        " VALUES (?, ?, ?, ?)",
                        (prefix, path, str(e.get("stringId") or 0), body),
                    )
                    n += 1
        print(f"subtitles: {n}")

    # ------------------------------------------------------------------- fts

    def _build_fts(self, con: sqlite3.Connection) -> None:
        con.executescript(FTS_SCHEMA)
        for fts in ("journal_fts", "lockeys_fts", "subtitles_fts"):
            con.execute(f"INSERT INTO {fts}({fts}) VALUES ('rebuild')")
        print("fts: rebuilt")


def flat_value_json(val: object) -> str:
    """Serialize a flat value; tuples (colors/vectors) become JSON arrays."""
    if isinstance(val, tuple):
        return json.dumps(list(val), separators=(",", ":"))
    return json.dumps(val, separators=(",", ":"))


JOURNAL_INSERT = (
    "INSERT INTO journal (source, kind, entry_id, path, title, body, extra)"
    " VALUES (?, ?, ?, ?, ?, ?, ?)"
)

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE lockeys (
    loc_key TEXT PRIMARY KEY,
    secondary_key TEXT NOT NULL,
    female_variant TEXT NOT NULL,
    male_variant TEXT NOT NULL
);

CREATE TABLE tweak_flats (
    source TEXT NOT NULL,
    flat_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    flat_type TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (source, flat_id)
);
CREATE INDEX idx_tweak_flats_name ON tweak_flats (name);

CREATE TABLE tweak_flat_texts (
    source TEXT NOT NULL,
    flat_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    loc_key TEXT NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (source, flat_id)
);
CREATE INDEX idx_tweak_flat_texts_name ON tweak_flat_texts (name);

CREATE TABLE tweak_records (
    source TEXT NOT NULL,
    record_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    type TEXT,
    PRIMARY KEY (source, record_id)
);
CREATE INDEX idx_tweak_records_name ON tweak_records (name);
CREATE INDEX idx_tweak_records_type ON tweak_records (type);

CREATE TABLE tweak_queries (
    source TEXT NOT NULL,
    query_id INTEGER NOT NULL,
    entries TEXT NOT NULL,
    PRIMARY KEY (source, query_id)
);

CREATE TABLE journal (
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    extra TEXT
);
CREATE INDEX idx_journal_kind ON journal (kind);
CREATE INDEX idx_journal_path ON journal (path);

CREATE TABLE subtitles (
    source TEXT NOT NULL,
    file_path TEXT NOT NULL,
    string_id TEXT NOT NULL,
    line TEXT NOT NULL
);
CREATE INDEX idx_subtitles_file ON subtitles (file_path);

CREATE VIEW v_items AS
SELECT
    r.source,
    r.name AS record_name,
    r.type AS record_type,
    (SELECT text FROM tweak_flat_texts t WHERE t.name = r.name || '.displayName' AND t.source = r.source) AS display_name,
    (SELECT text FROM tweak_flat_texts t WHERE t.name = r.name || '.localizedDescription' AND t.source = r.source) AS description
FROM tweak_records r
WHERE r.name LIKE 'Items.%'
  AND EXISTS (SELECT 1 FROM tweak_flat_texts t WHERE t.name = r.name || '.displayName');

CREATE VIEW v_shards AS
-- One row per internet page with all its readable text concatenated.
SELECT
    j.source,
    j.path AS page_path,
    p.title AS page_title,
    group_concat(j.title, '\\n') AS section_titles,
    group_concat(NULLIF(j.body, ''), '\\n\\n') AS body
FROM journal j
JOIN journal p ON p.path = j.path AND p.kind = 'internet_page' AND p.source = j.source
WHERE j.kind = 'shard_text'
GROUP BY j.source, j.path;

CREATE VIEW v_shard_texts AS
SELECT j.* FROM journal j WHERE kind = 'shard_text' AND body != '';

CREATE VIEW v_codex AS
SELECT j.* FROM journal j WHERE kind IN ('codex_section','codex_entry','codex_description');

CREATE VIEW v_emails AS
SELECT j.* FROM journal j WHERE kind = 'email';

CREATE VIEW v_quests AS
SELECT j.* FROM journal j WHERE kind IN ('quest','quest_phase','quest_objective','quest_description');

CREATE VIEW v_tarots AS
SELECT j.* FROM journal j WHERE kind = 'tarot';

CREATE VIEW v_vehicles AS
SELECT
    r.source,
    r.name AS record_name,
    (SELECT text FROM tweak_flat_texts t WHERE t.name = r.name || '.displayName' AND t.source = r.source) AS display_name,
    (SELECT value FROM tweak_flats f WHERE f.name = r.name || '.manufacturer' AND f.source = r.source) AS manufacturer
FROM tweak_records r
WHERE r.type = 'Vehicle'
  AND r.name NOT LIKE '%inline%';

CREATE VIEW v_perks AS
-- Old (pre-2.0) Perks.* and current NewPerks.* records; names/descriptions
-- come from loc_name_key / loc_desc_key string flats carrying LocKey#N.
SELECT
    r.source,
    r.name AS record_name,
    r.type AS record_type,
    (SELECT text FROM tweak_flat_texts t WHERE t.name = r.name || '.loc_name_key' AND t.source = r.source) AS name_text,
    (SELECT text FROM tweak_flat_texts t WHERE t.name = r.name || '.loc_desc_key' AND t.source = r.source) AS description
FROM tweak_records r
WHERE r.type IN ('Perk', 'NewPerk', 'BuildPerk', 'BuildNewPerk')
  AND (r.name LIKE 'Perks.%' OR r.name LIKE 'NewPerks.%')
  AND r.name NOT LIKE '%inline%';
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE journal_fts USING fts5(
    title, body, path,
    content='journal', content_rowid='rowid',
    tokenize = "unicode61 remove_diacritics 2"
);
CREATE VIRTUAL TABLE lockeys_fts USING fts5(
    secondary_key, female_variant, male_variant,
    content='lockeys', content_rowid='rowid',
    tokenize = "unicode61 remove_diacritics 2"
);
CREATE VIRTUAL TABLE subtitles_fts USING fts5(
    line,
    content='subtitles', content_rowid='rowid',
    tokenize = "unicode61 remove_diacritics 2"
);
"""


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("game_dir", help="Cyberpunk 2077 install directory")
    ap.add_argument("db_path", help="output SQLite file")
    ap.add_argument("--lang", default="en",
                    choices=GAME_LANGS,
                    help="onscreens language (default: en)")
    args = ap.parse_args()
    try:
        builder = DatasetBuilder(Path(args.game_dir), Path(args.db_path),
                                 args.lang)
        builder.build()
    except (BuildError, ArchiveError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()