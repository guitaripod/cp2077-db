#!/usr/bin/env python3
"""Extraction engine for cp2077-db.

Parses REDengine RDAR .archive containers (v11/v12) and extracts entries,
reimplementing WolvenKit's C# readers (WolvenKit.RED4/Archive/IO/ArchiveReader.cs,
GPL-3.0) in stdlib Python. Decompression goes through libkraken.so, WolvenKit's
Linux Oodle Kraken binding, vendored under lib/.
"""
from __future__ import annotations

import ctypes
import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path

KRAKEN_LIB = str(Path(__file__).resolve().parent / "lib" / "libkraken.so")
_kraken = ctypes.CDLL(KRAKEN_LIB)
_kraken.Kraken_Decompress.restype = ctypes.c_int
_kraken.Kraken_Decompress.argtypes = [
    ctypes.c_char_p, ctypes.c_long, ctypes.c_char_p, ctypes.c_long,
]

FNV_BASIS = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3

RDAR_MAGIC = 0x52414452  # little-endian u32 of the ASCII bytes "RDAR"
KARK_MAGIC = b"KARK"

HEADER_SIZE = 40
INDEX_HEADER_SIZE = 28
FILE_ENTRY_SIZE = 56
FILE_SEGMENT_SIZE = 16
DEPENDENCY_SIZE = 8


def fnv1a64(data: bytes) -> int:
    """FNV1A64 over ASCII/UTF-8 bytes, matching ResourcePath.CalculateHash."""
    h = FNV_BASIS
    for b in data:
        h = ((h ^ b) * FNV_PRIME) & 0xFFFFFFFFFFFFFFFF
    return h


def kraken_decompress(src: bytes, uncompressed_size: int) -> bytes:
    """Oodle Kraken decompress via libkraken.so (powzix/oo2ext-derived).

    `src` is copied into a C-allocated buffer because the lib may hold a pointer
    into it past the call; the output buffer is deliberately over-allocated by
    64 bytes (the lib writes quantum padding beyond dst_len).
    """
    src_buf = ctypes.create_string_buffer(src, len(src))
    out_buf = ctypes.create_string_buffer(uncompressed_size + 64)
    n = _kraken.Kraken_Decompress(src_buf, len(src), out_buf, uncompressed_size)
    if n != uncompressed_size:
        raise ValueError(
            f"kraken decompress returned {n}, expected {uncompressed_size}"
        )
    return out_buf.raw[:n]


def kark_decompress(buf: bytes) -> bytes:
    """Strip a KARK (Oodle) wrapper: magic u32 + uncompressed size u32 + payload."""
    if buf[:4] != KARK_MAGIC:
        return buf
    if len(buf) < 8:
        raise ValueError("truncated KARK header")
    (size,) = struct.unpack_from("<I", buf, 4)
    return kraken_decompress(buf[8:], size)


@dataclass
class FileEntry:
    name_hash64: int
    timestamp: int
    num_inline_buffer_segments: int
    segments_start: int
    segments_end: int
    dependencies_start: int
    dependencies_end: int
    sha1: bytes


class ArchiveError(ValueError):
    """Raised for malformed RDAR containers or corrupt entry data."""


class Archive:
    """Reader for REDengine .archive (RDAR) containers, index layout v11/v12.

    Duplicate name hashes keep the FIRST entry, matching WolvenKit's
    ArchiveReader.ReadFileEntry policy.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.files: dict[int, FileEntry] = {}
        self.segments: list[tuple[int, int, int]] = []  # (offset, zsize, size)
        self.dependencies: list[int] = []
        self._f = open(self.path, "rb")
        try:
            self._read_index()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self._f.close()

    def __enter__(self) -> "Archive":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _read_index(self) -> None:
        f = self._f
        file_size = f.seek(0, 2)
        f.seek(0)
        header = f.read(HEADER_SIZE)
        if len(header) < HEADER_SIZE:
            raise ArchiveError(f"{self.path.name}: truncated header")
        magic, version = struct.unpack_from("<II", header, 0)
        if magic != RDAR_MAGIC:
            raise ArchiveError(
                f"{self.path.name}: not a RDAR archive (magic={magic:#x})"
            )
        if version not in (11, 12):
            raise ArchiveError(
                f"{self.path.name}: unsupported archive version {version}"
            )
        index_position = struct.unpack_from("<Q", header, 8)[0]
        index_size = struct.unpack_from("<I", header, 16)[0]
        if index_position + index_size > file_size:
            raise ArchiveError(
                f"{self.path.name}: index [{index_position:#x}, "
                f"{index_position + index_size:#x}) beyond file size {file_size:#x}"
            )

        f.seek(index_position)
        index = f.read(index_size)
        if len(index) != index_size:
            raise ArchiveError(f"{self.path.name}: truncated index")
        self._parse_index(index)

    def _parse_index(self, index: bytes) -> None:
        off = 0
        # Index header: FileTableOffset u32, FileTableSize u32, Crc u64,
        # FileEntryCount u32, FileSegmentCount u32, ResourceDependencyCount u32.
        (_ft_offset, _ft_size, _crc, num_files, num_segments, num_deps) = (
            struct.unpack_from("<IIQIII", index, off)
        )
        off += INDEX_HEADER_SIZE

        required = (INDEX_HEADER_SIZE + num_files * FILE_ENTRY_SIZE
                    + num_segments * FILE_SEGMENT_SIZE + num_deps * DEPENDENCY_SIZE)
        if required > len(index):
            raise ArchiveError(
                f"{self.path.name}: index tables ({required} bytes) exceed "
                f"index size ({len(index)} bytes)"
            )

        for _ in range(num_files):
            # NameHash64 u64, Timestamp i64, NumInlineBufferSegments u32,
            # SegmentsStart u32, SegmentsEnd u32, ResourceDependenciesStart u32,
            # ResourceDependenciesEnd u32, SHA1[20] => 56 bytes.
            (name_hash64, timestamp, inline, seg_start, seg_end, dep_start,
             dep_end) = struct.unpack_from("<QQIIIII", index, off)
            sha1 = index[off + 36 : off + 56]
            off += FILE_ENTRY_SIZE
            if name_hash64 not in self.files:
                self.files[name_hash64] = FileEntry(
                    name_hash64, timestamp, inline, seg_start, seg_end,
                    dep_start, dep_end, sha1,
                )
        for _ in range(num_segments):
            # FileSegment disk order: Offset u64, ZSize u32 (compressed bytes
            # on disk), Size u32 (uncompressed) — WolvenKit ctor (offset, zsize, size).
            s_off, s_zsize, s_size = struct.unpack_from("<QII", index, off)
            off += FILE_SEGMENT_SIZE
            self.segments.append((s_off, s_zsize, s_size))
        for _ in range(num_deps):
            (dep,) = struct.unpack_from("<Q", index, off)
            off += DEPENDENCY_SIZE
            self.dependencies.append(dep)

    def read_entry(self, name_hash64: int) -> bytes:
        """Extract one entry, verifying its SHA1 against the index."""
        try:
            entry = self.files[name_hash64]
        except KeyError:
            raise ArchiveError(
                f"{self.path.name}: no entry with hash {name_hash64:#x}"
            ) from None
        if not (0 <= entry.segments_start <= entry.segments_end
                <= len(self.segments)):
            raise ArchiveError(
                f"{self.path.name}: entry {name_hash64:#x} has out-of-range "
                f"segment range [{entry.segments_start}, {entry.segments_end})"
            )
        parts = []
        for i in range(entry.segments_start, entry.segments_end):
            s_off, s_zsize, s_size = self.segments[i]
            self._f.seek(s_off)
            raw = self._f.read(s_zsize)
            if len(raw) != s_zsize:
                raise ArchiveError(
                    f"{self.path.name}: truncated segment {i} "
                    f"(wanted {s_zsize}, got {len(raw)})"
                )
            if s_zsize == s_size:
                parts.append(raw)
            elif raw[:4] == KARK_MAGIC:
                parts.append(kark_decompress(raw))
            else:
                parts.append(kraken_decompress(raw, s_size))
        data = b"".join(parts)
        if hashlib.sha1(data).digest() != entry.sha1:
            raise ArchiveError(
                f"{self.path.name}: entry {name_hash64:#x} failed SHA1 check"
            )
        return data


def extract(path: Path, name_hash64: int) -> bytes:
    """Extract a single file from an archive by its FNV1A64 path hash."""
    with Archive(path) as ar:
        return ar.read_entry(name_hash64)


def main() -> None:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Extract one entry from a RDAR archive")
    ap.add_argument("archive")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--hash", type=int)
    group.add_argument("--path")
    args = ap.parse_args()
    name_hash64 = args.hash or fnv1a64(args.path.encode())
    data = extract(Path(args.archive), name_hash64)
    sys.stdout.buffer.write(data)


if __name__ == "__main__":
    main()