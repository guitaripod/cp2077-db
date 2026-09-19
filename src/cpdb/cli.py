#!/usr/bin/env python3
"""cpdb - query a cp2077-db SQLite dataset from the command line."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

VIEWS = (
    "v_shards", "v_shard_texts", "v_codex", "v_emails", "v_quests",
    "v_tarots", "v_items", "v_vehicles", "v_perks",
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
    p_search.add_argument("--json", action="store_true")
    p_search.add_argument("--wide", action="store_true")

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

    p_stats = sub.add_parser("stats", help="row counts per table/view")

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

    try:
        if args.cmd == "search":
            union = (
                "SELECT 'journal' AS src, j.rowid, jf.title, jf.body, jf.path AS ctx "
                "FROM journal_fts jf JOIN journal j ON j.rowid = jf.rowid "
                "WHERE journal_fts MATCH ? "
                "UNION ALL "
                "SELECT 'lockey' AS src, l.rowid, l.secondary_key, l.female_variant, '' "
                "FROM lockeys_fts lf JOIN lockeys l ON l.rowid = lf.rowid "
                "WHERE lockeys_fts MATCH ? "
                "UNION ALL "
                "SELECT 'subtitle' AS src, s.rowid, '', sf.line, s.file_path "
                "FROM subtitles_fts sf JOIN subtitles s ON s.rowid = sf.rowid "
                "WHERE subtitles_fts MATCH ? "
                "LIMIT ?"
            )
            rows = con.execute(union, (args.query, args.query, args.query, args.limit)).fetchall()
            cols = ["src", "rowid", "title", "body", "ctx"]
            print(json.dumps([dict(zip(cols, tuple(r))) for r in rows], indent=1)
                  if args.json
                  else fmt_rows([tuple(r) for r in rows], cols, True))

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

        elif args.cmd == "stats":
            names = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%' ORDER BY name")]
            for n in names:
                cnt = con.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]
                print(f"{n:20s} {cnt:>10d}")
            print()
            for v in VIEWS:
                cnt = con.execute(f'SELECT COUNT(*) FROM "{v}"').fetchone()[0]
                print(f"{v:20s} {cnt:>10d}")
    except sqlite3.Error as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        con.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())