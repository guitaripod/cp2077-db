"""Regression tests for the cp2077-db extraction code.

Everything here runs without a game install: the parsers are exercised with
synthetic buffers built to the same on-disk layouts. The end-to-end build is
covered separately by `test_build.py`, which skips unless a game is present.
"""
from __future__ import annotations

import hashlib
import io
import sqlite3
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cpdb import cli, cr2w  # noqa: E402
from cpdb.build import (  # noqa: E402
    GAME_LANGS,
    BuildError,
    DatasetBuilder,
    file_identity,
    read_vlq,
)
from cpdb.cli import main  # noqa: E402
from cpdb.engine import (  # noqa: E402
    Archive,
    ArchiveError,
    fnv1a64,
    kark_decompress,
)
from cpdb.textures import dds_wrap, image_key, tarot_part  # noqa: E402
from cpdb.tweakdb import TweakDBError, murmur3, parse as parse_tweakdb, tweakdbid_hash  # noqa: E402


class HashTests(unittest.TestCase):
    def test_fnv1a64_reference_vectors(self):
        self.assertEqual(fnv1a64(b""), 0xCBF29CE484222325)
        self.assertEqual(fnv1a64(b"a"), 0xAF63DC4C8601EC8C)
        self.assertEqual(fnv1a64(b"foobar"), 0x85944171F73967E8)

    def test_fnv1a64_matches_cr2w_copy(self):
        for s in (b"", b"base\\journal\\cooked_journal.journal", b"\xff\x00"):
            self.assertEqual(fnv1a64(s), cr2w.fnv1a64(s))

    def test_murmur3_reference_vectors(self):
        self.assertEqual(murmur3(b"", 0), 0)
        self.assertEqual(murmur3(b"hello", 0), 0x248BFA47)

    def test_tweakdbid_is_crc32_plus_length(self):
        name = "Items.Preset_Yukimura_Default"
        raw = name.encode()
        self.assertEqual(
            tweakdbid_hash(name), (zlib.crc32(raw) & 0xFFFFFFFF) + (len(raw) << 32)
        )


class VlqTests(unittest.TestCase):
    def test_single_byte_values(self):
        self.assertEqual(read_vlq(bytes([0x00]), 0), (0, 1))
        self.assertEqual(read_vlq(bytes([0x3F]), 0), (63, 1))

    def test_negative_flag(self):
        self.assertEqual(read_vlq(bytes([0x80 | 0x05]), 0), (-5, 1))

    def test_continuation(self):
        self.assertEqual(read_vlq(bytes([0x40 | 0x01, 0x02]), 0), (1 | (2 << 6), 2))

    def test_reads_from_offset(self):
        self.assertEqual(read_vlq(b"\xff\xff\x07", 2), (7, 3))


def _cr2w_file(names: list[str], imports: list[str]) -> cr2w.CR2WFile:
    f = cr2w.CR2WFile()
    f.names = names
    f.imports = imports
    return f


class ResourceRefTests(unittest.TestCase):
    """raRef values are u16 ONE-based indices into the imports table (0 = null).

    Reading them as u64 misaligned every following variable in the chunk; a
    zero-based lookup silently shifted every resolved path by one entry.
    """

    def setUp(self):
        self.f = _cr2w_file(["None"], ["base\\a.xbm", "base\\b.xbm"])

    def read(self, raw: bytes, red_type: str = "raRef:CResource"):
        return cr2w._read_value(cr2w._Reader(raw), self.f, red_type, 2)

    def test_zero_is_null(self):
        self.assertIsNone(self.read(struct.pack("<H", 0)))

    def test_one_based_lookup(self):
        self.assertEqual(self.read(struct.pack("<H", 1)), "base\\a.xbm")
        self.assertEqual(self.read(struct.pack("<H", 2)), "base\\b.xbm")

    def test_out_of_range_is_null(self):
        self.assertIsNone(self.read(struct.pack("<H", 3)))

    def test_consumes_exactly_two_bytes(self):
        r = cr2w._Reader(struct.pack("<HH", 1, 0x4142))
        cr2w._read_value(r, self.f, "CResourceAsyncReference", 2)
        self.assertEqual(r.pos, 2)


def _variable(name_idx: int, type_idx: int, payload: bytes) -> bytes:
    return struct.pack("<HHI", name_idx, type_idx, len(payload) + 4) + payload


class StaticArrayAndBufferTests(unittest.TestCase):
    """`[n]T` static arrays and DataBuffer references, as atlases and
    textures use them: a u32 count then n elements; a u16 buffer index."""

    def setUp(self):
        self.f = _cr2w_file(["None", "Uint32", "Left", "Float", "RectF"], [])

    def read(self, raw: bytes, red_type: str):
        return cr2w._read_value(cr2w._Reader(raw), self.f, red_type, len(raw))

    def test_static_array_reads_declared_count(self):
        raw = struct.pack("<IIII", 3, 7, 8, 9)
        self.assertEqual(self.read(raw, "[3]Uint32"), [7, 8, 9])

    def test_data_buffer_is_a_one_based_index(self):
        self.assertEqual(self.read(struct.pack("<H", 1), "DataBuffer"), {"$buffer": 1})
        self.assertEqual(self.read(struct.pack("<H", 0), "serializationDeferredDataBuffer"), {"$buffer": 0})

    def test_uppercase_struct_is_read_as_nested_class(self):
        body = b"\x00" + _variable(2, 3, struct.pack("<f", 0.25)) + struct.pack("<H", 0)
        self.assertEqual(self.read(body, "RectF"), {"$type": "RectF", "Left": 0.25})


class UnknownTypeTests(unittest.TestCase):
    """Unknown/unsupported types skip by declared size, like WolvenKit."""

    def setUp(self):
        self.f = _cr2w_file(["None", "first", "Uint32", "second", "SomeUnknownType"], [])

    def test_unknown_type_does_not_desync_following_variables(self):
        body = (
            b"\x00"
            + _variable(3, 4, b"\xde\xad\xbe\xef\x01\x02")
            + _variable(1, 2, struct.pack("<I", 4242))
            + struct.pack("<H", 0)
        )
        r = cr2w._Reader(body)
        obj = cr2w._read_object(r, self.f, "testClass", len(body))
        self.assertIsNone(obj["second"])
        self.assertEqual(obj["first"], 4242)
        self.assertEqual(self.f.unread_variables,
                         ["testClass.second (SomeUnknownType)"])

    def test_none_type_is_skipped_not_read_as_u16(self):
        body = (
            b"\x00"
            + _variable(3, 0, b"\x01\x02\x03\x04")
            + _variable(1, 2, struct.pack("<I", 7))
            + struct.pack("<H", 0)
        )
        obj = cr2w._read_object(cr2w._Reader(body), self.f, "testClass", len(body))
        self.assertIsNone(obj["second"])
        self.assertEqual(obj["first"], 7)

    def test_implausible_array_count_is_rejected(self):
        raw = struct.pack("<I", 50_000_000)
        with self.assertRaises(ValueError):
            cr2w._read_value(cr2w._Reader(raw), self.f, "array:Uint32", 4)


class Cr2wHeaderTests(unittest.TestCase):
    def test_rejects_non_cr2w(self):
        with self.assertRaises(cr2w.CR2WError):
            cr2w.read_cr2w(b"NOPE" + b"\x00" * 200)

    def test_rejects_unsupported_version(self):
        raw = struct.pack("<II", cr2w.CR2W_MAGIC, 1) + b"\x00" * 200
        with self.assertRaises(cr2w.CR2WError):
            cr2w.read_cr2w(raw)

    def test_short_read_raises_cr2w_error(self):
        r = cr2w._Reader(b"\x01")
        with self.assertRaises(cr2w.CR2WError):
            r.u32()


def _rdar(index: bytes, version: int = 12, index_size: int | None = None) -> bytes:
    body_offset = 40
    header = struct.pack(
        "<IIQII", 0x52414452, version, body_offset,
        len(index) if index_size is None else index_size, 0,
    )
    header += b"\x00" * (40 - len(header))
    return header + index


def _index(num_files: int = 0, num_segments: int = 0, num_deps: int = 0,
           tail: bytes = b"") -> bytes:
    return struct.pack("<IIQIII", 0, 0, 0, num_files, num_segments, num_deps) + tail


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data: bytes) -> Path:
        p = self.dir / "test.archive"
        p.write_bytes(data)
        return p

    def assert_archive_error(self, data: bytes, needle: str):
        with self.assertRaises(ArchiveError) as cm:
            Archive(self.write(data))
        self.assertIn(needle, str(cm.exception))

    def test_junk_file(self):
        self.assert_archive_error(b"not an archive at all" * 8, "not a RDAR")

    def test_truncated_header(self):
        self.assert_archive_error(b"RDAR\x0c", "truncated header")

    def test_unsupported_version(self):
        self.assert_archive_error(_rdar(_index(), version=99),
                                  "unsupported archive version")

    def test_index_beyond_file(self):
        self.assert_archive_error(_rdar(_index(), index_size=1 << 20), "beyond file size")

    def test_index_tables_exceed_index(self):
        self.assert_archive_error(_rdar(_index(num_files=1000)), "exceed index size")

    def test_empty_archive_opens(self):
        with Archive(self.write(_rdar(_index()))) as ar:
            self.assertEqual(ar.files, {})

    def test_missing_entry_raises(self):
        with Archive(self.write(_rdar(_index()))) as ar:
            with self.assertRaises(ArchiveError):
                ar.read_entry(1234)

    def test_failed_open_does_not_leak_handle(self):
        path = self.write(b"RDAR" + b"\x00" * 100)
        with self.assertRaises(ArchiveError):
            Archive(path)
        self.assertEqual(_open_handles(path), 0)

    def test_duplicate_name_hash_keeps_first_entry(self):
        entry = struct.pack("<QQIIIII", 7, 0, 0, 0, 1, 0, 0) + b"\x11" * 20
        dupe = struct.pack("<QQIIIII", 7, 0, 0, 1, 2, 0, 0) + b"\x22" * 20
        segments = struct.pack("<QII", 0, 0, 0) * 2
        data = _rdar(_index(num_files=2, num_segments=2, tail=entry + dupe + segments))
        with Archive(self.write(data)) as ar:
            self.assertEqual(ar.files[7].sha1, b"\x11" * 20)

    def test_segment_range_validated(self):
        entry = struct.pack("<QQIIIII", 9, 0, 0, 0, 5, 0, 0) + b"\x00" * 20
        data = _rdar(_index(num_files=1, tail=entry))
        with Archive(self.write(data)) as ar:
            with self.assertRaises(ArchiveError) as cm:
                ar.read_entry(9)
            self.assertIn("out-of-range", str(cm.exception))

    def test_kark_passthrough_without_magic(self):
        self.assertEqual(kark_decompress(b"plain bytes"), b"plain bytes")

    def test_multi_segment_entry_returns_buffers_and_verifies_sha1(self):
        main, buffer = b"main-cr2w-bytes", b"texture-payload"
        entry_size = 56
        index = _index(num_files=1, num_segments=2, tail=b"")
        data_start = 40 + len(index) + entry_size + 2 * 16
        segments = (struct.pack("<QII", data_start, len(main), len(main))
                    + struct.pack("<QII", data_start + len(main), len(buffer), len(buffer)))
        sha1 = hashlib.sha1(main + buffer).digest()
        entry = struct.pack("<QQIIIII", 5, 0, 1, 0, 2, 0, 0) + sha1
        data = _rdar(index + entry + segments) + main + buffer
        with Archive(self.write(data)) as ar:
            self.assertEqual(ar.read_entry_with_buffers(5), (main, [buffer]))
            self.assertEqual(ar.read_entry(5), main)

    def test_multi_segment_sha1_mismatch_is_reported(self):
        main, buffer = b"main", b"buf"
        index = _index(num_files=1, num_segments=2)
        data_start = 40 + len(index) + 56 + 2 * 16
        segments = (struct.pack("<QII", data_start, 4, 4)
                    + struct.pack("<QII", data_start + 4, 3, 3))
        entry = struct.pack("<QQIIIII", 5, 0, 1, 0, 2, 0, 0) + b"\x00" * 20
        data = _rdar(index + entry + segments) + main + buffer
        with Archive(self.write(data)) as ar:
            with self.assertRaises(ArchiveError) as cm:
                ar.read_entry_with_buffers(5)
            self.assertIn("SHA1", str(cm.exception))


class TextureTests(unittest.TestCase):
    def test_dds_header_is_128_bytes_for_dxt(self):
        wrapped = dds_wrap("TCM_DXTAlpha", 8, 4, b"\x00" * 32)
        self.assertEqual(wrapped[:4], b"DDS ")
        self.assertEqual(len(wrapped), 128 + 32)
        self.assertEqual(struct.unpack_from("<II", wrapped, 12), (4, 8))
        self.assertEqual(wrapped[84:88], b"DXT5")

    def test_bc7_gets_a_dx10_extension(self):
        wrapped = dds_wrap("TCM_QualityColor", 4, 4, b"\x00" * 16)
        self.assertEqual(len(wrapped), 148 + 16)
        self.assertEqual(wrapped[84:88], b"DX10")
        self.assertEqual(struct.unpack_from("<I", wrapped, 128)[0], 99)

    def test_unknown_compression_is_rejected(self):
        from cpdb.textures import TextureError
        with self.assertRaises(TextureError):
            dds_wrap("TCM_Whatever", 4, 4, b"")

    def test_image_key_joins_atlas_and_part(self):
        self.assertEqual(image_key("base\\a.inkatlas", "p"), "base\\a.inkatlas#p")

    def test_tarot_names_map_onto_big_atlas_parts(self):
        parts = {"tarot_deathBIG", "tarot_highpriestessBIG", "tarot_judgementBIG",
                 "tarot_wheeloffortuneBIG", "tarot_king_of_cupsBIG",
                 "tarot_fool01BIG", "tarot_fool02BIG", "tarot_fool03BIG"}
        cases = {
            ("TarotCard_Death", "mq033_death"): "tarot_deathBIG",
            ("TarotCard_HighPristess", "mq033_the_high_priestess"): "tarot_highpriestessBIG",
            ("TarotCard_Judgment", "mq033_judgement"): "tarot_judgementBIG",
            ("TarotCard_WheelOfForutune", "mq033_the_wheel_of_fortune"): "tarot_wheeloffortuneBIG",
            ("TarotCard_KingOfCups", "mq033_ep1_king_of_the_cups"): "tarot_king_of_cupsBIG",
            ("TarotCard_Fool", "mq033_the_fool"): "tarot_fool01BIG",
            ("TarotCard_Fool", "mq033_the_fool1"): "tarot_fool02BIG",
            ("TarotCard_Fool", "mq033_the_fool2"): "tarot_fool03BIG",
            ("TarotCard_Nope", "x"): None,
        }
        for (part, entry), expected in cases.items():
            self.assertEqual(tarot_part(part, entry, parts), expected, part)

    def test_encode_drops_unused_alpha(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow not installed")
        from cpdb.textures import encode
        opaque, w, h = encode(Image.new("RGBA", (16, 8), (200, 30, 30, 255)))
        self.assertEqual((w, h), (16, 8))
        self.assertEqual(opaque[8:12], b"WEBP")
        self.assertEqual(Image.open(io.BytesIO(opaque)).mode, "RGB")
        translucent, _, _ = encode(Image.new("RGBA", (16, 8), (200, 30, 30, 90)))
        self.assertEqual(Image.open(io.BytesIO(translucent)).mode, "RGBA")


def _open_handles(path: Path) -> int:
    fd_dir = Path("/proc/self/fd")
    if not fd_dir.is_dir():
        return 0
    count = 0
    for fd in fd_dir.iterdir():
        try:
            if fd.resolve() == path.resolve():
                count += 1
        except OSError:
            pass
    return count


class TweakDbTests(unittest.TestCase):
    def test_rejects_non_blob(self):
        with self.assertRaises(TweakDBError):
            parse_tweakdb(b"\x00" * 64)

    def test_rejects_offsets_outside_blob(self):
        raw = struct.pack("<IIII", 0x0BB1DEE5, 5, 1, 0) + struct.pack(
            "<iiii", 32, 1 << 30, 1 << 30, 1 << 30
        )
        with self.assertRaises(TweakDBError):
            parse_tweakdb(raw + b"\x00" * 64)


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unknown_language_rejected(self):
        with self.assertRaises(BuildError) as cm:
            DatasetBuilder(self.dir, self.dir / "out.sqlite", "e")
        self.assertIn("unknown language", str(cm.exception))

    def test_known_languages_include_en(self):
        self.assertIn("en", GAME_LANGS)

    def test_missing_game_files_fail_before_any_work(self):
        out = self.dir / "out.sqlite"
        with self.assertRaises(BuildError) as cm:
            DatasetBuilder(self.dir, out, "en")
        self.assertIn("missing required game files", str(cm.exception))
        self.assertFalse(out.exists())


class FileIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name: str, data: bytes) -> Path:
        p = self.dir / name
        p.write_bytes(data)
        return p

    def test_same_content_same_identity(self):
        a = self.write("a.bin", b"the same bytes" * 100)
        b = self.write("b.bin", b"the same bytes" * 100)
        self.assertEqual(file_identity(a), file_identity(b))

    def test_changed_content_changes_identity(self):
        a = self.write("a.bin", b"x" * 1000)
        b = self.write("b.bin", b"x" * 999 + b"y")
        self.assertNotEqual(file_identity(a), file_identity(b))

    def test_empty_file_is_hashable(self):
        self.assertEqual(len(file_identity(self.write("e.bin", b""))), 32)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "tiny.sqlite"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO meta VALUES ('schema_version', ?)",
                    (str(cli.REQUIRED_SCHEMA_VERSION),))
        con.execute("CREATE TABLE journal (title TEXT)")
        con.execute("INSERT INTO journal VALUES ('Arasaka tower')")
        con.commit()
        con.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_subcommand_is_routed_without_a_database(self):
        rc = main(["build", str(self.dir / "not-a-game")])
        self.assertEqual(rc, 2)

    def test_build_rejects_unknown_language(self):
        with self.assertRaises(SystemExit) as cm:
            main(["build", str(self.dir), "--lang", "e"])
        self.assertEqual(cm.exception.code, 2)

    def test_missing_database_reports_cleanly(self):
        self.assertEqual(main([str(self.dir / "absent.sqlite"), "stats"]), 2)

    def test_bad_sql_reports_cleanly(self):
        self.assertEqual(main([str(self.db), "sql", "SELECT * FROM nope"]), 2)

    def test_bad_fts_expression_reports_cleanly(self):
        self.assertEqual(main([str(self.db), "search", "unbalanced("]), 2)

    def test_query_runs(self):
        self.assertEqual(main([str(self.db), "sql", "SELECT * FROM journal"]), 0)


if __name__ == "__main__":
    unittest.main()
