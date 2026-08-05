#!/usr/bin/env python3
"""Run the local research KB maintenance pipeline end to end."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import backfill_report_metadata as backfill
import kb_health_report as health
import update_obsidian_indexes as indexes
from kb_common import (
    DEFAULT_CONFIG_ROOT,
    DEFAULT_DB_PATH,
    DEFAULT_LIBRARY_ROOT,
    DEFAULT_VAULT_ROOT,
    extract_list_value,
    load_kb_configs,
    load_report_notes,
    obsidian_link,
    parse_note_datetime,
    rebuild_search_index,
    stable_report_id,
    write_text_if_changed,
)


def run_backfill(
    *,
    vault_root: Path,
    library_root: Path,
    config_root: Path,
    db_path: Path,
    dry_run: bool,
    force: bool,
    limit: int,
) -> dict[str, Any]:
    configs = load_kb_configs(config_root)
    rows = backfill.load_report_rows(db_path)
    memberships = backfill.load_page_memberships(vault_root)
    notes = load_report_notes(vault_root)
    if limit > 0:
        notes = notes[:limit]

    results: list[dict[str, Any]] = []
    for note in notes:
        results.append(
            backfill.process_note(
                note,
                configs=configs,
                db_row=rows.get(stable_report_id(note)),
                db_path=db_path,
                dry_run=dry_run,
                force=force,
                vault_root=vault_root,
                membership=memberships.get(note.link_target),
            )
        )
    total = len(results)
    coverage = {
        "broker": sum(1 for item in results if item["broker"]),
        "report_date": sum(1 for item in results if item["report_date"]),
        "companies": sum(1 for item in results if extract_list_value(item["companies"])),
        "themes": sum(1 for item in results if extract_list_value(item["themes"])),
    }
    return {
        "processed": total,
        "changed_notes": sum(1 for item in results if item["changed_fields"]),
        "low_confidence": sum(1 for item in results if item["metadata_status"] == "metadata_low_confidence"),
        "coverage": coverage,
        "coverage_pct": {key: round(value / total * 100, 1) if total else 0 for key, value in coverage.items()},
    }


def render_today_note(vault_root: Path, recent_days: int) -> tuple[str, int]:
    now = datetime.now().astimezone()
    cutoff = now - timedelta(days=recent_days)
    notes = [note for note in load_report_notes(vault_root) if parse_note_datetime(note) >= cutoff]
    notes.sort(key=parse_note_datetime, reverse=True)
    high_value = [
        note
        for note in notes
        if float(note.frontmatter.get("metadata_confidence") or 0) >= 0.75
        and extract_list_value(note.frontmatter.get("themes"))
    ]
    needs_review = [
        note
        for note in notes
        if "metadata_low_confidence" in extract_list_value(note.frontmatter.get("quality_status"))
        or not extract_list_value(note.frontmatter.get("companies"))
        or not extract_list_value(note.frontmatter.get("themes"))
    ]
    lines = [
        "---",
        "type: maintenance",
        f"generated_at: {json.dumps(now.isoformat(timespec='seconds'), ensure_ascii=False)}",
        "generated_by: kb_maintain_wiki",
        f"recent_days: {recent_days}",
        "---",
        "",
        "# 今日新增值得看",
        "",
        "## 高相关新增",
    ]
    for note in high_value[:40]:
        date = str(note.frontmatter.get("report_date") or "")[:10]
        broker = str(note.frontmatter.get("broker") or "")
        themes = "、".join(extract_list_value(note.frontmatter.get("themes"))[:3])
        lines.append(f"- {obsidian_link(note)}（{date or '-'} · {broker or '-'} · {themes or '-'}）")
    if not high_value:
        lines.append("- 暂无")
    lines.extend(["", "## 需要补实体/低置信度"])
    for note in needs_review[:40]:
        status = "、".join(extract_list_value(note.frontmatter.get("quality_status")))
        lines.append(f"- {obsidian_link(note)}（{status or '待确认'}）")
    if not needs_review:
        lines.append("- 暂无")
    lines.extend(
        [
            "",
            "## 固定检索入口",
            "- `python3 scripts/kb_search.py \"Google TPU 利润率\" --company Google --since 180d`",
            "- `python3 scripts/kb_search.py \"胜宏科技 HDI ABF\" --limit 20`",
            "- `python3 scripts/kb_answer.py \"Google TPU 对利润率有什么影响\" --company Google --since 365d`",
            "",
        ]
    )
    return "\n".join(lines), len(notes)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Maintain ResearchVault dashboards, search, and health reports.")
    parser.add_argument("--vault-root", default=str(DEFAULT_VAULT_ROOT))
    parser.add_argument("--library-root", default=str(DEFAULT_LIBRARY_ROOT))
    parser.add_argument("--config-root", default=str(DEFAULT_CONFIG_ROOT))
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--recent-days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force-backfill", action="store_true")
    parser.add_argument("--skip-backfill", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    vault_root = Path(args.vault_root).expanduser().resolve(strict=False)
    library_root = Path(args.library_root).expanduser().resolve(strict=False)
    config_root = Path(args.config_root).expanduser().resolve(strict=False)
    db_path = Path(args.db_path).expanduser().resolve(strict=False)

    result: dict[str, Any] = {
        "ok": True,
        "dry_run": args.dry_run,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if not args.skip_backfill:
        result["backfill"] = run_backfill(
            vault_root=vault_root,
            library_root=library_root,
            config_root=config_root,
            db_path=db_path,
            dry_run=args.dry_run,
            force=args.force_backfill,
            limit=args.limit,
        )
    if not args.dry_run:
        result["fts"] = rebuild_search_index(db_path, vault_root, config_root, upsert_notes=False)
    else:
        result["fts"] = {"indexed": 0, "dry_run": True}
    result["dashboards"] = indexes.rebuild_all_indexes(vault_root=vault_root, dry_run=args.dry_run, recent_days=args.recent_days)

    summary, buckets = health.build_health(vault_root=vault_root, library_root=library_root, config_root=config_root, db_path=db_path)
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    health_text = health.render_health_report(summary, buckets, generated_at)
    health_path = vault_root / "99_维护" / "数据质量清单.md"
    if not args.dry_run:
        write_text_if_changed(health_path, health_text)
    result["health"] = {**summary, "output": str(health_path)}

    today_text, recent_count = render_today_note(vault_root, args.recent_days)
    today_path = vault_root / "99_维护" / "今日新增值得看.md"
    if not args.dry_run:
        write_text_if_changed(today_path, today_text)
    result["today"] = {"recent_count": recent_count, "output": str(today_path)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
