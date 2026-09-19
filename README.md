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
| `journal` | The whole in-game journal tree: shards, codex, emails, phone messages/choices, quests, objectives, tarots, contacts, files, POIs (47,379 rows) |
| `subtitles` | Every spoken line with source file + `string_id` (109,205 rows) |
| `tweak_records` | All TweakDB records with resolved names and types (369,552: items, vehicles, perks, and everything else) |
| `tweak_flats` | All TweakDB flat values with their red types (6.19M) |
| `tweak_flat_texts` | LocKey-carrying flats resolved to text (126,927) |
| `tweak_queries` | TweakDB queries (record group definitions) |
| `journal_fts`, `lockeys_fts`, `subtitles_fts` | FTS5 indexes over all of the above |
| `v_shards` | Internet pages (shards) with concatenated readable text, one row per page |
| `v_shard_texts` | Individual text widgets of shard pages |
| `v_codex`, `v_emails`, `v_quests`, `v_tarots` | Codex, emails, quests, tarot cards |
| `v_items` | Items with display name + description resolved |
| `v_vehicles`, `v_perks` | Vehicles and perks with resolved text |

`journal.path` mirrors the game's internal category tree
(`codex/characters/quests/johnny_silverhand`, `onscreens/emails/quests/...`,
`internet_sites/n54/...`), so hierarchical UIs fall out of `ORDER BY path`.

## Build

```bash
pip install --user .        # installs the `cpdb` CLI
cpdb build "/path/to/Cyberpunk 2077" cp2077.sqlite --lang en
```

(Or without installing: `PYTHONPATH=src python -m cpdb.build <game dir> <db> [--lang XX]`.)

Requirements: the game installed with text archives present (Steam/GOG layout), ~2.5 GB free
disk. Build time ~100 s. Other languages: `--lang de` etc. uses `lang_<code>_text.archive`
(19 languages available in the game files).

The build reads only: `r6/cache/tweakdb.bin` + `tweakdb_ep1.bin`,
`archive/pc/{content,ep1}/lang_<lang>_text.archive`,
`archive/pc/{content,ep1}/*_gamedata.archive`. Nothing is modified.

## Query

```bash
cpdb cp2077.sqlite search '"Arasaka tower"'
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

## Personal-use note

The dataset contains CD PROJEKT RED's copyrighted game text. This repo never stores or
publishes extracted content — it only contains the extraction code. Keep your built
`cp2077.sqlite` to yourself.

## License

GPL-3.0. `libkraken.so` and the vendored hash tables originate from WolvenKit (GPL-3.0);
see their repository for source and credits.