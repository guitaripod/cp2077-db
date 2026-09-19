"""Derive smaller, purpose-built datasets from a full cp2077.sqlite.

The `reader` profile keeps everything a person reads (journal, subtitles, their
FTS indexes and the curated views over them) and drops the TweakDB half, which
is 85% of the file and only matters for stat lookups.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

PROFILES = ("reader",)

READER_DROP_TABLES = (
    "tweak_flats", "tweak_flat_texts", "tweak_records", "tweak_queries",
    "lockeys", "lockeys_fts",
)
READER_DROP_VIEWS = ("v_items", "v_flat_refs", "v_vehicles", "v_perks")


class ExportError(RuntimeError):
    """Raised when the source dataset cannot be exported."""


def export(src: Path, dst: Path, profile: str = "reader") -> None:
    if profile not in PROFILES:
        raise ExportError(f"unknown profile {profile!r}; one of: {' '.join(PROFILES)}")
    if not src.is_file():
        raise ExportError(f"{src} not found")
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    target = sqlite3.connect(tmp)
    try:
        source.backup(target)
        source.close()
        _apply_reader_profile(target)
        target.commit()
        target.execute("VACUUM")
        target.close()
        os.replace(tmp, dst)
    except Exception:
        target.close()
        if tmp.exists():
            tmp.unlink()
        raise


def _apply_reader_profile(con: sqlite3.Connection) -> None:
    existing = {
        row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
    }
    for view in READER_DROP_VIEWS:
        if view in existing:
            con.execute(f"DROP VIEW {view}")
    for table in READER_DROP_TABLES:
        if table in existing:
            con.execute(f"DROP TABLE {table}")
    con.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('profile', 'reader')")
