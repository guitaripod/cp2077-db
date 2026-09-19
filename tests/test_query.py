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
     "Johnny Silverhand", "", None),
    ("base", "codex_description", "c2", "codex/characters/johnny",
     "", "A rockerboy engram riding in V's head.", None),
    ("ep1", "email", "e1", "onscreens/emails/quests/dogtown",
     "Welcome to Dogtown", "Barghest runs the place now.",
     json.dumps({"sender": "Mr. Hands"})),
]

LOCKEY_ROWS = [
    ("LocKey#1", "ui-tower-label", "Arasaka Tower", "Arasaka Tower"),
    ("LocKey#2", "ui-militech-label", "Militech", "Militech"),
]

SUBTITLE_ROWS = [
    ("base", "base\\localization\\en-us\\subtitles\\q101.json", "1",
     "Wake up, samurai. We have a city to burn."),
    ("base", "base\\localization\\en-us\\subtitles\\q101.json", "2",
     "The tower is not what you think."),
]


def make_dataset(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(build.SCHEMA)
    con.executescript(build.FTS_SCHEMA)
    con.executemany(build.JOURNAL_INSERT, JOURNAL_ROWS)
    con.executemany(
        "INSERT INTO lockeys (loc_key, secondary_key, female_variant, male_variant)"
        " VALUES (?, ?, ?, ?)", LOCKEY_ROWS)
    con.executemany(
        "INSERT INTO subtitles (source, file_path, string_id, line)"
        " VALUES (?, ?, ?, ?)", SUBTITLE_ROWS)
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
        self.assertEqual(ids, list(range(1, len(JOURNAL_ROWS) + 1)))

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

    def test_unknown_page_returns_one(self):
        rc, _ = self.run_cli("page", "no-such-page")
        self.assertEqual(rc, 1)


class TreeTests(DatasetTestCase):
    def test_roots(self):
        rc, out = self.run_cli("tree", "--json")
        self.assertEqual(rc, 0)
        roots = {r["path"]: r["entries"] for r in json.loads(out)}
        self.assertEqual(set(roots), {"internet_sites", "codex", "onscreens"})
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
        self.assertEqual(stats["tables"]["journal"], len(JOURNAL_ROWS))
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


if __name__ == "__main__":
    unittest.main()
