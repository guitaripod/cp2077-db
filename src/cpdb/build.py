"""Build the cp2077 SQLite dataset from an installed game.

Inputs (all read directly from the game install):
  * r6/cache/tweakdb.bin + tweakdb_ep1.bin (TweakDB blobs, vanilla-verified)
  * archive/pc/content/lang_en_text.archive + archive/pc/ep1/lang_en_text.archive
    (onscreens LocKey table + subtitle files)
  * archive/pc/{content,ep1} gamedata archives: base\\journal\\cooked_journal.journal
    + ep1\\journal\\cooked_journal.journal (shards, codex, emails, ...)
  * WolvenKit hash tables (usedhashes.kark, tweakdbstr.kark) vendored under
    data/ for path and TweakDBID resolution

Output: cp2077.sqlite with typed tables, curated views, and FTS5 full-text
search over all readable text.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

from .cr2w import read_cr2w
from .engine import Archive, fnv1a64, kark_decompress
from .tweakdb import parse as parse_tweakdb, tweakdbid_hash, murmur3, crc32

DATA_DIR = Path(__file__).resolve().parent / "data"
GAME_LANGS = ["ar", "cs", "de", "en", "es-es", "es-mx", "fr", "hu", "it", "ja",
              "ko", "pl", "pt", "ru", "th", "tr", "ua", "zh-cn", "zh-tw"]


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
    n_names = 0
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
        n_names += 1
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
        for prefix, arch in (
            ("base", pc / "content" / f"lang_{self.lang}_text.archive"),
            ("ep1", pc / "ep1" / f"lang_{self.lang}_text.archive"),
        ):
            if not arch.exists():
                continue
            ar = Archive(arch)
            # The language folder is discovered from the archive contents:
            # any entry whose path ends with onscreens\\onscreens_final.json
            # under this prefix. Archive files are en-us etc.; "en" matches
            # lang_en_text.archive contents only.
            path = None
            for h in ar.files:
                p = used_hashes.get(h)
                if (
                    p
                    and p.startswith(prefix + "\\localization\\")
                    and p.endswith("\\onscreens\\onscreens_final.json")
                ):
                    path = p
                    break
            if path is None:
                print(f"warning: no onscreens in {arch.name}", file=sys.stderr)
                continue
            f = read_cr2w(ar.read_entry(fnv1a64(path.encode("utf-8"))))
            if f.root is None:
                continue
            inner = f.root.get("root")
            if not isinstance(inner, dict):
                continue
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


def journal_walk(node: object):
    """Yield every dict in the decoded journal tree depth-first."""
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            yield n
            for v in n.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(n, list):
            for v in n:
                stack.append(v)


class DatasetBuilder:
    def __init__(self, game_dir: Path, db_path: Path, lang: str = "en"):
        self.game_dir = Path(game_dir)
        self.db_path = Path(db_path)
        self.lang = lang
        self.used_hashes = load_used_hashes()
        self.tweak_names = load_tweak_names()
        self.record_types = load_record_types()
        self.loc = LocResolver(lang, self.game_dir, self.used_hashes)

    # ------------------------------------------------------------------ build

    def build(self) -> None:
        t0 = time.time()
        if self.db_path.exists():
            self.db_path.unlink()
        con = sqlite3.connect(self.db_path)
        con.executescript(SCHEMA)
        self._build_meta(con)
        self._build_strings(con)
        self._build_tweakdb(con)
        self._build_journal(con)
        self._build_subtitles(con)
        self._build_fts(con)
        con.execute("ANALYZE")
        con.commit()
        con.close()
        print(f"dataset built in {time.time()-t0:.1f}s -> {self.db_path}")

    def _build_meta(self, con: sqlite3.Connection) -> None:
        con.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("built_at", time.strftime("%Y-%m-%dT%H:%M:%S")),
        )
        con.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            ("language", self.lang),
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
                print(f"warning: {path} missing, skipping", file=sys.stderr)
                continue
            db = parse_tweakdb(path.read_bytes())
            flats = []
            flat_texts = []
            for fid, val in db.flats.items():
                name = self.tweak_names.get(fid)
                if name is None:
                    continue
                red_type = db.flat_type_by_id.get(fid, "")
                flats.append((source, fid, name, red_type, flat_value_json(val)))
                if red_type == "gamedataLocKeyWrapper" and isinstance(val, int):
                    flat_texts.append((source, fid, name, str(val),
                                       self.loc.text(val)))
                elif red_type == "String" and isinstance(val, str) and val.startswith("LocKey#"):
                    try:
                        key = int(val[7:])
                    except ValueError:
                        key = None
                    if key is not None:
                        flat_texts.append((source, fid, name, str(key),
                                           self.loc.text(key)))
            records = []
            for rid, tkey in db.records.items():
                name = self.tweak_names.get(rid)
                tname = self.record_types.get(tkey)
                if name is None:
                    continue
                records.append((source, rid, name, tname))
            queries = [
                (source, qid, json.dumps([self.tweak_names.get(e) or hex(e) for e in ents]))
                for qid, ents in db.queries.items()
            ]
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
                continue
            ar = Archive(arch_path)
            h = fnv1a64(entry_path.encode("utf-8"))
            if h not in ar.files:
                print(f"warning: {entry_path} not found in {arch_path.name}",
                      file=sys.stderr)
                continue
            f = read_cr2w(ar.read_entry(h))
            if f.root is None:
                continue
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
                    json.dumps(extra) if extra else None,
                )
            )

        # First pass: index nodes by handle identity is not needed — the tree
        # is already nested; we walk with an accumulated path from the root.
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
                return
            if t == "gameJournalQuest":
                title = self.loc.text(locstr_key(node.get("title")))
                add("quest", node, path, title, "")
                walk_folder(node, path, seen)
                return
            if t == "gameJournalQuestPhase":
                add("quest_phase", node, path, "", "")
                walk_folder(node, path, seen)
                return
            if t == "gameJournalQuestObjective":
                desc = self.loc.text(locstr_key(node.get("description")))
                add("quest_objective", node, path, str(nid or ""), desc)
                walk_folder(node, path, seen)
                return
            if t == "gameJournalQuestDescription":
                desc = self.loc.text(locstr_key(node.get("description")))
                add("quest_description", node, path, str(nid or ""), desc)
                return
            if t == "gameJournalInternetSite":
                name = self.loc.text(locstr_key(node.get("shortName")))
                add("internet_site", node, path, name, "")
                walk_folder(node, path, seen)
                return
            if t == "gameJournalInternetPage":
                add("internet_page", node, path, str(nid or ""), "",
                    {"address": node.get("address") or ""})
                # texts are child gameJournalInternetText in a `texts` array;
                # walk both it and entries for structure
                for child in node.get("texts") or []:
                    if isinstance(child, dict):
                        walk_entry(child, path, seen)
                walk_folder(node, path, seen)
                return
            if t == "gameJournalInternetText":
                body = self.loc.text(locstr_key(node.get("text")))
                nm = node.get("name")
                nm = str(nm) if not isinstance(nm, dict) else ""
                add("shard_text", node, path, nm, body)
                return
            if t == "gameJournalInternetImage":
                add("internet_image", node, path, str(nid or ""), "")
                return
            if t == "gameJournalEmailGroup":
                add("email_group", node, path, str(nid or ""), "")
                walk_folder(node, path, seen)
                return
            if t == "gameJournalEmail":
                title = self.loc.text(locstr_key(node.get("title")))
                sender = self.loc.text(locstr_key(node.get("sender")))
                addressee = self.loc.text(locstr_key(node.get("addressee")))
                body = self.loc.text(locstr_key(node.get("content")))
                add("email", node, path, title, body,
                    {"sender": sender, "addressee": addressee})
                return
            if t == "gameJournalPhoneConversation":
                add("phone_conversation", node, path, str(nid or ""), "")
                walk_folder(node, path, seen)
                return
            if t == "gameJournalPhoneMessage":
                body = self.loc.text(locstr_key(node.get("text")))
                sender = node.get("sender") or ""
                add("phone_message", node, path, "", body, {"sender": str(sender)})
                return
            if t == "gameJournalPhoneChoiceGroup":
                add("phone_choice_group", node, path, str(nid or ""), "")
                walk_folder(node, path, seen)
                return
            if t == "gameJournalPhoneChoiceEntry":
                body = self.loc.text(locstr_key(node.get("text")))
                add("phone_choice", node, path, "", body)
                return
            if t == "gameJournalCodexCategory" or t == "gameJournalCodexGroup":
                name = self.loc.text(locstr_key(
                    node.get("categoryName") or node.get("groupName")))
                add("codex_section", node, path, name, "")
                walk_folder(node, path, seen)
                return
            if t == "gameJournalCodexEntry":
                title = self.loc.text(locstr_key(node.get("title")))
                add("codex_entry", node, path, title, "")
                walk_folder(node, path, seen)
                return
            if t == "gameJournalCodexDescription":
                sub = self.loc.text(locstr_key(node.get("subTitle")))
                body = self.loc.text(locstr_key(node.get("textContent")))
                add("codex_description", node, path, sub, body)
                return
            if t == "gameJournalTarot":
                name = self.loc.text(locstr_key(node.get("name")))
                body = self.loc.text(locstr_key(node.get("description")))
                add("tarot", node, path, name, body)
                return
            if t == "gameJournalContact":
                add("contact", node, path, str(nid or ""), "")
                walk_folder(node, path, seen)
                return
            if t == "gameJournalFileGroup":
                add("file_group", node, path, str(nid or ""), "")
                walk_folder(node, path, seen)
                return
            if t == "gameJournalFile":
                title = self.loc.text(locstr_key(node.get("title")))
                body = self.loc.text(locstr_key(node.get("content")))
                add("file", node, path, title, body)
                return
            if t == "gameJournalOnscreenGroup":
                add("onscreen_group", node, path, str(nid or ""), "")
                walk_folder(node, path, seen)
                return
            if t == "gameJournalOnscreen":
                title = self.loc.text(locstr_key(node.get("title")))
                desc = self.loc.text(locstr_key(node.get("description")))
                add("onscreen", node, path, title, desc)
                return
            if t == "gameJournalBriefing":
                add("briefing", node, path, str(nid or ""), "")
                walk_folder(node, path, seen)
                return
            if t == "gameJournalBriefingVideoSection":
                add("briefing_video", node, path, str(nid or ""), "")
                walk_folder(node, path, seen)
                return
            if t == "gameJournalImageEntry":
                add("image", node, path, str(nid or ""), "")
                return
            if t == "gameJournalPointOfInterestGroup":
                add("poi_group", node, path, str(nid or ""), "")
                walk_folder(node, path, seen)
                return
            if t == "gameJournalPointOfInterestMappin":
                add("poi", node, path, str(nid or ""), "")
                return
            if t == "gameJournalQuestCodexLink":
                add("quest_codex_link", node, path, str(nid or ""), "")
                return
            if t == "gameJournalQuestMapPin":
                add("map_pin", node, path, str(nid or ""), "")
                return
            if t == "gameJournalPath":
                return
            # unknown: still record folders so nothing readable is silently lost
            walk_folder(node, path, seen)

        root_node = root.get("entry") or {}
        walk_folder(root_node, "", set())
        return rows

    # ------------------------------------------------------------- subtitles

    def _build_subtitles(self, con: sqlite3.Connection) -> None:
        pc = self.game_dir / "archive" / "pc"
        n = 0
        for prefix, arch in (
            ("base", pc / "content" / f"lang_{self.lang}_text.archive"),
            ("ep1", pc / "ep1" / f"lang_{self.lang}_text.archive"),
        ):
            if not arch.exists():
                continue
            ar = Archive(arch)
            for h in ar.files:
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
                    body = (e.get("femaleVariant") or e.get("maleVariant") or "").replace(
                        "\\n", "\n"
                    )
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
        con.execute(
            "INSERT INTO journal_fts(journal_fts)"
            " SELECT 'rebuild' WHERE EXISTS (SELECT 1 FROM journal WHERE body != '' OR title != '')"
        )
        con.execute(
            "INSERT INTO lockeys_fts(lockeys_fts)"
            " SELECT 'rebuild' WHERE EXISTS (SELECT 1 FROM lockeys)"
        )
        con.execute(
            "INSERT INTO subtitles_fts(subtitles_fts)"
            " SELECT 'rebuild' WHERE EXISTS (SELECT 1 FROM subtitles)"
        )
        print("fts: rebuilt")


def flat_value_json(val: object) -> str:
    """Serialize a flat value: LocKey ints resolved later by views; keep raw."""
    if isinstance(val, tuple):
        return json.dumps(list(val))
    return json.dumps(val)


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
    group_concat(j.title, '\n') AS section_titles,
    group_concat(NULLIF(j.body, ''), '\n\n') AS body
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
    ap.add_argument("--lang", default="en", help="onscreens language (default: en)")
    args = ap.parse_args()
    builder = DatasetBuilder(Path(args.game_dir), Path(args.db_path), args.lang)
    builder.build()


if __name__ == "__main__":
    main()