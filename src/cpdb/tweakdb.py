"""TweakDB blob parser for cp2077-db.

Implements the byte layout of WolvenKit.RED4/TweakDB/TweakDBReader.cs (GPL-3.0)
in stdlib Python: flats (per-type tables keyed by FNV1A64 of the red type name),
records (id + murmur3 type key), queries, group tags.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

TWEAKDB_MAGIC = 0x0BB1DB47
BLOB_VERSION = 8
PARSER_VERSION = 4
RECORDS_SEED = 0x5EEDBA5E

FNV_BASIS = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3

TWEAK_TYPE_NAMES = {
    0: "CName",
    1: "String",
    2: "TweakDBID",
    3: "raRef:CResource",
    4: "Float",
    5: "Bool",
    6: "Uint8",
    7: "Uint16",
    8: "Uint32",
    9: "Uint64",
    10: "Int8",
    11: "Int16",
    12: "Int32",
    13: "Int64",
    14: "Color",
    15: "EulerAngles",
    16: "Quaternion",
    17: "Vector2",
    18: "Vector3",
    19: "gamedataLocKeyWrapper",
}


def fnv1a64(data: bytes) -> int:
    h = FNV_BASIS
    for b in data:
        h = ((h ^ b) * FNV_PRIME) & 0xFFFFFFFFFFFFFFFF
    return h


def murmur3(data: bytes, seed: int) -> int:
    """MurmurHash3 x86 32bit, WolvenKit.Core/Murmur3/Murmur32.cs semantics."""
    c1 = 0xCC9E2D51
    c2 = 0x1B873593
    length = len(data)
    val = seed & 0xFFFFFFFF
    nblocks = length >> 2
    for i in range(nblocks):
        k = struct.unpack_from("<I", data, i * 4)[0]
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        val ^= k
        val = ((val << 13) | (val >> 19)) & 0xFFFFFFFF
        val = (val * 5 + 0xE6546B64) & 0xFFFFFFFF
    k = 0
    tail = data[nblocks * 4 :]
    for i in range(len(tail) - 1, -1, -1):
        k = (k << 8) | tail[i]
    if tail:
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        val ^= k
    val ^= length
    val ^= val >> 16
    val = (val * 0x85EBCA6B) & 0xFFFFFFFF
    val ^= val >> 13
    val = (val * 0xC2B2AE35) & 0xFFFFFFFF
    val ^= val >> 16
    return val


def crc32(data: bytes) -> int:
    """CRC-32 (IEEE 802.3, reflected), the variant Crc32Algorithm uses."""
    import zlib

    return zlib.crc32(data) & 0xFFFFFFFF


def tweakdbid_hash(name: str) -> int:
    """TweakDBID: crc32(path) + (len(path) << 32)."""
    b = name.encode("utf-8")
    return crc32(b) + (len(b) << 32)


class _Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read(self, n: int) -> bytes:
        b = self.data[self.pos : self.pos + n]
        if len(b) != n:
            raise ValueError(f"short read {len(b)} at {self.pos}, wanted {n}")
        self.pos += n
        return b

    def u8(self) -> int:
        return self.read(1)[0]

    def i8(self) -> int:
        return struct.unpack("<b", self.read(1))[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.read(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.read(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.read(8))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.read(8))[0]

    def f32(self) -> float:
        return struct.unpack("<f", self.read(4))[0]

    def seek(self, pos: int) -> None:
        self.pos = pos

    def vlq(self) -> int:
        """ReadVLQInt32: first byte has sign(0x80) and has-more(0x40) bits,
        6 value bits; then 7 value bits per continuation byte."""
        b = self.read(1)[0]
        neg = bool(b & 0x80)
        val = b & 0x3F
        if b & 0x40:
            shift = 6
            while True:
                b = self.read(1)[0]
                val |= (b & 0x7F) << shift
                shift += 7
                if not (b & 0x80):
                    break
        return -val if neg else val

    def lp_string(self) -> str:
        """Length-prefixed string: VLQ length; positive = UTF-16LE chars,
        negative = UTF-8 bytes (ReadLengthPrefixedString)."""
        n = self.vlq()
        if n == 0:
            return ""
        if n > 0:
            s = self.read(n * 2).decode("utf-16-le")
        else:
            s = self.read(-n).decode("utf-8")
        return s


@dataclass
class FlatTypeInfo:
    type_hash: int
    value_count: int
    key_count: int
    offset: int


@dataclass
class TweakDB:
    flats: dict[int, object] = field(default_factory=dict)
    flat_type_by_id: dict[int, str] = field(default_factory=dict)
    records: dict[int, int] = field(default_factory=dict)  # id -> murmur3 type key
    queries: dict[int, list[int]] = field(default_factory=dict)
    group_tags: dict[int, int] = field(default_factory=dict)
    flat_types: list[FlatTypeInfo] = field(default_factory=list)
    checksum: int = 0


def _read_flat_value(r: _Reader, red_type: str) -> object:
    """Decode one flat value of the given red type (ETweakType set + arrays)."""
    if red_type.startswith("array:"):
        inner = red_type[len("array:") :]
        count = r.vlq()
        return [_read_flat_value(r, inner) for _ in range(count)]
    if red_type == "CName":
        return r.lp_string()
    if red_type == "String":
        return r.lp_string()
    if red_type == "TweakDBID":
        return r.u64()
    if red_type == "raRef:CResource":
        return r.u64()
    if red_type == "Float":
        return r.f32()
    if red_type == "Bool":
        return bool(r.u8())
    if red_type == "Uint8":
        return r.u8()
    if red_type == "Uint16":
        return r.u16()
    if red_type == "Uint32":
        return r.u32()
    if red_type == "Uint64":
        return r.u64()
    if red_type == "Int8":
        return r.i8()
    if red_type == "Int16":
        return struct.unpack("<h", r.read(2))[0]
    if red_type == "Int32":
        return r.i32()
    if red_type == "Int64":
        return r.i64()
    if red_type == "Color":
        # Red4Reader.Color: red, green, blue, alpha u8 each
        rr, g, b, a = r.read(4)
        return (rr, g, b, a)
    if red_type == "EulerAngles":
        return (r.f32(), r.f32(), r.f32())
    if red_type == "Quaternion":
        return (r.f32(), r.f32(), r.f32(), r.f32())
    if red_type == "Vector2":
        return (r.f32(), r.f32())
    if red_type == "Vector3":
        return (r.f32(), r.f32(), r.f32())
    if red_type == "gamedataLocKeyWrapper":
        return r.u64()
    raise ValueError(f"unsupported flat type {red_type!r}")


def parse(data: bytes) -> TweakDB:
    r = _Reader(data)
    if r.u32() != TWEAKDB_MAGIC:
        raise ValueError("not a tweakdb blob")
    blob_version = r.u32()
    parser_version = r.u32()
    if blob_version != BLOB_VERSION or parser_version != PARSER_VERSION:
        raise ValueError(
            f"unsupported tweakdb version {blob_version}/{parser_version}"
        )
    checksum = r.u32()
    flats_offset = r.i32()
    records_offset = r.i32()
    queries_offset = r.i32()
    group_tags_offset = r.i32()
    db = TweakDB(checksum=checksum)
    _read_flats(r, flats_offset, db)
    _read_records(r, records_offset, db)
    _read_queries(r, queries_offset, db)
    _read_group_tags(r, group_tags_offset, db)
    return db


def _read_flats(r: _Reader, offset: int, db: TweakDB) -> None:
    r.seek(offset)
    num_types = r.i32()
    type_infos = []
    for _ in range(num_types):
        type_hash = r.u64()
        value_count = r.u32()
        key_count = r.u32()
        type_offset = r.u32()
        type_infos.append((type_hash, value_count, key_count, type_offset))

    # Map type hash -> red type string, then decode values.
    hash_to_redtype = {}
    for idx, name in TWEAK_TYPE_NAMES.items():
        hash_to_redtype[fnv1a64(name.encode("ascii"))] = name
        hash_to_redtype[fnv1a64(f"array:{name}".encode("ascii"))] = f"array:{name}"

    for type_hash, value_count, key_count, type_offset in type_infos:
        red_type = hash_to_redtype.get(type_hash)
        if red_type is None:
            raise ValueError(f"unknown flat type hash {type_hash:#x}")
        r.seek(type_offset)
        n_values = r.u32()
        values = []
        for _ in range(n_values):
            values.append(_read_flat_value(r, red_type))
        n_keys = r.u32()
        for _ in range(n_keys):
            flat_id = r.u64()
            value_index = r.i32()
            db.flats[flat_id] = values[value_index]
            db.flat_type_by_id[flat_id] = red_type
        db.flat_types.append(
            FlatTypeInfo(type_hash, value_count, key_count, type_offset)
        )


def _read_records(r: _Reader, offset: int, db: TweakDB) -> None:
    r.seek(offset)
    n = r.i32()
    for _ in range(n):
        rid = r.u64()
        type_key = r.u32()
        db.records[rid] = type_key


def _read_queries(r: _Reader, offset: int, db: TweakDB) -> None:
    r.seek(offset)
    n = r.i32()
    for _ in range(n):
        qid = r.u64()
        count = r.u32()
        entries = [r.u64() for _ in range(count)]
        db.queries[qid] = entries


def _read_group_tags(r: _Reader, offset: int, db: TweakDB) -> None:
    r.seek(offset)
    n = r.i32()
    for _ in range(n):
        gid = r.u64()
        db.group_tags[gid] = r.u8()


def load(path: Path) -> TweakDB:
    return parse(Path(path).read_bytes())