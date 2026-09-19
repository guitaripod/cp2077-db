"""Decode the UI textures the journal points at.

Every journal picture (codex art, phone avatars, tarot cards, net-page images)
is a rectangle of an `.inkatlas`: a list of named parts with UV rectangles over
one `.xbm` texture per resolution slot (slot 0 is the 4K art, slot 1 its 1080p
half). The texture payload is a DXT/BC block-compressed bitmap stored
bottom-up; wrapping it in a DDS header lets Pillow decode it.

Pillow is the one optional dependency of this package and is imported lazily,
so everything else keeps working without it.
"""
from __future__ import annotations

import importlib.util
import io
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .cr2w import read_cr2w
from .engine import Archive, fnv1a64

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

#: Encoded image format written to the dataset.
IMAGE_FORMAT = "webp"
IMAGE_QUALITY = 85

#: STextureGroupSetup.compression -> DDS FourCC (DX10 for the BC6/7 family).
FOURCC = {
    "TCM_DXTNoAlpha": b"DXT1",
    "TCM_DXTAlpha": b"DXT5",
    "TCM_DXTAlphaLinear": b"DXT5",
    "TCM_Normalmap": b"ATI2",
    "TCM_QualityR": b"DX10",
    "TCM_QualityRG": b"DX10",
    "TCM_QualityColor": b"DX10",
}
DXGI = {"TCM_QualityR": 80, "TCM_QualityRG": 83, "TCM_QualityColor": 99}


class TextureError(ValueError):
    """Raised when a texture or atlas cannot be decoded."""


def image_key(atlas: str, part: str) -> str:
    """The `images.key` for one atlas part: `<atlas path>#<part name>`."""
    return f"{atlas}#{part}"


def dds_wrap(compression: str, width: int, height: int, data: bytes) -> bytes:
    """A minimal DDS container (single mip) around raw block-compressed data."""
    try:
        fourcc = FOURCC[compression]
    except KeyError:
        raise TextureError(f"unsupported texture compression {compression}") from None
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000
    header = (
        struct.pack("<4sI", b"DDS ", 124)
        + struct.pack("<IIIIII", flags, height, width, len(data), 0, 0)
        + bytes(44)
        + struct.pack("<II4sIIIII", 32, 0x4, fourcc, 0, 0, 0, 0, 0)
        + struct.pack("<IIIII", 0x1000, 0, 0, 0, 0)
    )
    if fourcc == b"DX10":
        header += struct.pack("<IIIII", DXGI[compression], 3, 0, 1, 0)
    return header + data


@dataclass(frozen=True)
class AtlasPart:
    texture: str
    left: float
    top: float
    right: float
    bottom: float


class TextureStore:
    """Finds and decodes atlases and textures across every game archive.

    Decoded textures are cached one at a time: callers group their crops by
    atlas so a 60 MB bitmap is decoded once and released before the next.
    """

    def __init__(self, game_dir: Path):
        pc = game_dir / "archive" / "pc"
        self.archives: list[Archive] = []
        for folder in ("content", "ep1"):
            for path in sorted((pc / folder).glob("*.archive")):
                if path.name.startswith("lang_"):
                    continue
                self.archives.append(Archive(path))
        self.archives_used: set[Path] = set()
        self._atlas_cache: dict[str, list[dict[str, AtlasPart]]] = {}
        self._texture_path: str | None = None
        self._texture: PILImage | None = None

    def close(self) -> None:
        for archive in self.archives:
            archive.close()

    def _locate(self, path: str) -> tuple[Archive, int]:
        h = fnv1a64(path.encode("utf-8"))
        for archive in self.archives:
            if h in archive.files:
                self.archives_used.add(archive.path)
                return archive, h
        raise TextureError(f"{path} not found in any archive")

    def slots(self, atlas: str) -> list[dict[str, AtlasPart]]:
        """Per resolution slot, the atlas's parts by name (highest first)."""
        cached = self._atlas_cache.get(atlas)
        if cached is not None:
            return cached
        archive, h = self._locate(atlas)
        f = read_cr2w(archive.read_entry(h))
        if not f.root or f.root.get("$type") != "inkTextureAtlas":
            raise TextureError(f"{atlas} is not an inkTextureAtlas")
        slots: list[dict[str, AtlasPart]] = []
        for slot in f.root.get("slots") or []:
            texture = slot.get("texture") if isinstance(slot, dict) else None
            if not isinstance(texture, str):
                continue
            parts: dict[str, AtlasPart] = {}
            for mapper in slot.get("parts") or []:
                rect = mapper.get("clippingRectInUVCoords") if isinstance(mapper, dict) else None
                name = mapper.get("partName") if isinstance(mapper, dict) else None
                if not isinstance(rect, dict) or not isinstance(name, str):
                    continue
                parts[name] = AtlasPart(
                    texture,
                    float(rect.get("Left") or 0.0), float(rect.get("Top") or 0.0),
                    float(rect.get("Right") or 0.0), float(rect.get("Bottom") or 0.0),
                )
            slots.append(parts)
        self._atlas_cache[atlas] = slots
        return slots

    def part(self, atlas: str, name: str) -> AtlasPart:
        """The best-resolution slot that carries `name`."""
        for parts in self.slots(atlas):
            if name in parts:
                return parts[name]
        raise TextureError(f"{atlas} has no part {name!r}")

    def texture(self, path: str) -> PILImage:
        """The decoded RGBA `PIL.Image` for one `.xbm`, top row first."""
        if self._texture_path == path and self._texture is not None:
            return self._texture
        from PIL import Image

        archive, h = self._locate(path)
        main, buffers = archive.read_entry_with_buffers(h)
        f = read_cr2w(main)
        root = f.root or {}
        if root.get("$type") != "CBitmapTexture":
            raise TextureError(f"{path} is not a CBitmapTexture")
        width, height = int(root.get("width") or 0), int(root.get("height") or 0)
        setup = root.get("setup") or {}
        compression = str(setup.get("compression") or "")
        blob = (root.get("renderTextureResource") or {}).get("renderResourceBlobPC") or {}
        data_ref = blob.get("textureData") or {}
        index = int(data_ref.get("$buffer") or 0)
        if not (1 <= index <= len(buffers)):
            raise TextureError(f"{path}: texture data buffer {index} missing")
        data = buffers[index - 1]
        if compression == "TCM_None":
            image = Image.frombytes("RGBA", (width, height), data[: width * height * 4])
        else:
            image = Image.open(io.BytesIO(dds_wrap(compression, width, height, data)))
            image = image.convert("RGBA")
        image = image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        self._texture_path, self._texture = path, image
        return image

    def crop(self, atlas: str, name: str) -> PILImage:
        """One atlas part as a `PIL.Image`, at the best resolution available."""
        part = self.part(atlas, name)
        image = self.texture(part.texture)
        box = (
            round(part.left * image.width), round(part.top * image.height),
            round(part.right * image.width), round(part.bottom * image.height),
        )
        if box[2] <= box[0] or box[3] <= box[1]:
            raise TextureError(f"{atlas}#{name} has an empty rectangle")
        return image.crop(box)


def encode(image: PILImage) -> tuple[bytes, int, int]:
    """Lossy WebP (alpha kept only when used); returns (bytes, width, height)."""
    if image.mode == "RGBA" and image.getextrema()[3][0] == 255:
        image = image.convert("RGB")
    out = io.BytesIO()
    image.save(out, IMAGE_FORMAT, quality=IMAGE_QUALITY, method=4)
    return out.getvalue(), image.width, image.height


_TAROT_FIXES = (("pristess", "priestess"), ("judgment", "judgement"),
                ("forutune", "fortune"))


def tarot_part(image_part: str, entry_id: str, available: set[str]) -> str | None:
    """Map the journal's `TarotCard_X` name to the `tarot_xBIG` atlas part.

    The journal misspells three cards and shares one name for the three Fool
    cards, which the atlas numbers; the entry id's trailing digit picks one.
    """
    stem = re.sub(r"^tarotcard_", "", image_part.lower())
    stem = re.sub(r"[^a-z0-9]", "", stem)
    for wrong, right in _TAROT_FIXES:
        stem = stem.replace(wrong, right)
    normalized = {re.sub(r"[^a-z0-9]", "", p.lower()): p for p in available}
    plain = normalized.get(f"tarot{stem}big")
    if plain:
        return plain
    digit = re.search(r"(\d+)$", entry_id)
    number = int(digit.group(1)) + 1 if digit else 1
    return normalized.get(f"tarot{stem}{number:02d}big")


def require_pillow() -> None:
    if importlib.util.find_spec("PIL") is None:
        raise TextureError(
            "image extraction needs Pillow: pip install 'cpdb[images]'"
        )
