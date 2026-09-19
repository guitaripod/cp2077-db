#!/usr/bin/env python3
"""cpdb - query a cp2077-db SQLite dataset from the command line."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

VIEWS = (
    "v_shards", "v_shard_texts", "v_codex", "v_codex_tree", "v_emails",
    "v_contacts", "v_phone", "v_map_pins", "v_quests", "v_objectives",
    "v_quest_tree", "v_tarots", "v_dialogue", "v_items", "v_flat_refs",
    "v_vehicles", "v_perks",
)


def fmt_rows(rows: list[tuple], cols: list[str], wide: bool) -> str:
    if not rows:
        return "(no rows)"
    if wide:
        out = []
        for row in rows:
            parts = []
            for name, val in zip(cols, row):
                if isinstance(val, str) and len(val) > 120:
                    val = val[:117] + "..."
                parts.append(f"{name}: {val}")
            out.append(" | ".join(parts))
        return "\n".join(out)
    widths = [max(len(str(c)), *(len(str(r[i])) for r in rows)) for i, c in enumerate(cols)]
    sep = "-+-".join("-" * w for w in widths)
    head = " | ".join(str(c).ljust(w) for c, w in zip(cols, widths))
    body = "\n".join(
        " | ".join(str(v).ljust(w).replace("\n", "\\n")[:w] for v, w in zip(row, widths))
        for row in rows
    )
    return f"{head}\n{sep}\n{body}"


#: Dataset layout this CLI speaks; kept in step with build.SCHEMA_VERSION.
REQUIRED_SCHEMA_VERSION = 3

SEARCH_SOURCES = ("journal", "lockey", "subtitle")

SEARCH_SQL = {
    "journal": (
        "SELECT j.id AS id, j.kind AS kind, j.path AS ctx, j.title AS title, "
        "snippet(journal_fts, -1, '[', ']', '…', 14) AS excerpt, "
        "bm25(journal_fts) AS rank "
        "FROM journal_fts JOIN journal j ON j.id = journal_fts.rowid "
        "WHERE journal_fts MATCH ? ORDER BY rank LIMIT ?"
    ),
    "lockey": (
        "SELECT l.rowid AS id, 'lockey' AS kind, l.loc_key AS ctx, "
        "l.secondary_key AS title, "
        "snippet(lockeys_fts, -1, '[', ']', '…', 14) AS excerpt, "
        "bm25(lockeys_fts) AS rank "
        "FROM lockeys_fts JOIN lockeys l ON l.rowid = lockeys_fts.rowid "
        "WHERE lockeys_fts MATCH ? ORDER BY rank LIMIT ?"
    ),
    "subtitle": (
        "SELECT s.id AS id, 'subtitle' AS kind, s.file_path AS ctx, "
        "'' AS title, "
        "snippet(subtitles_fts, -1, '[', ']', '…', 14) AS excerpt, "
        "bm25(subtitles_fts) AS rank "
        "FROM subtitles_fts JOIN subtitles s ON s.id = subtitles_fts.rowid "
        "WHERE subtitles_fts MATCH ? ORDER BY rank LIMIT ?"
    ),
}

SEARCH_COLUMNS = ["src", "id", "kind", "ctx", "title", "excerpt", "rank"]


def ranked_search(con: sqlite3.Connection, query: str, sources: list[str],
                  limit: int) -> list[tuple]:
    """Best matches per index, interleaved so every source gets a fair share.

    Each index is queried and bm25-ranked separately (lower is better). They are
    then interleaved rather than merged by score: lockey entries are single short
    strings and would otherwise take every top slot from shards and dialogue.
    """
    per_source = []
    for src in sources:
        rows = [(src, *tuple(row))
                for row in con.execute(SEARCH_SQL[src], (query, limit))]
        if rows:
            per_source.append(rows)
    merged: list[tuple] = []
    while per_source and len(merged) < limit:
        per_source.sort(key=lambda rows: rows[0][-1])
        for rows in list(per_source):
            merged.append(rows.pop(0))
            if not rows:
                per_source.remove(rows)
            if len(merged) >= limit:
                break
    return merged


def schema_version(con: sqlite3.Connection) -> int:
    """The dataset's schema version; 1 for datasets built before it was stored."""
    try:
        row = con.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.Error:
        return 0
    try:
        return int(row[0]) if row else 1
    except (TypeError, ValueError):
        return 0


def parse_sources(value: str | None) -> list[str]:
    if not value:
        return list(SEARCH_SOURCES)
    chosen = [s.strip() for s in value.split(",") if s.strip()]
    unknown = [s for s in chosen if s not in SEARCH_SOURCES]
    if unknown:
        raise SystemExit(
            f"error: unknown search source(s) {', '.join(unknown)}; "
            f"choose from {', '.join(SEARCH_SOURCES)}"
        )
    return chosen


def path_children(con: sqlite3.Connection, prefix: str) -> list[tuple]:
    """Direct children of a journal path with their entry counts, sorted."""
    like = f"{prefix}/%" if prefix else "%"
    depth = len(prefix.split("/")) if prefix else 0
    counts: dict[str, int] = {}
    for (path,) in con.execute(
        "SELECT path FROM journal WHERE path <> '' AND path LIKE ?", (like,)
    ):
        parts = path.split("/")
        if len(parts) <= depth:
            continue
        child = "/".join(parts[: depth + 1])
        counts[child] = counts.get(child, 0) + 1
    return sorted(counts.items())


def journal_entries(con: sqlite3.Connection, target: str, limit: int = 20):
    """Journal rows for an id, an exact path, or a path fragment.

    A container path (a codex entry, a quest, a phone thread) also brings back
    its descendants, so one lookup prints the whole readable article.
    """
    if target.isdigit():
        rows = con.execute(
            "SELECT * FROM journal WHERE id = ?", (int(target),)
        ).fetchall()
        if rows:
            return rows
    exact = con.execute(
        "SELECT * FROM journal WHERE path = ? OR path LIKE ? || '/%' "
        "ORDER BY id LIMIT ?", (target, target, limit)
    ).fetchall()
    if exact:
        return exact
    return con.execute(
        "SELECT * FROM journal WHERE path LIKE '%' || ? || '%' "
        "ORDER BY length(path), id LIMIT ?", (target, limit)
    ).fetchall()


def build_command(argv: list[str]) -> int:
    """`cpdb build <game dir> [db]` - build a dataset from a game install."""
    from .build import GAME_LANGS, BuildError, DatasetBuilder
    from .engine import ArchiveError

    ap = argparse.ArgumentParser(
        prog="cpdb build",
        description="Build a cp2077-db dataset from an installed game.",
    )
    ap.add_argument("game_dir", help="Cyberpunk 2077 install directory")
    ap.add_argument("db_path", nargs="?", default=None,
                    help="output SQLite file (default: ./cp2077.sqlite)")
    ap.add_argument("--lang", default="en", choices=GAME_LANGS,
                    metavar="CODE", help="onscreens language (default: en)")
    args = ap.parse_args(argv)
    db_path = Path(args.db_path) if args.db_path else Path("cp2077.sqlite")
    try:
        DatasetBuilder(Path(args.game_dir), db_path, args.lang).build()
    except (BuildError, ArchiveError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "build":
        return build_command(argv[1:])

    ap = argparse.ArgumentParser(
        prog="cpdb",
        description="Query a cp2077-db dataset. Examples:\n"
        '  cpdb cp2077.sqlite search "militech"\n'
        '  cpdb cp2077.sqlite table v_shards --where "page_path LIKE \'%n54%\'" --limit 5\n'
        '  cpdb cp2077.sqlite sql "SELECT COUNT(*) FROM journal WHERE kind = \'email\'"\n'
        '  cpdb cp2077.sqlite item Items.Preset_Yukimura_Default',
    )
    ap.add_argument("db", help="path to cp2077.sqlite")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("build", help="build a dataset from a game install "
                                 "(cpdb build <game dir> [db])")

    p_search = sub.add_parser("search", help="full-text search across all content")
    p_search.add_argument("query", help="FTS5 MATCH expression, e.g. 'militech' or '\"Arasaka tower\"'")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--source", default=None,
                          help=f"comma-separated subset of: {', '.join(SEARCH_SOURCES)}")
    p_search.add_argument("--json", action="store_true")
    p_search.add_argument("--wide", action="store_true")

    p_page = sub.add_parser("page", help="print one shard/internet page in full")
    p_page.add_argument("path", help="page path or a fragment of it")
    p_page.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", help="print a journal entry and its text")
    p_show.add_argument("target", help="journal id, exact path, or path fragment")
    p_show.add_argument("--json", action="store_true")

    p_tree = sub.add_parser("tree", help="journal categories under a path, with counts")
    p_tree.add_argument("path", nargs="?", default="",
                        help="path prefix, e.g. codex/characters (default: the roots)")
    p_tree.add_argument("--json", action="store_true")

    p_table = sub.add_parser("table", help="list rows of a curated view or table")
    p_table.add_argument("name", help=f"one of: {', '.join(VIEWS)} or any table")
    p_table.add_argument("--where", default=None)
    p_table.add_argument("--order", default=None)
    p_table.add_argument("--limit", type=int, default=20)
    p_table.add_argument("--columns", default=None, help="comma-separated column list")
    p_table.add_argument("--json", action="store_true")
    p_table.add_argument("--wide", action="store_true")

    p_sql = sub.add_parser("sql", help="run an arbitrary SQL query")
    p_sql.add_argument("query")
    p_sql.add_argument("--json", action="store_true")
    p_sql.add_argument("--wide", action="store_true")

    p_item = sub.add_parser("item", help="show one item record with resolved text")
    p_item.add_argument("record_name", help="e.g. Items.Preset_Yukimura_Default")
    p_item.add_argument("--json", action="store_true")

    p_export = sub.add_parser("export", help="derive a smaller dataset (reader profile)")
    p_export.add_argument("dst", help="output SQLite file")
    p_export.add_argument("--profile", default="reader", help="reader (default)")

    p_stats = sub.add_parser("stats", help="row counts per table/view")
    p_stats.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)

    db = Path(args.db)
    if not db.exists():
        print(f"error: {db} not found (build one with: cpdb build <game dir>)",
              file=sys.stderr)
        return 2
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.OperationalError as e:
        print(f"error: cannot open {db}: {e}", file=sys.stderr)
        return 2
    con.row_factory = sqlite3.Row

    found = schema_version(con)
    if found != REQUIRED_SCHEMA_VERSION:
        con.close()
        print(f"error: {db} has dataset schema version {found}, this cpdb needs "
              f"{REQUIRED_SCHEMA_VERSION} — rebuild it with: cpdb build <game dir> {db}",
              file=sys.stderr)
        return 2

    try:
        if args.cmd == "search":
            rows = ranked_search(con, args.query, parse_sources(args.source),
                                 args.limit)
            if args.json:
                print(json.dumps([dict(zip(SEARCH_COLUMNS, r)) for r in rows],
                                 indent=1))
            else:
                print(fmt_rows(rows, SEARCH_COLUMNS, True))

        elif args.cmd == "page":
            rows = con.execute(
                "SELECT source, page_path, site_title, address, body FROM v_shards "
                "WHERE page_path = ? OR page_path LIKE ? ORDER BY page_path",
                (args.path, f"%{args.path}%"),
            ).fetchall()
            if not rows:
                print(f"no page matching {args.path!r}", file=sys.stderr)
                return 1
            if args.json:
                print(json.dumps([dict(r) for r in rows], indent=1))
            else:
                for row in rows:
                    print(f"=== {row['page_path']}  ({row['source']})")
                    if row["site_title"]:
                        print(f"{row['site_title']}  {row['address'] or ''}".strip())
                    print()
                    print(row["body"] or "(no text)")
                    print()

        elif args.cmd == "show":
            rows = journal_entries(con, args.target)
            if not rows:
                print(f"nothing matching {args.target!r}", file=sys.stderr)
                return 1
            if args.json:
                print(json.dumps([dict(r) for r in rows], indent=1))
            else:
                for row in rows:
                    header = f"=== [{row['kind']}] {row['path']}"
                    print(header)
                    if row["title"]:
                        print(row["title"])
                    if row["body"]:
                        print()
                        print(row["body"])
                    if row["extra"]:
                        print(f"\n({row['extra']})")
                    print()

        elif args.cmd == "tree":
            children = path_children(con, args.path.strip("/"))
            if not children:
                print(f"no journal paths under {args.path!r}", file=sys.stderr)
                return 1
            if args.json:
                print(json.dumps([{"path": p, "entries": n}
                                  for p, n in children], indent=1))
            else:
                print(fmt_rows([(p, n) for p, n in children],
                               ["path", "entries"], False))

        elif args.cmd == "table":
            if not args.columns:
                cur = con.execute(f'SELECT * FROM "{args.name}" LIMIT 0')
                cols = [d[0] for d in cur.description]
            else:
                cols = [c.strip() for c in args.columns.split(",")]
            q = f'SELECT {", ".join(chr(34)+c+chr(34) for c in cols)} FROM "{args.name}"'
            if args.where:
                q += f" WHERE {args.where}"
            if args.order:
                q += f" ORDER BY {args.order}"
            q += f" LIMIT {args.limit}"
            rows = [tuple(r) for r in con.execute(q)]
            if args.json:
                print(json.dumps([dict(zip(cols, r)) for r in rows], indent=1))
            else:
                print(fmt_rows(rows, cols, args.wide))

        elif args.cmd == "sql":
            cur = con.execute(args.query)
            cols = [d[0] for d in cur.description]
            rows = [tuple(r) for r in cur.fetchall()]
            if args.json:
                print(json.dumps([dict(zip(cols, r)) for r in rows], indent=1))
            else:
                print(fmt_rows(rows, cols, args.wide))

        elif args.cmd == "item":
            row = con.execute(
                "SELECT * FROM v_items WHERE record_name = ? LIMIT 1",
                (args.record_name,),
            ).fetchone()
            if row is None:
                # fall back to raw flats
                rows = con.execute(
                    "SELECT name, flat_type, value, text FROM tweak_flats f "
                    "LEFT JOIN tweak_flat_texts t ON t.flat_id = f.flat_id AND t.source = f.source "
                    "WHERE f.name LIKE ? ORDER BY f.name",
                    (args.record_name + ".%",),
                ).fetchall()
                if not rows:
                    print(f"no record {args.record_name}", file=sys.stderr)
                    return 1
                if args.json:
                    print(json.dumps([dict(r) for r in rows], indent=1))
                else:
                    for r in rows:
                        val = r["text"] if r["text"] else r["value"]
                        if r["flat_type"] == "gamedataLocKeyWrapper":
                            val = f"{val}  ({r['value']})"
                        print(f"{r['name']}  =  {val}")
            else:
                if args.json:
                    print(json.dumps(dict(row), indent=1))
                else:
                    for k in row.keys():
                        print(f"{k}: {row[k]}")

        elif args.cmd == "export":
            from .export import ExportError, export

            con.close()
            try:
                export(db, Path(args.dst), args.profile)
            except ExportError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
            print(f"exported {args.profile} dataset -> {args.dst}")
            return 0

        elif args.cmd == "stats":
            names = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%' "
                "ORDER BY name")]
            present = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='view'")}
            tables = [(n, con.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0])
                      for n in names]
            views = [(v, con.execute(f'SELECT COUNT(*) FROM "{v}"').fetchone()[0])
                     for v in VIEWS if v in present]
            if args.json:
                print(json.dumps({"tables": dict(tables), "views": dict(views)},
                                 indent=1))
            else:
                for name, cnt in tables:
                    print(f"{name:20s} {cnt:>10d}")
                print()
                for name, cnt in views:
                    print(f"{name:20s} {cnt:>10d}")

    except sqlite3.Error as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        con.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())