#!/usr/bin/env python3
"""Generate a health report for the local research knowledge base."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from kb_common import (
    DEFAULT_CONFIG_ROOT,
    DEFAULT_DB_PATH,
    DEFAULT_LIBRARY_ROOT,
    DEFAULT_VAULT_ROOT,
    candidate_aliases_from_title,
    extract_list_value,
    extract_report_metadata,
    load_kb_configs,
    load_report_notes,
    obsidian_link,
    parse_note_datetime,
    path_from_uri,
    stable_report_id,
)


def count_files(root: Path, pattern: str) -> int:
    return sum(1 for _ in root.rglob(pattern)) if root.exists() else 0


def db_scalar(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> int:
    if not db_path.exists():
        return 0
    with closing(sqlite3.connect(str(db_path))) as conn:
        try:
            row = conn.execute(sql, params).fetchone()
        except sqlite3.OperationalError:
            return 0
    return int(row[0] or 0) if row else 0


def duplicate_groups(notes: list[Any]) -> list[list[Any]]:
    groups: dict[str, list[Any]] = defaultdict(list)
    for note in notes:
        key = str(note.frontmatter.get("report_id") or note.frontmatter.get("pdf_path") or note.path)
        groups[key].append(note)
    return [group for group in groups.values() if len(group) > 1]


def limited_links(notes: list[Any], limit: int = 50) -> list[str]:
    lines = [f"- {obsidian_link(note)}" for note in notes[:limit]]
    if len(notes) > limit:
        lines.append(f"- ...另有 {len(notes) - limit} 条")
    return lines or ["- 暂无"]


def render_health_report(summary: dict[str, Any], buckets: dict[str, list[Any]], generated_at: str) -> str:
    lines = [
        "---",
        "type: maintenance",
        f"generated_at: {json.dumps(generated_at, ensure_ascii=False)}",
        "generated_by: kb_health_report",
        "---",
        "",
        "# 数据质量清单",
        "",
        "## 总览",
        f"- report note 总数: {summary['report_note_count']}",
        f"- SQLite reports: {summary['sqlite_report_count']}",
        f"- PDF / summary / raw / clean: {summary['pdf_count']} / {summary['summary_count']} / {summary['raw_count']} / {summary['clean_count']}",
        f"- 缺 Feishu 链接: {summary['missing_feishu']}",
        f"- 缺 raw / clean: {summary['missing_raw']} / {summary['missing_clean']}",
        f"- needs_review: {summary['needs_review']}",
        f"- summary_failed: {summary['summary_failed']}",
        f"- feishu_failed: {summary['feishu_failed']}",
        f"- 重复报告组: {summary['duplicate_group_count']}",
        f"- 脏标题: {summary['dirty_title_count']}",
        f"- company 覆盖: {summary['company_matched']} / {summary['report_note_count']} ({summary['company_coverage_pct']}%)",
        f"- company 适用性覆盖: {summary['company_scope_covered']} / {summary['report_note_count']} ({summary['company_scope_coverage_pct']}%)",
        f"- 公司不适用 / 需补公司: {summary['company_not_applicable']} / {summary['company_needs_entity']}",
        f"- 未命中主题 / 公司 / 实体: {summary['unmatched_theme']} / {summary['unmatched_company']} / {summary['unmatched_entity']}",
        f"- 最近 7 / 30 天新增: {summary['recent_7d']} / {summary['recent_30d']}",
        "",
        "## 不可检索或检索质量低",
        *limited_links(buckets["retrieval_issues"]),
        "",
        "## 需要补实体/主题",
        *limited_links(buckets["metadata_issues"]),
        "",
        "## 候选 alias / 新实体",
    ]
    if buckets["candidate_aliases"]:
        for name, count, examples in buckets["candidate_aliases"][:60]:
            sample_links = "；".join(obsidian_link(note) for note in examples[:3])
            lines.append(f"- `{name}`（{count}）: {sample_links}")
    else:
        lines.append("- 暂无")
    lines.extend(
        [
            "",
            "## 重复报告",
        ]
    )
    if buckets["duplicates"]:
        for group in buckets["duplicates"][:40]:
            lines.append(f"- `{stable_report_id(group[0])}`")
            for note in group:
                lines.append(f"  - {obsidian_link(note)}")
        if len(buckets["duplicates"]) > 40:
            lines.append(f"- ...另有 {len(buckets['duplicates']) - 40} 组")
    else:
        lines.append("- 暂无")
    lines.extend(
        [
            "",
            "## 脏标题",
            *limited_links(buckets["dirty_titles"]),
            "",
            "## Summary/Feishu 失败",
            *limited_links(buckets["failed_notes"]),
            "",
            "## 最近新增",
            *limited_links(buckets["recent_notes"], 80),
            "",
            "## 自动维护入口",
            "- `python3 scripts/kb_maintain_wiki.py`",
            "- `python3 scripts/backfill_report_metadata.py`",
            "- `python3 scripts/kb_search.py \"Google TPU 利润率\" --company Google --since 180d`",
            "- `python3 scripts/kb_answer.py \"Google TPU 对利润率有什么影响\" --company Google --since 365d`",
            "- `python3 scripts/update_obsidian_indexes.py --rebuild-all`",
            "",
        ]
    )
    return "\n".join(lines)


def build_health(
    *,
    vault_root: Path,
    library_root: Path,
    config_root: Path,
    db_path: Path,
) -> tuple[dict[str, Any], dict[str, list[Any]]]:
    configs = load_kb_configs(config_root)
    notes = load_report_notes(vault_root)
    now = datetime.now().astimezone()
    cutoff_7 = now - timedelta(days=7)
    cutoff_30 = now - timedelta(days=30)

    missing_feishu: list[Any] = []
    missing_raw: list[Any] = []
    missing_clean: list[Any] = []
    retrieval_issues: list[Any] = []
    metadata_issues: list[Any] = []
    dirty_titles: list[Any] = []
    failed_notes: list[Any] = []
    recent_notes: list[Any] = []
    quality_counter: Counter[str] = Counter()
    unmatched_theme = 0
    unmatched_company = 0
    unmatched_entity = 0
    company_matched = 0
    company_not_applicable = 0
    company_needs_entity = 0
    candidate_alias_notes: dict[str, list[Any]] = defaultdict(list)

    for note in notes:
        metadata = extract_report_metadata(note, configs, vault_root)
        quality = set(extract_list_value(note.frontmatter.get("quality_status")) or extract_list_value(metadata.get("quality_status")))
        retrieval = set(extract_list_value(note.frontmatter.get("retrieval_status")) or extract_list_value(metadata.get("retrieval_status")))
        quality_counter.update(quality)
        if not str(note.frontmatter.get("feishu_doc_url") or "").strip():
            missing_feishu.append(note)
        if not path_from_uri(str(note.frontmatter.get("raw_md_path") or "")):
            missing_raw.append(note)
        if not path_from_uri(str(note.frontmatter.get("clean_md_path") or "")):
            missing_clean.append(note)
        if retrieval - {"searchable"}:
            retrieval_issues.append(note)
        company_scope = str(metadata.get("company_scope") or "")
        has_companies = bool(metadata.get("companies"))
        if has_companies:
            company_matched += 1
        elif company_scope == "not_applicable":
            company_not_applicable += 1
        elif company_scope == "needs_entity":
            company_needs_entity += 1
        if not metadata.get("themes") or (not has_companies and company_scope != "not_applicable") or metadata.get("metadata_confidence", 0) < 0.65:
            metadata_issues.append(note)
        if not metadata.get("themes"):
            unmatched_theme += 1
        if not has_companies and company_scope != "not_applicable":
            unmatched_company += 1
            for candidate in candidate_aliases_from_title(note.title):
                candidate_alias_notes[candidate].append(note)
        if not metadata.get("themes") and not has_companies and company_scope != "not_applicable":
            unmatched_entity += 1
        if "�" in note.title or "�" in note.path.name:
            dirty_titles.append(note)
        if "summary_failed" in quality or "feishu_failed" in quality or "summary_failed" in retrieval:
            failed_notes.append(note)
        if parse_note_datetime(note) >= cutoff_30:
            recent_notes.append(note)

    dupes = duplicate_groups(notes)
    summary = {
        "report_note_count": len(notes),
        "sqlite_report_count": db_scalar(db_path, "SELECT count(*) FROM reports"),
        "pdf_count": count_files(library_root / "pdfs", "*.pdf"),
        "summary_count": count_files(library_root / "summaries", "*.summary.md"),
        "raw_count": count_files(library_root / "markdown" / "raw", "*.md"),
        "clean_count": count_files(library_root / "markdown" / "clean", "*.md"),
        "missing_feishu": len(missing_feishu),
        "missing_raw": len(missing_raw),
        "missing_clean": len(missing_clean),
        "needs_review": quality_counter.get("needs_review", 0) + quality_counter.get("metadata_low_confidence", 0),
        "summary_failed": db_scalar(db_path, "SELECT count(*) FROM report_events WHERE status LIKE 'summary%failed%'")
        + db_scalar(db_path, "SELECT count(*) FROM reports WHERE index_status LIKE 'summary%failed%'"),
        "feishu_failed": db_scalar(db_path, "SELECT count(*) FROM report_events WHERE status LIKE 'feishu%failed%'")
        + db_scalar(db_path, "SELECT count(*) FROM reports WHERE index_status LIKE 'feishu%failed%'"),
        "duplicate_group_count": len(dupes),
        "dirty_title_count": len(dirty_titles),
        "company_matched": company_matched,
        "company_not_applicable": company_not_applicable,
        "company_needs_entity": company_needs_entity,
        "company_coverage_pct": round(company_matched / len(notes) * 100, 1) if notes else 0,
        "company_scope_covered": company_matched + company_not_applicable,
        "company_scope_coverage_pct": round((company_matched + company_not_applicable) / len(notes) * 100, 1) if notes else 0,
        "unmatched_theme": unmatched_theme,
        "unmatched_company": unmatched_company,
        "unmatched_entity": unmatched_entity,
        "recent_7d": sum(1 for note in notes if parse_note_datetime(note) >= cutoff_7),
        "recent_30d": sum(1 for note in notes if parse_note_datetime(note) >= cutoff_30),
    }
    buckets = {
        "retrieval_issues": retrieval_issues,
        "metadata_issues": metadata_issues,
        "candidate_aliases": sorted(
            [(name, len(items), items) for name, items in candidate_alias_notes.items()],
            key=lambda item: (-item[1], item[0]),
        ),
        "duplicates": dupes,
        "dirty_titles": dirty_titles,
        "failed_notes": failed_notes,
        "recent_notes": sorted(recent_notes, key=parse_note_datetime, reverse=True),
    }
    return summary, buckets


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate ResearchVault data-quality report.")
    parser.add_argument("--vault-root", default=str(DEFAULT_VAULT_ROOT))
    parser.add_argument("--library-root", default=str(DEFAULT_LIBRARY_ROOT))
    parser.add_argument("--config-root", default=str(DEFAULT_CONFIG_ROOT))
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--output", default="")
    parser.add_argument("--no-write", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    vault_root = Path(args.vault_root).expanduser().resolve(strict=False)
    library_root = Path(args.library_root).expanduser().resolve(strict=False)
    config_root = Path(args.config_root).expanduser().resolve(strict=False)
    db_path = Path(args.db_path).expanduser().resolve(strict=False)
    summary, buckets = build_health(vault_root=vault_root, library_root=library_root, config_root=config_root, db_path=db_path)
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    text = render_health_report(summary, buckets, generated_at)
    output = Path(args.output).expanduser() if args.output else vault_root / "99_维护" / "数据质量清单.md"
    if not args.no_write:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    print(json.dumps({"ok": True, "output": str(output), **summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
