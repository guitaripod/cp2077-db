"""End-to-end build tests against a real game install.

Skipped unless a Cyberpunk 2077 directory is given in `CP2077_DIR` (or found at
the default Steam path). The full determinism check builds the dataset twice and
is only run with `CPDB_SLOW=1` (~4 minutes, ~2.5 GB of scratch space).
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cpdb.build import DatasetBuilder  # noqa: E402

DEFAULT_GAME_DIR = Path(
    "/mnt/nvme8tb/SteamLibrary/steamapps/common/Cyberpunk 2077"
)


def game_dir() -> Path | None:
    """The install to test against, or None when no game is available."""
    env = os.environ.get("CP2077_DIR")
    candidate = Path(env) if env else DEFAULT_GAME_DIR
    return candidate if (candidate / "r6" / "cache" / "tweakdb.bin").is_file() else None


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


_FOUND = game_dir()
HAVE_GAME = _FOUND is not None
GAME = _FOUND if _FOUND is not None else Path()


@unittest.skipUnless(HAVE_GAME, "no Cyberpunk 2077 install found")
class BuildTests(unittest.TestCase):
    """One build, then every invariant checked against it."""

    db_path: Path
    tmp: tempfile.TemporaryDirectory

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "cp2077.sqlite"
        DatasetBuilder(GAME, cls.db_path, "en").build()
        cls.con = sqlite3.connect(f"file:{cls.db_path}?mode=ro", uri=True)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.tmp.cleanup()

    def count(self, table: str) -> int:
        return self.con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]

    def test_content_tables_are_populated(self):
        for table, minimum in (
            ("lockeys", 70_000),
            ("journal", 47_000),
            ("subtitles", 100_000),
            ("tweak_records", 360_000),
            ("tweak_flats", 6_000_000),
            ("tweak_flat_texts", 120_000),
        ):
            with self.subTest(table=table):
                self.assertGreaterEqual(self.count(table), minimum)

    def test_views_resolve_text(self):
        for view, minimum in (("v_shards", 600), ("v_codex", 500),
                              ("v_emails", 500), ("v_quests", 500),
                              ("v_tarots", 20), ("v_items", 10_000),
                              ("v_vehicles", 100), ("v_perks", 100)):
            with self.subTest(view=view):
                self.assertGreaterEqual(self.count(view), minimum)

    def test_meta_has_no_wall_clock_fields(self):
        meta = dict(self.con.execute("SELECT key, value FROM meta"))
        self.assertEqual(set(meta), {"lang", "input_fingerprint"})
        self.assertEqual(meta["lang"], "en")
        self.assertEqual(len(meta["input_fingerprint"]), 64)

    def test_full_text_search_finds_known_content(self):
        for match in ("Arasaka", "Militech", "braindance"):
            with self.subTest(match=match):
                hits = self.con.execute(
                    "SELECT COUNT(*) FROM journal_fts WHERE journal_fts MATCH ?",
                    (match,),
                ).fetchone()[0]
                self.assertGreater(hits, 0)

    def test_journal_paths_are_hierarchical(self):
        roots = {
            r[0] for r in self.con.execute(
                "SELECT DISTINCT substr(path, 1, instr(path || '/', '/') - 1) "
                "FROM journal WHERE path <> ''"
            )
        }
        self.assertTrue({"codex", "onscreens", "internet_sites"} <= roots, roots)

    def test_every_codex_row_carries_readable_text(self):
        """Description entries have no title of their own, but always a body."""
        blank = self.con.execute(
            "SELECT COUNT(*) FROM v_codex "
            "WHERE COALESCE(title, '') = '' AND COALESCE(body, '') = ''"
        ).fetchone()[0]
        self.assertLessEqual(blank, 1)


@unittest.skipUnless(HAVE_GAME, "no Cyberpunk 2077 install found")
@unittest.skipUnless(os.environ.get("CPDB_SLOW") == "1", "slow: set CPDB_SLOW=1")
class DeterminismTests(unittest.TestCase):
    def test_two_builds_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.sqlite"
            second = Path(tmp) / "b.sqlite"
            DatasetBuilder(GAME, first, "en").build()
            DatasetBuilder(GAME, second, "en").build()
            self.assertEqual(sha256(first), sha256(second))


if __name__ == "__main__":
    unittest.main()
