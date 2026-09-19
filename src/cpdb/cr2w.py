"""CR2W reader for Cyberpunk 2077 localization resources.

Implements enough of the CR2W container + RedPackage CVariable serialization
(WolvenKit.RED4/Archive/IO/CR2WReader.cs, GPL-3.0) to decode JsonResource files
holding localizationPersistenceOnScreenEntries / SubtitleEntries, without a
full RTTI registry: the serialized variables are self-describing
(name CName, type CName, u32 size), so unknown types can be skipped by size.

Only the flat value types used by these resources are supported: CName, String,
LocalizationString, TweakDBID, CHandle (as opaque handle id), arrays of the
above, and small numeric fundamentals.
"""
from __future__ import annotations

import struct

CR2W_MAGIC = 0x57325243  # "CR2W" LE u32
FNV_BASIS = 0xCBF29CE484222325
FNV_PRIME = 0x100000001B3


def fnv1a64(data: bytes) -> int:
    h = FNV_BASIS
    for b in data:
        h = ((h ^ b) * FNV_PRIME) & 0xFFFFFFFFFFFFFFFF
    return h


class _CName(str):
    """Marker for strings that came from the CR2W name table (CName)."""


class _Reader:
    __slots__ = ("data", "pos")

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read(self, n: int) -> bytes:
        b = self.data[self.pos : self.pos + n]
        if len(b) != n:
            raise ValueError(f"short read at {self.pos}")
        self.pos += n
        return b

    def u8(self) -> int:
        return self.read(1)[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.read(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.read(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.read(8))[0]

    def f32(self) -> float:
        return struct.unpack("<f", self.read(4))[0]

    def seek(self, pos: int) -> None:
        self.pos = pos


class CR2WFile:
    def __init__(self) -> None:
        self.version: int = 0
        self.strings: dict[int, str] = {}      # offset -> string
        self.names: list[str] = []            # index -> name
        self.imports: list[str] = []
        self.exports: list[dict] = []          # className, dataSize, dataOffset
        self.root: dict | None = None


def read_cr2w(data: bytes) -> CR2WFile:
    r = _Reader(data)
    if r.u32() != CR2W_MAGIC:
        raise ValueError("not a CR2W file")
    f = CR2WFile()
    version = r.u32()
    if not 163 <= version <= 195:
        raise ValueError(f"unsupported CR2W version {version}")
    f.version = version
    r.read(4)          # flags
    r.u64()             # timeStamp
    r.u32()             # buildVersion
    r.u32()             # objectsEnd
    r.u32()             # buffersEnd
    r.u32()             # crc32
    r.u32()             # numChunks

    tables = []
    for _ in range(10):
        offset, count, _crc = struct.unpack("<III", r.read(12))
        tables.append((offset, count))

    # table 0: string dict (offset-relative positions)
    str_off, str_len = tables[0]
    p = str_off
    end = str_off + str_len
    while p < end:
        zero = data.index(b"\x00", p)
        s = data[p:zero].decode("utf-8")
        f.strings[p - str_off] = s if s else "None"
        p = zero + 1

    # table 1: names (u32 offset into strings, u32 hash)
    off, count = tables[1]
    r.seek(off)
    for _ in range(count):
        so, _h = struct.unpack("<II", r.read(8))
        f.names.append(f.strings[so])

    # table 2: imports (u32 offset, u16 className, u16 flags)
    off, count = tables[2]
    r.seek(off)
    for _ in range(count):
        so, cn, _fl = struct.unpack("<IHH", r.read(8))
        f.imports.append(f.strings[so])

    # table 3: properties (unused in these files) — skip struct size 24
    # table 4: exports (24 bytes each)
    off, count = tables[4]
    r.seek(off)
    for _ in range(count):
        cname, objflags, parent, dsize, doffset, template, _crc = struct.unpack(
            "<HHIIIII", r.read(24)
        )
        f.exports.append(
            {"className": f.names[cname], "dataSize": dsize, "dataOffset": doffset}
        )

    # Decode every chunk; CHandle values are resolved to chunk payloads after.
    chunks = []
    for e in f.exports:
        r.seek(e["dataOffset"])
        chunks.append(_read_object(r, f, e["className"], e["dataSize"]))
    _resolve_handles(chunks)
    f.root = chunks[0] if chunks else None
    return f

def _resolve_handles(chunks: list) -> None:
    """Replace {"$handle": n} markers with the decoded chunk payloads.

    The chunk object itself stays in `chunks` (it is shared, not copied); the
    JSON-like tree keeps the handle marker replaced by direct reference.
    """
    def resolve(node):
        if isinstance(node, dict):
            if set(node.keys()) == {"$handle"} and isinstance(node["$handle"], int):
                idx = node["$handle"]
                return chunks[idx] if 0 <= idx < len(chunks) else None
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    for c in chunks:
        for k, v in list(c.items()):
            c[k] = resolve(v)

def _read_lp_string(r: _Reader) -> str:
    """CR2W uses ReadLengthPrefixedString: VLQ len, + = UTF-16LE, - = UTF-8."""
    b = r.u8()
    neg = bool(b & 0x80)
    val = b & 0x3F
    if b & 0x40:
        shift = 6
        while True:
            b = r.u8()
            val |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                break
    if val == 0:
        return ""
    if neg:
        return r.read(val).decode("utf-8")
    return r.read(val * 2).decode("utf-16-le")


def _read_value(r: _Reader, f: CR2WFile, red_type: str, size: int):
    if red_type == "CName":
        idx = r.u16()
        return _CName(f.names[idx]) if idx < len(f.names) else None
    if red_type == "String":
        return _read_lp_string(r)
    if red_type == "LocalizationString":
        r.u64()
        return {"$locstr": True, "value": _read_lp_string(r)}
    if red_type == "CRUID":
        return r.u64()
    if red_type == "ResourcePath":
        return r.u64()
    if red_type == "CResourceAsyncReference":
        return r.u16()
    if red_type == "CResourceReference":
        return r.u16()
    if red_type.startswith("handle:") or red_type.startswith("whandle:"):
        return {"$handle": r.i32() - 1}
    if red_type == "raRef:CResource":
        return r.u64()
    if red_type == "NodeRef":
        return _read_lp_string(r)
    if red_type == "Color":
        rr, g, b, a = r.read(4)
        return {"$color": (rr, g, b, a)}
    if red_type == "Vector2":
        return (r.f32(), r.f32())
    if red_type == "Vector3":
        return (r.f32(), r.f32(), r.f32())
    if red_type == "Vector4":
        return (r.f32(), r.f32(), r.f32(), r.f32())
    if red_type == "EulerAngles":
        return (r.f32(), r.f32(), r.f32())
    if red_type == "Quaternion":
        return (r.f32(), r.f32(), r.f32(), r.f32())
    if red_type == "Bool":
        return bool(r.u8())
    if red_type == "Int8":
        return struct.unpack("<b", r.read(1))[0]
    if red_type == "Uint8":
        return r.u8()
    if red_type == "Int16":
        return struct.unpack("<h", r.read(2))[0]
    if red_type == "Uint16":
        return r.u16()
    if red_type == "Int32":
        return r.i32()
    if red_type == "Uint32":
        return r.u32()
    if red_type == "Int64":
        return struct.unpack("<q", r.read(8))[0]
    if red_type == "Uint64":
        return r.u64()
    if red_type == "Float":
        return r.f32()
    if red_type.startswith("array:"):
        inner = red_type[len("array:"):]
        count = r.u32()
        return [_read_value(r, f, inner, 0) for _ in range(count)]
    if red_type.startswith("static:"):
        # static array: static:[n; inner]
        rest = red_type[len("static:"):]
        n_str, inner = rest.split(";", 1)
        r.u32()
        return [_read_value(r, f, inner, 0) for _ in range(int(n_str.strip("[]")))]
    if CLASS_RE.match(red_type) and red_type not in ENUM_NAMES:
        return _read_class_value(r, f, red_type)
    if CLASS_RE.match(red_type):
        # enum value: u16 index into the name table
        idx = r.u16()
        return _CName(f.names[idx]) if idx < len(f.names) else idx
    return r.u16()


import re as _re

from cpdb.enum_names import ENUM_NAMES

CLASS_RE = _re.compile(r"^[a-z][A-Za-z0-9_]*$")




def _read_class_value(r: _Reader, f: CR2WFile, class_name: str) -> dict:
    """Nested CVariable inside an array: [0x00][vars...] until name==0.

    Per-element size is not declared; the zero-terminator ends the object.
    """
    zero = r.u8()
    if zero != 0:
        raise ValueError(f"expected zero pad in nested {class_name}, got {zero}")
    obj: dict[str, object] = {"$type": class_name}
    while True:
        name_idx = r.u16()
        if name_idx == 0:
            break
        var_name = f.names[name_idx] if name_idx < len(f.names) else str(name_idx)
        type_idx = r.u16()
        var_type = f.names[type_idx] if type_idx < len(f.names) else str(type_idx)
        vsize = r.u32() - 4
        data_start = r.pos
        try:
            val = _read_value(r, f, var_type, vsize)
        except ValueError:
            r.seek(data_start + vsize)
            val = None
        obj[var_name] = val
    return obj
def _read_object(r: _Reader, f: CR2WFile, class_name: str, size: int) -> dict:
    """Read a CVariable chunk: [0x00] then sequence of typed variables."""
    zero = r.u8()
    if zero != 0:
        raise ValueError(f"expected zero pad in {class_name}, got {zero}")
    obj: dict[str, object] = {"$type": class_name}
    start = r.pos
    while r.pos < start + size:
        name_idx = r.u16()
        if name_idx == 0:
            break
        var_name = f.names[name_idx] if name_idx < len(f.names) else str(name_idx)
        type_idx = r.u16()
        var_type = f.names[type_idx] if type_idx < len(f.names) else str(type_idx)
        vsize = r.u32() - 4
        data_start = r.pos
        try:
            val = _read_value(r, f, var_type, vsize)
        except ValueError:
            # skip unknown type by size
            r.seek(data_start + vsize)
            val = None
        obj[var_name] = val
    return obj