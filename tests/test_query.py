"""Query-layer tests against a miniature dataset built from the real schema.

The fixture uses `build.SCHEMA` / `build.FTS_SCHEMA` verbatim, so a schema
change that breaks the CLI fails here rather than only on a 60-second build.
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cpdb import build, cli  # noqa: E402

JOURNAL_ROWS = [
    ("base", "internet_page", "p1", "internet_sites/n54/tower", "tower", "", None),
    ("base", "shard_text", "t1", "internet_sites/n54/tower",
     "headline", "Arasaka tower stands over Night City.", None),
    ("base", "shard_text", "t2", "internet_sites/n54/tower",
     "body", "Militech denies involvement in the tower raid.", None),
    ("base", "codex_entry", "c1", "codex/characters/johnny",
     "Johnny Silverhand", "",
     json.dumps({"image": "base\\gui\\codex.inkatlas#johnny_full",
                 "thumb": "base\\gui\\codex.inkatlas#johnny"})),
    ("base", "codex_description", "c2", "codex/characters/johnny/johnny_desc",
     "", "A rockerboy engram riding in V's head.", None),
    ("ep1", "email", "e1", "onscreens/emails/quests/dogtown",
     "Welcome to Dogtown", "Barghest runs the place now.",
     json.dumps({"sender": "Mr. Hands"})),
]

LOCKEY_ROWS = [
    ("LocKey#1", "ui-tower-label", "Arasaka Tower", "Arasaka Tower"),
    ("LocKey#2", "ui-militech-label", "Militech", "Militech"),
]

PHONE_ROWS = [
    ("base", "contact", "zane", "contacts/zane", "Zane", "",
     json.dumps({"contact_type": "Friend"})),
    ("base", "phone_conversation", "mq030", "contacts/zane/mq030",
     "About the gig", "", None),
    ("base", "phone_message", "m1", "contacts/zane/mq030/m1", "",
     "Thanks for the help, V.", json.dumps({"quest_important": True})),
    ("base", "phone_choice", "c1", "contacts/zane/mq030/c1", "",
     "Anytime.", None),
    ("base", "quest", "q001", "quests/main_quest/q001", "The Heist", "",
     json.dumps({"quest_type": "MainQuest", "district": "Districts.Watson",
                 "content_assignment": "DeviceContentAssignment.q001"})),
    ("base", "quest_description", "d1", "quests/main_quest/q001/d1", "d1",
     "Steal the relic from Konpeki Plaza.", None),
    ("base", "quest_objective", "o1", "quests/main_quest/q001/o1", "o1",
     "Meet Jackie at the bar.", json.dumps({"optional": True})),
    ("base", "map_pin", "p1", "quests/main_quest/q001/p1", "p1",
     "Konpeki Plaza", None),
    ("base", "internet_site", "n54", "internet_sites/n54", "N54 News", "", None),
    ("base", "internet_image", "logo", "internet_sites/n54/tower", "logo", "",
     json.dumps({"image": "base\\gui\\n54.inkatlas#logo"})),
    ("base", "tarot", "mq033_death", "tarots/mq033_death", "Death",
     "A card about endings.",
     json.dumps({"index": 13, "image": "base\\gui\\tarot.inkatlas#tarot_deathBIG"})),
]

IMAGE_ROWS = [
    ("base\\gui\\codex.inkatlas#johnny_full", 1860, 609, "webp",
     b"RIFF\x00\x00\x00\x00WEBPjohnny"),
    ("base\\gui\\n54.inkatlas#logo", 200, 80, "webp",
     b"RIFF\x00\x00\x00\x00WEBPlogo"),
]

SUBTITLE_ROWS = [
    ("base", "base\\localization\\en-us\\subtitles\\q101.json", "1",
     "Wake up, samurai. We have a city to burn."),
    ("base", "base\\localization\\en-us\\subtitles\\q101.json", "2",
     "The tower is not what you think."),
]


ALL_ROWS = JOURNAL_ROWS + PHONE_ROWS


def make_dataset(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(build.SCHEMA)
    con.executescript(build.FTS_SCHEMA)
    con.executemany(build.JOURNAL_INSERT, ALL_ROWS)
    con.executemany(
        "INSERT INTO lockeys (loc_key, secondary_key, female_variant, male_variant)"
        " VALUES (?, ?, ?, ?)", LOCKEY_ROWS)
    con.executemany(
        "INSERT INTO subtitles (source, file_path, string_id, line)"
        " VALUES (?, ?, ?, ?)", SUBTITLE_ROWS)
    con.executemany(
        "INSERT INTO images (key, width, height, format, data)"
        " VALUES (?, ?, ?, ?, ?)", IMAGE_ROWS)
    con.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(build.SCHEMA_VERSION),))
    for fts in ("journal_fts", "lockeys_fts", "subtitles_fts"):
        con.execute(f"INSERT INTO {fts}({fts}) VALUES ('rebuild')")
    con.commit()
    con.close()


class DatasetTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "mini.sqlite"
        make_dataset(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.main([str(self.db), *args])
        return rc, out.getvalue()


class SchemaTests(DatasetTestCase):
    def test_cli_and_builder_agree_on_schema_version(self):
        self.assertEqual(cli.REQUIRED_SCHEMA_VERSION, build.SCHEMA_VERSION)

    def test_journal_rows_have_stable_ids(self):
        con = sqlite3.connect(self.db)
        ids = [r[0] for r in con.execute("SELECT id FROM journal ORDER BY id")]
        con.close()
        self.assertEqual(ids, list(range(1, len(ALL_ROWS) + 1)))

    def test_older_dataset_is_rejected_with_a_rebuild_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old.sqlite"
            make_dataset(old)
            con = sqlite3.connect(old)
            con.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
            con.commit()
            con.close()
            err = io.StringIO()
            with redirect_stdout(io.StringIO()):
                sys.stderr, saved = err, sys.stderr
                try:
                    rc = cli.main([str(old), "stats"])
                finally:
                    sys.stderr = saved
            self.assertEqual(rc, 2)
            self.assertIn("rebuild", err.getvalue())


class SearchTests(DatasetTestCase):
    def search(self, *args: str) -> list[dict]:
        rc, out = self.run_cli("search", *args, "--json")
        self.assertEqual(rc, 0)
        return json.loads(out)

    def test_finds_matches_in_every_source(self):
        hits = self.search("tower", "--limit", "20")
        self.assertEqual({h["src"] for h in hits},
                         {"journal", "lockey", "subtitle"})

    def test_results_are_ordered_by_relevance_within_a_source(self):
        hits = self.search("tower", "--limit", "20")
        for src in ("journal", "lockey", "subtitle"):
            ranks = [h["rank"] for h in hits if h["src"] == src]
            self.assertEqual(ranks, sorted(ranks), src)

    def test_no_single_source_crowds_out_the_others(self):
        hits = self.search("tower", "--limit", "3")
        self.assertEqual(len({h["src"] for h in hits}), 3)

    def test_excerpt_marks_the_match(self):
        hits = self.search('"Arasaka tower"', "--limit", "5")
        self.assertTrue(any("[" in h["excerpt"] for h in hits), hits)

    def test_source_filter(self):
        hits = self.search("tower", "--source", "subtitle")
        self.assertEqual({h["src"] for h in hits}, {"subtitle"})

    def test_unknown_source_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.run_cli("search", "tower", "--source", "nope")

    def test_limit_is_respected_across_sources(self):
        self.assertEqual(len(self.search("tower", "--limit", "2")), 2)

    def test_prefix_query_matches(self):
        hits = self.search("mili*", "--limit", "5")
        self.assertTrue(hits)

    def test_no_match_is_not_an_error(self):
        self.assertEqual(self.search("zzzznotpresent"), [])

    def test_bad_expression_reports_cleanly(self):
        rc, _ = self.run_cli("search", "unbalanced(")
        self.assertEqual(rc, 2)


class PageTests(DatasetTestCase):
    def test_prints_full_page_text(self):
        rc, out = self.run_cli("page", "n54/tower")
        self.assertEqual(rc, 0)
        self.assertIn("Arasaka tower stands over Night City.", out)
        self.assertIn("Militech denies involvement", out)

    def test_json_output_carries_the_page(self):
        rc, out = self.run_cli("page", "n54/tower", "--json")
        self.assertEqual(rc, 0)
        rows = json.loads(out)
        self.assertEqual(rows[0]["page_path"], "internet_sites/n54/tower")
        self.assertEqual(rows[0]["site_title"], "N54 News")

    def test_unknown_page_returns_one(self):
        rc, _ = self.run_cli("page", "no-such-page")
        self.assertEqual(rc, 1)


class ShowTests(DatasetTestCase):
    def test_show_by_id(self):
        rc, out = self.run_cli("show", "2")
        self.assertEqual(rc, 0)
        self.assertIn("Arasaka tower stands over Night City.", out)

    def test_show_by_path_includes_descendants(self):
        rc, out = self.run_cli("show", "codex/characters/johnny")
        self.assertEqual(rc, 0)
        self.assertIn("Johnny Silverhand", out)
        self.assertIn("rockerboy engram", out)

    def test_show_by_fragment(self):
        rc, out = self.run_cli("show", "dogtown")
        self.assertEqual(rc, 0)
        self.assertIn("Welcome to Dogtown", out)

    def test_show_json(self):
        rc, out = self.run_cli("show", "codex/characters/johnny", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(len(json.loads(out)), 2)

    def test_show_unknown_returns_one(self):
        rc, _ = self.run_cli("show", "nothing-like-this")
        self.assertEqual(rc, 1)


class TreeTests(DatasetTestCase):
    def test_roots(self):
        rc, out = self.run_cli("tree", "--json")
        self.assertEqual(rc, 0)
        roots = {r["path"]: r["entries"] for r in json.loads(out)}
        self.assertEqual(set(roots),
                         {"internet_sites", "codex", "onscreens", "contacts",
                          "quests", "tarots"})
        self.assertEqual(roots["codex"], 2)

    def test_descends_into_a_prefix(self):
        rc, out = self.run_cli("tree", "codex/characters", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual([r["path"] for r in json.loads(out)],
                         ["codex/characters/johnny"])

    def test_leading_and_trailing_slashes_are_tolerated(self):
        rc, out = self.run_cli("tree", "/codex/", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual([r["path"] for r in json.loads(out)],
                         ["codex/characters"])

    def test_unknown_prefix_returns_one(self):
        rc, _ = self.run_cli("tree", "nope")
        self.assertEqual(rc, 1)


class CuratedViewTests(DatasetTestCase):
    def rows(self, sql: str) -> list[dict]:
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql)]
        finally:
            con.close()

    def test_dev_placeholder_subtitles_are_suppressed(self):
        con = sqlite3.connect(self.db)
        con.execute(
            "UPDATE journal SET title = '!WIP' WHERE kind = 'codex_description'")
        con.commit()
        subtitle = con.execute("SELECT subtitle FROM v_codex").fetchone()[0]
        con.execute("UPDATE journal SET title = '' WHERE kind = 'codex_description'")
        con.commit()
        con.close()
        self.assertIsNone(subtitle)

    def test_codex_descriptions_inherit_their_entry_title(self):
        row = self.rows("SELECT * FROM v_codex")[0]
        self.assertEqual(row["title"], "Johnny Silverhand")
        self.assertIsNone(row["subtitle"])
        self.assertIn("rockerboy engram", row["body"])

    def test_codex_rows_carry_their_entry_picture_keys(self):
        row = self.rows("SELECT * FROM v_codex")[0]
        self.assertEqual(row["image"], "base\\gui\\codex.inkatlas#johnny_full")
        self.assertEqual(row["thumb"], "base\\gui\\codex.inkatlas#johnny")
        picture = self.rows(
            "SELECT width, height, format FROM images WHERE key = ?"
            .replace("?", "'base\\gui\\codex.inkatlas#johnny_full'"))[0]
        self.assertEqual((picture["width"], picture["height"], picture["format"]),
                         (1860, 609, "webp"))

    def test_shards_list_their_page_pictures(self):
        row = self.rows("SELECT images FROM v_shards WHERE page_path LIKE '%tower'")[0]
        self.assertEqual(row["images"], "base\\gui\\n54.inkatlas#logo")

    def test_tarots_and_contacts_expose_picture_keys(self):
        tarot = self.rows("SELECT image FROM v_tarots")[0]
        self.assertEqual(tarot["image"], "base\\gui\\tarot.inkatlas#tarot_deathBIG")
        contact = self.rows("SELECT avatar FROM v_contacts")[0]
        self.assertIsNone(contact["avatar"])

    def test_emails_expose_sender_and_addressee(self):
        row = self.rows("SELECT * FROM v_emails")[0]
        self.assertEqual(row["subject"], "Welcome to Dogtown")
        self.assertEqual(row["sender"], "Mr. Hands")

    def test_contacts_carry_names_and_message_counts(self):
        row = self.rows("SELECT * FROM v_contacts")[0]
        self.assertEqual(row["name"], "Zane")
        self.assertEqual(row["contact_type"], "Friend")
        self.assertEqual(row["message_count"], 1)

    def test_phone_lines_are_attributed_to_contact_and_thread(self):
        rows = self.rows("SELECT * FROM v_phone ORDER BY id")
        self.assertEqual([r["line_type"] for r in rows], ["message", "choice"])
        self.assertEqual({r["contact"] for r in rows}, {"Zane"})
        self.assertEqual(rows[0]["conversation"], "About the gig")
        self.assertEqual(rows[0]["quest_important"], 1)

    def test_quests_carry_metadata_and_description(self):
        row = self.rows("SELECT * FROM v_quests")[0]
        self.assertEqual(row["title"], "The Heist")
        self.assertEqual(row["quest_type"], "MainQuest")
        self.assertEqual(row["district"], "Districts.Watson")
        self.assertEqual(row["content_assignment"],
                         "DeviceContentAssignment.q001")
        self.assertIn("Konpeki", row["description"])
        self.assertEqual(row["objective_count"], 1)

    def test_objectives_name_their_quest(self):
        row = self.rows("SELECT * FROM v_objectives")[0]
        self.assertEqual(row["quest"], "The Heist")
        self.assertEqual(row["optional"], 1)

    def test_map_pins_expose_captions(self):
        self.assertEqual([r["caption"] for r in self.rows("SELECT * FROM v_map_pins")],
                         ["Konpeki Plaza"])

    def test_shards_name_their_site(self):
        row = self.rows("SELECT * FROM v_shards")[0]
        self.assertEqual(row["site_title"], "N54 News")
        self.assertEqual(row["text_count"], 2)


class ViewTests(DatasetTestCase):
    def test_dialogue_view_extracts_the_scene_name(self):
        con = sqlite3.connect(self.db)
        scenes = {r[0] for r in con.execute("SELECT scene FROM v_dialogue")}
        con.close()
        self.assertEqual(scenes, {"q101"})

    def test_stats_json_separates_tables_and_views(self):
        rc, out = self.run_cli("stats", "--json")
        self.assertEqual(rc, 0)
        stats = json.loads(out)
        self.assertEqual(stats["tables"]["journal"], len(ALL_ROWS))
        self.assertIn("v_dialogue", stats["views"])

    def test_shard_view_concatenates_page_text(self):
        con = sqlite3.connect(self.db)
        body = con.execute(
            "SELECT body FROM v_shards WHERE page_path LIKE '%tower'"
        ).fetchone()[0]
        con.close()
        self.assertIn("Arasaka tower", body)
        self.assertIn("Militech", body)

    def test_stats_lists_tables_and_views(self):
        rc, out = self.run_cli("stats")
        self.assertEqual(rc, 0)
        self.assertIn("journal", out)
        self.assertIn("v_shards", out)
        self.assertIn("images", out)


class ReaderProfileSearchTests(DatasetTestCase):
    """A reader-profile dataset has no lockeys; search must still work."""

    def setUp(self):
        self.reader = Path(self.tmp.name) / "reader.sqlite"
        if not self.reader.exists():
            make_dataset(self.reader)
            con = sqlite3.connect(self.reader)
            con.execute("DROP TABLE lockeys_fts")
            con.execute("DROP TABLE lockeys")
            con.commit()
            con.close()

    def run_reader(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.main([str(self.reader), *args])
        return rc, out.getvalue()

    def test_default_sources_skip_the_missing_index(self):
        rc, out = self.run_reader("search", "tower", "--json")
        self.assertEqual(rc, 0)
        hits = json.loads(out)
        self.assertTrue(hits)
        self.assertNotIn("lockey", {h["src"] for h in hits})

    def test_asking_for_the_missing_index_by_name_is_an_error(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_reader("search", "tower", "--source", "lockey")
        self.assertIn("no lockey index", str(cm.exception))


class ImageCommandTests(DatasetTestCase):
    def test_writes_a_picture_by_key(self):
        out = Path(self.tmp.name) / "johnny.webp"
        rc, text = self.run_cli("image", "base\\gui\\codex.inkatlas#johnny_full", str(out))
        self.assertEqual(rc, 0)
        self.assertIn("1860x609 webp", text)
        self.assertTrue(out.read_bytes().startswith(b"RIFF"))

    def test_resolves_a_journal_entry_to_its_picture(self):
        out = Path(self.tmp.name) / "entry.webp"
        rc, _ = self.run_cli("image", "codex/characters/johnny", str(out))
        self.assertEqual(rc, 0)
        self.assertTrue(out.read_bytes().endswith(b"johnny"))

    def test_entry_without_a_picture_returns_one(self):
        out = Path(self.tmp.name) / "none.webp"
        rc, _ = self.run_cli("image", "quests/main_quest/q001", str(out))
        self.assertEqual(rc, 1)
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
