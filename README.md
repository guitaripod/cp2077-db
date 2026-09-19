# cp2077-db

One SQLite file containing every readable string in a Cyberpunk 2077 install — shards, the entire
encyclopedia (codex), emails, phone messages, quests, tarot cards, item/weapon/cyberware names and
descriptions, vehicles, perks, and 109k lines of spoken dialogue subtitles — indexed for full-text
search. Build your own UI on top: it's a plain SQLite database with FTS5, typed tables and curated
views. No server, no API, no dependencies beyond Python's stdlib.

## What's in the dataset

| Table / view | Contents |
|---|---|
| `lockeys` | Every localization key: `loc_key`, `secondary_key`, `female_variant`, `male_variant` (70,579 rows, en) |
| `journal` | The whole in-game journal tree: shards, codex, emails, phone messages/choices, quests, objectives, tarots, contacts, files, map pins, POIs (47k rows) |
| `subtitles` | Every spoken line with source file + `string_id` (109,205 rows) |
| `tweak_records` | All TweakDB records with resolved names and types (369,552: items, vehicles, perks, and everything else) |
| `tweak_flats` | All TweakDB flat values with their red types (6.18M) |
| `tweak_flat_texts` | LocKey-carrying flats resolved to text (137k) |
| `tweak_queries` | TweakDB queries (record group definitions) |
| `journal_fts`, `lockeys_fts`, `subtitles_fts` | FTS5 indexes (bm25-ranked, 2- and 3-character prefix indexes for autocomplete) |

Curated views — each one is flat, readable and ready to render:

| View | One row per | Useful columns |
|---|---|---|
| `v_shards` | internet page | `site_title`, `address`, `text_count`, `body` (sections joined with blank lines) |
| `v_shard_texts` | text widget on a page | `path`, `body` |
| `v_codex` | codex article | `section`, `title` (from the entry), `subtitle`, `body` |
| `v_emails` | email | `subject`, `sender`, `addressee`, `body` |
| `v_contacts` | phone contact | `name`, `contact_type`, `message_count` |
| `v_phone` | phone message or reply | `contact`, `conversation`, `line_type`, `text`, `quest_important` |
| `v_quests` | quest | `title`, `quest_type`, `district`, `content_assignment`, `description`, `objective_count` |
| `v_objectives` | quest objective | `quest`, `description`, `optional`, `counter` |
| `v_dialogue` | spoken line | `scene` (`q112_08_parade_speech`), `string_id`, `line` |
| `v_map_pins` | map pin / POI with a caption | `caption`, `content_assignment` |
| `v_tarots` | tarot card | `title`, `body` |
| `v_items` | item with a display name | `display_name`, `description`, `quality`, `tags`, `record_type` |
| `v_vehicles`, `v_perks` | vehicle / perk | resolved names and descriptions |
| `v_flat_refs` | TweakDBID-valued flat | `flat_name` → `record_name`, `record_type` |
| `v_codex_tree`, `v_quest_tree` | raw journal rows | the hierarchy behind the flat views |

`journal.id` and `subtitles.id` are stable integer keys within a build (the build is
deterministic, so they are stable across rebuilds of the same game version) — use them as
foreign keys from your own tables. `meta.schema_version` tells you which layout a file has.

`journal.path` mirrors the game's internal category tree
(`codex/characters/quests/johnny_silverhand`, `onscreens/emails/quests/...`,
`internet_sites/n54/...`), so hierarchical UIs fall out of `ORDER BY path`.

## Build

```bash
pip install --user .        # installs the `cpdb` CLI
                            # (Arch/Debian: add --break-system-packages, or use pipx)
cpdb build "/path/to/Cyberpunk 2077" cp2077.sqlite --lang en
```

(Or without installing: `PYTHONPATH=src python -m cpdb.cli build <game dir> [db] [--lang XX]`.)

Requirements: the game installed with text archives present (Steam/GOG layout), ~3 GB free
disk (the finished file is ~1.3 GB). Build time ~60 s. Other languages: `--lang de` etc. uses
`lang_<code>_text.archive` (19 languages available in the game files).

The build reads only: `r6/cache/tweakdb.bin` + `tweakdb_ep1.bin`,
`archive/pc/{content,ep1}/lang_<lang>_text.archive`,
`archive/pc/content/basegame_4_gamedata.archive` and
`archive/pc/ep1/ep1_2_gamedata.archive`. Nothing is modified, and a missing input is
reported before any work starts.

## Tests

```bash
python -m unittest discover -s tests          # parser regressions, no game needed
CP2077_DIR="/path/to/Cyberpunk 2077" python -m unittest tests.test_build
CPDB_SLOW=1 TMPDIR=/some/disk python -m unittest tests.test_build   # + determinism
```

`tests/test_cpdb.py` runs against synthetic buffers: FNV1A64/murmur3/TweakDBID
vectors, VLQ decoding, the one-based `raRef` import index, skip-by-declared-size
for unknown red types, and every malformed-archive path (junk, truncated header,
bad version, out-of-range index or segment range, duplicate name hashes).
`tests/test_query.py` builds a miniature dataset from the real schema and covers the
query layer: ranked search, excerpts, source filters, `page`, `tree`, the schema-version
guard and the curated views. `tests/test_build.py` builds from a real install and checks
row counts, view content, FTS hits, the timestamp-free `meta` table, and — with
`CPDB_SLOW=1` — that two independent builds are byte-identical.

## Query

```bash
cpdb cp2077.sqlite search '"Arasaka tower"'              # bm25-ranked, with excerpts
cpdb cp2077.sqlite search 'neurotox*' --source subtitle  # one index only
cpdb cp2077.sqlite tree codex/characters                 # categories + entry counts
cpdb cp2077.sqlite show codex/characters/quests/johnny_silverhand   # entry + its text
cpdb cp2077.sqlite page n54/tower                        # one shard page in full
cpdb cp2077.sqlite table v_shards --where "page_path LIKE '%n54%'" --limit 5
cpdb cp2077.sqlite table v_items --where "display_name LIKE '%Yukimura%'"
cpdb cp2077.sqlite item Items.Preset_Yukimura_Default
cpdb cp2077.sqlite sql "SELECT kind, COUNT(*) FROM journal GROUP BY kind"
cpdb cp2077.sqlite stats
```

Or from any language with SQLite:

```python
import sqlite3, json
con = sqlite3.connect("cp2077.sqlite")
rows = con.execute(
    "SELECT title, body FROM journal_fts WHERE journal_fts MATCH 'neurotoxin'"
).fetchall()
```

Every command takes `--json` for structured output.

## How it works

All parsers are implemented from scratch in stdlib Python, reverse-engineered from
[WolvenKit](https://github.com/WolvenKit/WolvenKit)'s C# readers (GPL-3.0):

- `cpdb/engine.py` — REDengine RDAR v12 `.archive` container reader; per-entry SHA1
  verification; Oodle Kraken decompression via vendored `libkraken.so`.
- `cpdb/tweakdb.py` — `tweakdb.bin` blob parser: flats pool (22 typed value groups keyed by
  FNV1A64 of the red type name), records (CRC32+len TweakDBIDs + MurmurHash3 type keys),
  queries, group tags.
- `cpdb/cr2w.py` — CR2W container + RedPackage CVariable reader, self-describing
  (name/type/size per variable), enough to decode the entire 47k-chunk journal in 8 s.
- Name resolution: TweakDBIDs → path names and archive FNV hashes → paths via WolvenKit's
  community hash tables (vendored under `src/cpdb/data/`, GPL-3.0). 100% of records resolve.

Byte-level correctness is verified during extraction: every archive entry's SHA1 is checked,
and the CR2W/onscreens decode was validated field-for-field against WolvenKit CLI's own
serializer.

The build is deterministic and fail-fast: missing game files are reported before any work
starts, `meta` records an input fingerprint instead of a wall-clock timestamp, unknown or
unsupported red types are skipped by their declared size (never guessed at), and the database
is written to a temporary file and renamed only on success — a failed build leaves no partial
dataset behind. Two builds from the same install are byte-identical.

## Personal-use note

The dataset contains CD PROJEKT RED's copyrighted game text. This repo never stores or
publishes extracted content — it only contains the extraction code. Keep your built
`cp2077.sqlite` to yourself.

## License

GPL-3.0. `libkraken.so` and the vendored hash tables originate from WolvenKit (GPL-3.0);
see their repository for source and credits.