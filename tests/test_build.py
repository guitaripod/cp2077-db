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
        for view, minimum in (("v_shards", 600), ("v_codex", 400),
                              ("v_codex_tree", 900), ("v_emails", 500),
                              ("v_contacts", 150), ("v_phone", 4_000),
                              ("v_quests", 350), ("v_objectives", 4_000),
                              ("v_quest_tree", 6_000), ("v_map_pins", 1_000),
                              ("v_tarots", 20), ("v_dialogue", 100_000),
                              ("v_items", 10_000), ("v_flat_refs", 500_000),
                              ("v_vehicles", 100), ("v_perks", 100)):
            with self.subTest(view=view):
                self.assertGreaterEqual(self.count(view), minimum)

    def test_meta_has_no_wall_clock_fields(self):
        meta = dict(self.con.execute("SELECT key, value FROM meta"))
        self.assertEqual(set(meta),
                         {"schema_version", "lang", "images", "input_fingerprint"})
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

    def test_dialogue_scenes_are_file_names_not_paths(self):
        scenes = [r[0] for r in self.con.execute(
            "SELECT DISTINCT scene FROM v_dialogue LIMIT 50")]
        self.assertTrue(scenes)
        for scene in scenes:
            self.assertNotIn("\\", scene)
            self.assertFalse(scene.endswith(".json"))

    def test_shard_bodies_use_real_newlines(self):
        body = self.con.execute(
            "SELECT body FROM v_shards WHERE length(body) > 200 LIMIT 1"
        ).fetchone()[0]
        self.assertIn("\n", body)
        self.assertNotIn("\\n", body)

    def test_journal_paths_are_hierarchical(self):
        roots = {
            r[0] for r in self.con.execute(
                "SELECT DISTINCT substr(path, 1, instr(path || '/', '/') - 1) "
                "FROM journal WHERE path <> ''"
            )
        }
        self.assertTrue({"codex", "onscreens", "internet_sites"} <= roots, roots)

    def test_journal_entries_reference_pictures_even_without_images(self):
        keyed = self.con.execute(
            "SELECT COUNT(*) FROM journal WHERE kind = 'codex_entry' "
            "AND json_extract(extra, '$.image') LIKE '%.inkatlas#%'").fetchone()[0]
        self.assertGreater(keyed, 400)
        tarots = self.con.execute(
            "SELECT COUNT(*) FROM v_tarots WHERE image LIKE '%BIG'").fetchone()[0]
        self.assertEqual(tarots, 28)
        avatars = self.con.execute(
            "SELECT COUNT(*) FROM v_contacts WHERE avatar IS NOT NULL").fetchone()[0]
        self.assertGreater(avatars, 100)
        self.assertEqual(self.count("images"), 0)
        self.assertEqual(self.con.execute(
            "SELECT value FROM meta WHERE key = 'images'").fetchone()[0], "0")

    def test_every_codex_row_carries_readable_text(self):
        """Description entries have no title of their own, but always a body."""
        blank = self.con.execute(
            "SELECT COUNT(*) FROM v_codex "
            "WHERE COALESCE(title, '') = '' AND COALESCE(body, '') = ''"
        ).fetchone()[0]
        self.assertLessEqual(blank, 1)


def have_pillow() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(HAVE_GAME, "no Cyberpunk 2077 install found")
@unittest.skipUnless(os.environ.get("CPDB_SLOW") == "1", "slow: set CPDB_SLOW=1")
@unittest.skipUnless(have_pillow(), "Pillow not installed")
class ImageBuildTests(unittest.TestCase):
    """One build with images, then the picture table checked against it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "cp2077.sqlite"
        DatasetBuilder(GAME, cls.db_path, "en", images=True).build()
        cls.con = sqlite3.connect(f"file:{cls.db_path}?mode=ro", uri=True)

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.tmp.cleanup()

    def test_every_referenced_key_has_a_picture(self):
        for field in ("image", "thumb", "avatar"):
            missing = self.con.execute(
                f"SELECT COUNT(*) FROM journal j WHERE json_extract(j.extra, '$.{field}')"
                f" IS NOT NULL AND NOT EXISTS (SELECT 1 FROM images i"
                f" WHERE i.key = json_extract(j.extra, '$.{field}'))").fetchone()[0]
            self.assertLessEqual(missing, 2, field)

    def test_pictures_are_webp_at_native_size(self):
        rows = self.con.execute(
            "SELECT i.width, i.height, i.format, substr(i.data, 1, 4), substr(i.data, 9, 4)"
            " FROM images i JOIN v_codex c ON c.image = i.key").fetchall()
        self.assertGreater(len(rows), 400)
        for width, height, fmt, riff, webp in rows:
            self.assertEqual((fmt, riff, webp), ("webp", b"RIFF", b"WEBP"))
            self.assertGreaterEqual(width, 640)
            self.assertGreaterEqual(height, 360)

    def test_tarot_cards_are_portrait(self):
        rows = self.con.execute(
            "SELECT i.width, i.height FROM images i JOIN v_tarots t ON t.image = i.key").fetchall()
        self.assertEqual(len(rows), 28)
        for width, height in rows:
            self.assertGreater(height, width * 1.8)

    def test_meta_records_images(self):
        self.assertEqual(self.con.execute(
            "SELECT value FROM meta WHERE key = 'images'").fetchone()[0], "1")


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
