# cp2077-db

Every readable string in a Cyberpunk 2077 install, in one SQLite file: shards, codex, emails,
phone messages, quests, tarot, item and perk text, 109k dialogue lines, plus the journal's
pictures (codex art, avatars, tarot cards, net-page images) at the game's 4K-UI resolution.
FTS5-indexed, with curated views ready to render. Stdlib Python; pictures need Pillow.

```bash
pip install --user '.[images]'                        # Arch/Debian: --break-system-packages
cpdb build "/path/to/Cyberpunk 2077" cp2077.sqlite --images   # ~3 min, ~1.4 GB (60 s without)
cpdb cp2077.sqlite search '"Arasaka tower"'           # bm25-ranked, with excerpts
cpdb cp2077.sqlite show codex/characters/quests/johnny_silverhand
cpdb cp2077.sqlite image codex/locations/quest/arasaka_tower tower.webp
cpdb cp2077.sqlite export reader.sqlite               # reader profile: text + pictures, ~180 MB
```

| View | One row per | Columns |
|---|---|---|
| `v_shards` | net page | `site_title`, `address`, `body`, `images` |
| `v_codex` | codex article | `section`, `title`, `subtitle`, `body`, `image`, `thumb` |
| `v_emails` | email | `subject`, `sender`, `addressee`, `body`, `image` |
| `v_contacts`, `v_phone` | contact / phone line | `name`, `avatar`, `conversation`, `line_type`, `text` |
| `v_quests`, `v_objectives` | quest / objective | `title`, `quest_type`, `district`, `description` |
| `v_dialogue` | spoken line | `scene`, `string_id`, `line` |
| `v_items`, `v_vehicles`, `v_perks` | TweakDB record | resolved names and descriptions |
| `v_map_pins`, `v_tarots`, `v_flat_refs` | caption / card / TweakDBID reference | |

Tables: `journal` (the whole journal tree, `path` mirrors the game's category tree; the game's
own datashards are `kind = 'onscreen'`), `subtitles`, `lockeys`, `tweak_records`, `tweak_flats`,
`tweak_flat_texts`, `images` (WebP q85 blobs keyed `<atlas>#<part>`, referenced from
`journal.extra` as `image`, `thumb` or `avatar`), `meta`. `journal.id` is stable across rebuilds
of the same game files.

Building a dataset needs Linux: the vendored Oodle Kraken decompressor is a Linux
`.so`, and it is loaded only when an archive is read, so querying, exporting and
dumping pictures from a dataset someone else built works on macOS and Windows too.

Parsers for RDAR archives, TweakDB blobs and CR2W resources are reimplemented from
[WolvenKit](https://github.com/WolvenKit/WolvenKit)'s readers; `libkraken.so` and the hash
tables are vendored from it. Builds are deterministic (two builds are byte-identical) and
fail-fast. `python -m unittest discover -s tests` runs the parser tests without a game install.

The dataset is CD PROJEKT RED's text: build it locally, keep it to yourself. GPL-3.0.
