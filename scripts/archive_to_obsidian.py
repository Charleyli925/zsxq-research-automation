#!/usr/bin/env python3
"""Create simple Obsidian notes for processed ZSXQ reports."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from research_library_index import db_path_for_library, record_event, upsert_report
except ModuleNotFoundError:  # pragma: no cover
    from scripts.research_library_index import db_path_for_library, record_event, upsert_report

try:
    from kb_common import (
        extract_report_metadata,
        load_kb_configs,
        load_note,
        merge_metadata_frontmatter,
        render_frontmatter,
        split_frontmatter,
        upsert_metadata,
    )
except ModuleNotFoundError:  # pragma: no cover
    from scripts.kb_common import (
        extract_report_metadata,
        load_kb_configs,
        load_note,
        merge_metadata_frontmatter,
        render_frontmatter,
        split_frontmatter,
        upsert_metadata,
    )

try:
    from runtime_paths import DEFAULT_LIBRARY_ROOT, DEFAULT_VAULT_ROOT
except ModuleNotFoundError:  # pragma: no cover
    from scripts.runtime_paths import DEFAULT_LIBRARY_ROOT, DEFAULT_VAULT_ROOT


def artifact_batch_id(item: dict[str, Any], library_root: Path) -> str:
    existing = str(item.get("batch_id", "") or "").strip()
    if existing:
        return existing
    pdf_path = Path(str(item.get("path", "") or item.get("pdf_path", "") or "")).expanduser()
    try:
        relative = pdf_path.resolve(strict=False).relative_to(
            (library_root / "pdfs").expanduser().resolve(strict=False)
        )
        if len(relative.parts) >= 2:
            return relative.parts[0]
    except ValueError:
        pass
    parent_name = pdf_path.parent.name
    if "__to__" in parent_name:
        return parent_name
    raw = str(item.get("modified_at", "") or item.get("downloaded_at", "") or "").strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return "manual"
    return f"{dt.strftime('%Y-%m-%d_%H-%M-%S')}__to__{dt.strftime('%Y-%m-%d_%H-%M-%S')}"


def artifact_stem(item: dict[str, Any]) -> str:
    filename = str(item.get("filename", "") or "").strip()
    if filename:
        return Path(filename).stem
    path = str(item.get("path", "") or item.get("pdf_path", "") or "report").strip()
    return Path(path).stem or "report"


def report_id_for_item(item: dict[str, Any]) -> str:
    existing = str(item.get("report_id", "") or "").strip()
    if existing:
        return existing
    raw = str(item.get("pdf_sha256", "") or item.get("text_extract_cache_key", "") or "").strip()
    if len(raw) >= 16:
        return f"zsxq_{raw[:16]}"
    return Path(str(item.get("filename", "") or "report")).stem


def file_uri(path: str) -> str:
    value = str(path or "").strip()
    if not value:
        return ""
    return Path(value).expanduser().resolve(strict=False).as_uri()


def note_path_for_item(vault_root: Path, library_root: Path, item: dict[str, Any]) -> Path:
    batch_id = artifact_batch_id(item, library_root)
    stem = artifact_stem(item)
    return vault_root / "10_Reports" / batch_id / f"{stem}.md"


def frontmatter_value(value: str) -> str:
    return json.dumps(str(value or ""), ensure_ascii=False)


def render_note(item: dict[str, Any], feishu_doc_url: str) -> str:
    title = str(item.get("title", "") or Path(str(item.get("filename", "") or "未命名研报")).stem).strip()
    summary_path = str(item.get("summary_md_path", "") or "").strip()
    summary_text = ""
    if summary_path and Path(summary_path).exists():
        summary_text = Path(summary_path).read_text(encoding="utf-8", errors="replace").strip()
    pdf_uri = file_uri(str(item.get("path", "") or item.get("pdf_path", "")))
    raw_uri = file_uri(str(item.get("raw_md_path", "")))
    clean_uri = file_uri(str(item.get("clean_md_path", "")))
    summary_uri = file_uri(summary_path)
    doc_url = feishu_doc_url or str(item.get("feishu_doc_url", "") or "").strip()

    lines = [
        "---",
        "type: report",
        "source: zsxq",
        "source_tag: 外资研报",
        f"report_id: {frontmatter_value(report_id_for_item(item))}",
        f"title: {frontmatter_value(title)}",
        f"downloaded_at: {frontmatter_value(str(item.get('modified_at', '') or item.get('downloaded_at', '')))}",
        f"pdf_path: {frontmatter_value(pdf_uri)}",
        f"raw_md_path: {frontmatter_value(raw_uri)}",
        f"clean_md_path: {frontmatter_value(clean_uri)}",
        f"summary_md_path: {frontmatter_value(summary_uri)}",
        f"feishu_doc_url: {frontmatter_value(doc_url)}",
        "status: obsidian_archived",
        "reviewed: false",
        "---",
        "",
        f"# {title}",
        "",
        "## 本地文件",
        "",
        f"- PDF: [打开 PDF]({pdf_uri})" if pdf_uri else "- PDF: ",
        f"- Raw Markdown: [打开 raw.md]({raw_uri})" if raw_uri else "- Raw Markdown: ",
        f"- Clean Markdown: [打开 clean.md]({clean_uri})" if clean_uri else "- Clean Markdown: ",
        f"- Summary Markdown: [打开 summary.md]({summary_uri})" if summary_uri else "- Summary Markdown: ",
        f"- 飞书文档: {doc_url}",
        "",
        "## 摘要",
        "",
        summary_text,
        "",
    ]
    return "\n".join(lines)


def enrich_note_metadata(
    note_path: Path,
    vault_root: Path,
    library_root: Path,
    library_database: Path | None = None,
) -> dict[str, Any]:
    configs = load_kb_configs(library_root / "config")
    note = load_note(note_path, vault_root)
    metadata = extract_report_metadata(note, configs, vault_root)
    text = note_path.read_text(encoding="utf-8", errors="replace")
    frontmatter, body = split_frontmatter(text)
    merged, changed_fields = merge_metadata_frontmatter(frontmatter, metadata, force=True)
    if changed_fields:
        note_path.write_text(render_frontmatter(merged, body), encoding="utf-8")
        note = load_note(note_path, vault_root)
    upsert_metadata(library_database or db_path_for_library(library_root), note, metadata, "archive_to_obsidian")
    return metadata


def archive_batch(
    batch_file: Path,
    library_root: Path,
    vault_root: Path,
    feishu_doc_url: str,
    library_database: Path | None = None,
) -> dict[str, Any]:
    batch = json.loads(batch_file.read_text(encoding="utf-8"))
    files = batch.get("files", [])
    if not isinstance(files, list):
        raise SystemExit("invalid batch json: files must be a list")

    archived_count = 0
    for item in files:
        if not isinstance(item, dict):
            continue
        summary_path = str(item.get("summary_md_path", "") or "").strip()
        if not summary_path or not Path(summary_path).exists():
            continue
        note_path = note_path_for_item(vault_root, library_root, item)
        item["batch_id"] = artifact_batch_id(item, library_root)
        try:
            note_path.parent.mkdir(parents=True, exist_ok=True)
            note_path.write_text(render_note(item, feishu_doc_url), encoding="utf-8")
            metadata = enrich_note_metadata(note_path, vault_root, library_root, library_database)
            item["obsidian_note_path"] = str(note_path)
            item["feishu_doc_url"] = feishu_doc_url or str(item.get("feishu_doc_url", "") or "")
            item["metadata_confidence"] = metadata.get("metadata_confidence", 0)
            item["metadata_status"] = metadata.get("metadata_status", "")
            archived_count += 1
            db_path = library_database or db_path_for_library(library_root)
            upsert_report(
                db_path,
                {
                    "report_id": report_id_for_item(item),
                    "pdf_sha256": str(item.get("pdf_sha256", "") or item.get("text_extract_cache_key", "") or ""),
                    "title": str(item.get("title", "") or Path(str(item.get("filename", ""))).stem),
                    "pdf_path": str(item.get("path", "") or item.get("pdf_path", "")),
                    "raw_md_path": str(item.get("raw_md_path", "") or ""),
                    "clean_md_path": str(item.get("clean_md_path", "") or ""),
                    "summary_md_path": summary_path,
                    "obsidian_note_path": str(note_path),
                    "feishu_doc_url": item["feishu_doc_url"],
                    "index_status": "obsidian_archived",
                    "error_message": "",
                },
            )
            try:
                record_event(
                    db_path,
                    {
                        "report_id": report_id_for_item(item),
                        "pdf_sha256": str(item.get("pdf_sha256", "") or item.get("text_extract_cache_key", "") or ""),
                        "batch_id": str(item.get("batch_id", "") or ""),
                        "status": "obsidian_archived",
                        "artifact_path": str(note_path),
                        "feishu_doc_url": item["feishu_doc_url"],
                    },
                )
            except Exception:
                pass
        except Exception as exc:
            item["obsidian_error"] = str(exc)
            try:
                db_path = library_database or db_path_for_library(library_root)
                upsert_report(
                    db_path,
                    {
                        "report_id": report_id_for_item(item),
                        "pdf_sha256": str(item.get("pdf_sha256", "") or item.get("text_extract_cache_key", "") or ""),
                        "title": str(item.get("title", "") or Path(str(item.get("filename", ""))).stem),
                        "pdf_path": str(item.get("path", "") or item.get("pdf_path", "")),
                        "summary_md_path": summary_path,
                        "obsidian_note_path": str(note_path),
                        "feishu_doc_url": feishu_doc_url or str(item.get("feishu_doc_url", "") or ""),
                        "index_status": "obsidian_failed",
                        "error_message": str(exc),
                    },
                )
                try:
                    record_event(
                        db_path,
                        {
                            "report_id": report_id_for_item(item),
                            "pdf_sha256": str(item.get("pdf_sha256", "") or item.get("text_extract_cache_key", "") or ""),
                            "batch_id": str(item.get("batch_id", "") or ""),
                            "status": "obsidian_failed",
                            "artifact_path": str(note_path),
                            "feishu_doc_url": feishu_doc_url or str(item.get("feishu_doc_url", "") or ""),
                            "error_message": str(exc),
                        },
                    )
                except Exception:
                    pass
            except Exception:
                pass

    batch["files"] = files
    batch_file.write_text(json.dumps(batch, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"batch_file": str(batch_file), "archived_count": archived_count}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive ZSXQ summaries to Obsidian.")
    parser.add_argument("--batch-file", required=True)
    parser.add_argument("--library-root", default=str(DEFAULT_LIBRARY_ROOT))
    parser.add_argument("--library-database", default="")
    parser.add_argument("--vault-root", default=str(DEFAULT_VAULT_ROOT))
    parser.add_argument("--feishu-doc-url", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = archive_batch(
        batch_file=Path(args.batch_file).expanduser().resolve(),
        library_root=Path(args.library_root).expanduser(),
        vault_root=Path(args.vault_root).expanduser(),
        feishu_doc_url=args.feishu_doc_url.strip(),
        library_database=(
            Path(args.library_database).expanduser().resolve(strict=False)
            if args.library_database.strip()
            else None
        ),
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
