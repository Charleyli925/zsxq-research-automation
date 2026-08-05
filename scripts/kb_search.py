#!/usr/bin/env python3
"""Search the local research knowledge base with SQLite FTS5."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from kb_common import (
    DEFAULT_CONFIG_ROOT,
    DEFAULT_DB_PATH,
    DEFAULT_LIBRARY_ROOT,
    DEFAULT_VAULT_ROOT,
    rebuild_search_index,
    search_reports,
)


def table_exists(db_path: Path, name: str) -> bool:
    if not db_path.exists():
        return False
    with closing(sqlite3.connect(str(db_path))) as conn:
        row = conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone()
    return row is not None


def parse_since(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.endswith("d") and raw[:-1].isdigit():
        return (datetime.now().astimezone() - timedelta(days=int(raw[:-1]))).date().isoformat()
    return raw


def render_result(index: int, item: dict[str, object]) -> str:
    companies = "、".join(item.get("companies", []) or [])
    themes = "、".join(item.get("themes", []) or [])
    subthemes = "、".join(item.get("subthemes", []) or [])
    lines = [
        f"{index}. {item.get('title', '')}",
        f"   日期/券商: {item.get('report_date', '') or '-'} / {item.get('broker', '') or '-'}",
        f"   公司: {companies or '-'}",
        f"   主题: {themes or '-'}" + (f" / {subthemes}" if subthemes else ""),
        f"   命中: {item.get('snippet', '') or '-'}",
        f"   Obsidian: {item.get('note_path', '') or '-'}",
        f"   PDF: {item.get('pdf_path', '') or '-'}",
        f"   Feishu: {item.get('feishu_doc_url', '') or '-'}",
    ]
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search ResearchLibrary/ResearchVault reports.")
    parser.add_argument("query")
    parser.add_argument("--company", default="")
    parser.add_argument("--theme", default="")
    parser.add_argument("--broker", default="")
    parser.add_argument("--since", default="", help="YYYY-MM-DD or Nd, e.g. 180d.")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--vault-root", default=str(DEFAULT_VAULT_ROOT))
    parser.add_argument("--library-root", default=str(DEFAULT_LIBRARY_ROOT))
    parser.add_argument("--config-root", default=str(DEFAULT_CONFIG_ROOT))
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = Path(args.db_path).expanduser().resolve(strict=False)
    vault_root = Path(args.vault_root).expanduser().resolve(strict=False)
    config_root = Path(args.config_root).expanduser().resolve(strict=False)
    if args.rebuild or not table_exists(db_path, "report_search"):
        rebuild_search_index(db_path, vault_root, config_root, upsert_notes=False)

    results = search_reports(
        db_path,
        args.query,
        company=args.company.strip(),
        theme=args.theme.strip(),
        broker=args.broker.strip(),
        since_date=parse_since(args.since),
        limit=args.limit,
    )
    if args.json:
        print(json.dumps({"query": args.query, "results": results}, ensure_ascii=False, indent=2))
    else:
        print(f"query: {args.query}")
        print(f"results: {len(results)}")
        print("")
        for index, item in enumerate(results, start=1):
            print(render_result(index, item))
            print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
