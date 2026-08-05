#!/usr/bin/env python3
"""Backfill report metadata into Obsidian notes and SQLite."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from kb_common import (
    DEFAULT_CONFIG_ROOT,
    DEFAULT_DB_PATH,
    DEFAULT_LIBRARY_ROOT,
    DEFAULT_VAULT_ROOT,
    NoteRecord,
    extract_list_value,
    extract_report_metadata,
    file_uri,
    load_kb_configs,
    load_report_notes,
    merge_metadata_frontmatter,
    render_frontmatter,
    split_frontmatter,
    stable_report_id,
    upsert_metadata,
    write_text_if_changed,
)


PATH_FIELDS = ["pdf_path", "raw_md_path", "clean_md_path", "summary_md_path", "feishu_doc_url"]
LINK_TARGET_RE = re.compile(r"\[\[([^|\]]+)(?:\|[^\]]*)?\]\]")


def load_page_memberships(vault_root: Path) -> dict[str, dict[str, set[str]]]:
    memberships: dict[str, dict[str, set[str]]] = {}
    for kind, folder in (("companies", "30_Companies"), ("themes", "20_Themes")):
        for page_path in sorted((vault_root / folder).glob("*.md")):
            if page_path.name in {"公司索引.md", "主题索引.md"}:
                continue
            text = page_path.read_text(encoding="utf-8", errors="replace")
            for target in LINK_TARGET_RE.findall(text):
                if not target.startswith("10_Reports/"):
                    continue
                item = memberships.setdefault(target, {"companies": set(), "themes": set()})
                item[kind].add(page_path.stem)
    return memberships


def apply_membership_metadata(metadata: dict[str, Any], membership: dict[str, set[str]] | None) -> dict[str, Any]:
    if not membership:
        return metadata
    merged = dict(metadata)
    # Company pages are generated from metadata, so feeding their report links
    # back into company extraction creates a circular false-positive loop.
    for key in ("themes",):
        values = [*extract_list_value(merged.get(key)), *sorted(membership.get(key, set()))]
        merged[key] = list(dict.fromkeys(values))
    if merged.get("companies") and merged.get("themes"):
        merged["metadata_confidence"] = max(float(merged.get("metadata_confidence") or 0), 0.75)
        merged["metadata_status"] = "metadata_ready"
    return merged


def load_report_rows(db_path: Path) -> dict[str, dict[str, Any]]:
    if not db_path.exists():
        return {}
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM reports").fetchall()
    return {str(row["report_id"]): dict(row) for row in rows}


def add_db_paths(frontmatter: dict[str, Any], row: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    if not row:
        return frontmatter, []
    merged = dict(frontmatter)
    changed: list[str] = []
    for key in PATH_FIELDS:
        current = str(merged.get(key) or "").strip()
        value = str(row.get(key) or "").strip()
        if current or not value:
            continue
        merged[key] = file_uri(value) if key.endswith("_path") else value
        changed.append(key)
    return merged, changed


def process_note(
    note: NoteRecord,
    *,
    configs: dict[str, Any],
    db_row: dict[str, Any] | None,
    db_path: Path,
    dry_run: bool,
    force: bool,
    vault_root: Path,
    membership: dict[str, set[str]] | None,
) -> dict[str, Any]:
    text = note.path.read_text(encoding="utf-8", errors="replace")
    frontmatter, body = split_frontmatter(text)
    with_paths, path_changes = add_db_paths(frontmatter, db_row)
    extract_frontmatter = dict(with_paths)
    if force:
        for key in (
            "broker",
            "report_date",
            "companies",
            "tickers",
            "themes",
            "subthemes",
            "regions",
            "industries",
            "key_numbers",
            "ratings",
            "target_prices",
            "risks",
            "catalysts",
            "report_type",
            "company_scope",
            "quality_status",
            "retrieval_status",
            "metadata_confidence",
            "metadata_status",
        ):
            extract_frontmatter.pop(key, None)
    note_for_extract = NoteRecord(
        path=note.path,
        frontmatter=extract_frontmatter,
        body=body,
        title=note.title,
        display_title=note.display_title,
        link_target=note.link_target,
    )
    metadata = extract_report_metadata(note_for_extract, configs, vault_root)
    metadata = apply_membership_metadata(metadata, membership)
    merged, metadata_changes = merge_metadata_frontmatter(with_paths, metadata, force=force)
    changed_fields = [*path_changes, *metadata_changes]
    if changed_fields and not dry_run:
        write_text_if_changed(note.path, render_frontmatter(merged, body))
    note_for_db = NoteRecord(
        path=note.path,
        frontmatter=merged,
        body=body,
        title=note.title,
        display_title=note.display_title,
        link_target=note.link_target,
    )
    if not dry_run:
        upsert_metadata(db_path, note_for_db, metadata, "backfill_report_metadata")
    return {
        "report_id": stable_report_id(note_for_db),
        "note_path": str(note.path),
        "changed_fields": changed_fields,
        "broker": metadata.get("broker", ""),
        "report_date": metadata.get("report_date", ""),
        "companies": metadata.get("companies", []),
        "themes": metadata.get("themes", []),
        "report_type": metadata.get("report_type", ""),
        "company_scope": metadata.get("company_scope", ""),
        "metadata_confidence": metadata.get("metadata_confidence", 0),
        "metadata_status": metadata.get("metadata_status", ""),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill ResearchVault report metadata.")
    parser.add_argument("--vault-root", default=str(DEFAULT_VAULT_ROOT))
    parser.add_argument("--library-root", default=str(DEFAULT_LIBRARY_ROOT))
    parser.add_argument("--config-root", default=str(DEFAULT_CONFIG_ROOT))
    parser.add_argument("--db-path", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--force", action="store_true", help="Overwrite existing metadata fields.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    vault_root = Path(args.vault_root).expanduser().resolve(strict=False)
    library_root = Path(args.library_root).expanduser().resolve(strict=False)
    db_path = Path(args.db_path).expanduser().resolve(strict=False) if args.db_path else library_root / "state" / "processed_files.sqlite"
    configs = load_kb_configs(Path(args.config_root).expanduser().resolve(strict=False))
    rows = load_report_rows(db_path)
    memberships = load_page_memberships(vault_root)
    notes = load_report_notes(vault_root)
    if args.limit > 0:
        notes = notes[: args.limit]

    results: list[dict[str, Any]] = []
    for note in notes:
        row = rows.get(stable_report_id(note))
        results.append(
            process_note(
                note,
                configs=configs,
                db_row=row,
                db_path=db_path,
                dry_run=args.dry_run,
                force=args.force,
                vault_root=vault_root,
                membership=memberships.get(note.link_target),
            )
        )

    total = len(results)
    coverage = {
        "broker": sum(1 for item in results if item["broker"]),
        "report_date": sum(1 for item in results if item["report_date"]),
        "companies": sum(1 for item in results if extract_list_value(item["companies"])),
        "company_scope_covered": sum(
            1
            for item in results
            if extract_list_value(item["companies"]) or item.get("company_scope") == "not_applicable"
        ),
        "themes": sum(1 for item in results if extract_list_value(item["themes"])),
    }
    by_report_type: dict[str, int] = {}
    by_company_scope: dict[str, int] = {}
    for item in results:
        report_type = str(item.get("report_type") or "unknown")
        company_scope = str(item.get("company_scope") or "unknown")
        by_report_type[report_type] = by_report_type.get(report_type, 0) + 1
        by_company_scope[company_scope] = by_company_scope.get(company_scope, 0) + 1
    summary = {
        "ok": True,
        "dry_run": args.dry_run,
        "processed": total,
        "changed_notes": sum(1 for item in results if item["changed_fields"]),
        "low_confidence": sum(1 for item in results if item["metadata_status"] == "metadata_low_confidence"),
        "coverage": coverage,
        "coverage_pct": {key: round(value / total * 100, 1) if total else 0 for key, value in coverage.items()},
        "by_report_type": dict(sorted(by_report_type.items())),
        "by_company_scope": dict(sorted(by_company_scope.items())),
        "sample_changed": [item for item in results if item["changed_fields"]][:20],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
