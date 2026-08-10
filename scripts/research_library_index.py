#!/usr/bin/env python3
"""Small SQLite index for the local ZSXQ research library.

This script records metadata only. It does not decide whether download,
summary, or publishing tasks should run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from runtime_paths import DEFAULT_LIBRARY_ROOT
except ModuleNotFoundError:  # pragma: no cover
    from scripts.runtime_paths import DEFAULT_LIBRARY_ROOT

DEFAULT_DB_RELATIVE_PATH = Path("state/processed_files.sqlite")
SCHEMA_VERSION = 2


def library_root_from_arg(value: str | None) -> Path:
    return Path(value).expanduser() if value else DEFAULT_LIBRARY_ROOT


def db_path_for_library(library_root: Path) -> Path:
    return library_root / DEFAULT_DB_RELATIVE_PATH


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


def compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_report_id(pdf_sha256: str) -> str:
    value = str(pdf_sha256 or "").strip().lower()
    if len(value) < 16:
        raise ValueError("pdf_sha256 is required to build report_id")
    return f"zsxq_{value[:16]}"


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(db_path: Path) -> None:
    with closing(connect(db_path)) as conn:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reports (
                  report_id TEXT PRIMARY KEY,
                  source TEXT NOT NULL DEFAULT 'zsxq',
                  source_url TEXT NOT NULL DEFAULT '',
                  title TEXT NOT NULL DEFAULT '',
                  batch_id TEXT NOT NULL DEFAULT '',
                  pdf_path TEXT NOT NULL DEFAULT '',
                  pdf_sha256 TEXT NOT NULL DEFAULT '',
                  downloaded_at TEXT NOT NULL DEFAULT '',
                  raw_md_path TEXT NOT NULL DEFAULT '',
                  clean_md_path TEXT NOT NULL DEFAULT '',
                  summary_md_path TEXT NOT NULL DEFAULT '',
                  obsidian_note_path TEXT NOT NULL DEFAULT '',
                  feishu_doc_url TEXT NOT NULL DEFAULT '',
                  index_status TEXT NOT NULL DEFAULT '',
                  error_message TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_pdf_sha256
                ON reports(pdf_sha256)
                WHERE pdf_sha256 != ''
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS report_events (
                  event_id TEXT PRIMARY KEY,
                  report_id TEXT NOT NULL DEFAULT '',
                  pdf_sha256 TEXT NOT NULL DEFAULT '',
                  batch_id TEXT NOT NULL DEFAULT '',
                  stage TEXT NOT NULL DEFAULT '',
                  status TEXT NOT NULL DEFAULT '',
                  artifact_path TEXT NOT NULL DEFAULT '',
                  feishu_doc_url TEXT NOT NULL DEFAULT '',
                  error_message TEXT NOT NULL DEFAULT '',
                  run_id TEXT NOT NULL DEFAULT '',
                  batch_file TEXT NOT NULL DEFAULT '',
                  chunk_file TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_report_events_report_id
                ON report_events(report_id, created_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_report_events_pdf_sha256
                ON report_events(pdf_sha256, created_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_report_events_status
                ON report_events(status, created_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO metadata(key, value)
                VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(SCHEMA_VERSION),),
            )


REPORT_FIELDS = [
    "source",
    "source_url",
    "title",
    "batch_id",
    "pdf_path",
    "pdf_sha256",
    "downloaded_at",
    "raw_md_path",
    "clean_md_path",
    "summary_md_path",
    "obsidian_note_path",
    "feishu_doc_url",
    "index_status",
    "error_message",
]

EVENT_FIELDS = [
    "report_id",
    "pdf_sha256",
    "batch_id",
    "stage",
    "status",
    "artifact_path",
    "feishu_doc_url",
    "error_message",
    "run_id",
    "batch_file",
    "chunk_file",
]


def clean_payload(payload: dict[str, Any]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key in REPORT_FIELDS:
        value = str(payload.get(key, "") or "").strip()
        if value:
            cleaned[key] = value
    return cleaned


def upsert_report(db_path: Path, payload: dict[str, Any]) -> dict[str, str]:
    ensure_schema(db_path)
    raw_report_id = str(payload.get("report_id", "") or "").strip()
    pdf_sha256 = str(payload.get("pdf_sha256", "") or "").strip().lower()
    report_id = raw_report_id or (build_report_id(pdf_sha256) if pdf_sha256 else "")
    if not report_id:
        raise ValueError("report_id or pdf_sha256 is required")

    cleaned = clean_payload({**payload, "pdf_sha256": pdf_sha256})
    if "error_message" in payload and not str(payload.get("error_message", "") or "").strip():
        cleaned["error_message"] = ""
    updated_at = str(payload.get("updated_at", "") or "").strip() or now_iso()

    with closing(connect(db_path)) as conn:
        with conn:
            existing = conn.execute(
                "SELECT * FROM reports WHERE report_id = ?",
                (report_id,),
            ).fetchone()
            if existing is None and pdf_sha256:
                existing = conn.execute(
                    "SELECT * FROM reports WHERE pdf_sha256 = ?",
                    (pdf_sha256,),
                ).fetchone()
                if existing is not None:
                    report_id = str(existing["report_id"] or report_id)

            merged = {key: "" for key in REPORT_FIELDS}
            if existing is not None:
                merged.update({key: str(existing[key] or "") for key in REPORT_FIELDS})
            merged.update(cleaned)
            merged["updated_at"] = updated_at

            columns = ["report_id", *REPORT_FIELDS, "updated_at"]
            values = [report_id, *[merged.get(key, "") for key in REPORT_FIELDS], updated_at]
            assignments = ", ".join(f"{key}=excluded.{key}" for key in [*REPORT_FIELDS, "updated_at"])
            conn.execute(
                f"""
                INSERT INTO reports({", ".join(columns)})
                VALUES({", ".join("?" for _ in columns)})
                ON CONFLICT(report_id) DO UPDATE SET {assignments}
                """,
                values,
            )

    return {"report_id": report_id, **merged}


def stage_for_status(status: str) -> str:
    value = str(status or "").strip()
    if value.startswith("raw_md_"):
        return "raw_md"
    if value.startswith("clean_md_"):
        return "clean_md"
    if value.startswith("text_extract_"):
        return "text_extract"
    if value.startswith("summary_"):
        return "summary"
    if value.startswith("feishu_"):
        return "feishu"
    if value.startswith("obsidian_"):
        return "obsidian"
    if value.startswith("pdf_") or value == "downloaded":
        return "pdf"
    if value == "needs_review":
        return "review"
    return value


def clean_event_payload(payload: dict[str, Any]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    status = str(payload.get("status", "") or payload.get("index_status", "") or "").strip()
    stage = str(payload.get("stage", "") or "").strip() or stage_for_status(status)
    for key in EVENT_FIELDS:
        if key == "status":
            value = status
        elif key == "stage":
            value = stage
        else:
            value = str(payload.get(key, "") or "").strip()
        cleaned[key] = value
    return cleaned


def record_event(db_path: Path, payload: dict[str, Any]) -> dict[str, str]:
    """Append one trace event. This is metadata only and never drives scheduling."""

    ensure_schema(db_path)
    cleaned = clean_event_payload(payload)
    if not cleaned["status"]:
        raise ValueError("status is required")
    if not cleaned["report_id"] and not cleaned["pdf_sha256"]:
        raise ValueError("report_id or pdf_sha256 is required")

    event_id = str(payload.get("event_id", "") or "").strip() or uuid.uuid4().hex
    created_at = str(payload.get("created_at", "") or "").strip() or now_iso()

    with closing(connect(db_path)) as conn:
        with conn:
            if cleaned["pdf_sha256"] and (not cleaned["report_id"] or not cleaned["batch_id"]):
                existing = conn.execute(
                    "SELECT report_id, batch_id FROM reports WHERE pdf_sha256 = ?",
                    (cleaned["pdf_sha256"],),
                ).fetchone()
                if existing is not None:
                    if not cleaned["report_id"]:
                        cleaned["report_id"] = str(existing["report_id"] or "")
                    if not cleaned["batch_id"]:
                        cleaned["batch_id"] = str(existing["batch_id"] or "")
            columns = ["event_id", *EVENT_FIELDS, "created_at"]
            values = [event_id, *[cleaned[key] for key in EVENT_FIELDS], created_at]
            conn.execute(
                f"""
                INSERT INTO report_events({", ".join(columns)})
                VALUES({", ".join("?" for _ in columns)})
                """,
                values,
            )

    return {"event_id": event_id, **cleaned, "created_at": created_at}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def item_payload_from_batch_item(
    item: dict[str, Any],
    *,
    feishu_doc_url: str,
    index_status: str,
    error_message: str,
) -> dict[str, Any]:
    pdf_path = Path(str(item.get("path", "") or item.get("pdf_path", "")).strip()).expanduser()
    pdf_sha256 = str(item.get("pdf_sha256", "") or item.get("sha256", "") or item.get("text_extract_cache_key", "")).strip()
    if not pdf_sha256 and pdf_path.exists() and pdf_path.is_file():
        try:
            pdf_sha256 = compute_sha256(pdf_path)
        except OSError:
            pdf_sha256 = ""
    report_id = str(item.get("report_id", "") or "").strip() or (build_report_id(pdf_sha256) if pdf_sha256 else "")
    title = str(item.get("title", "") or Path(str(item.get("filename", ""))).stem).strip()
    return {
        "report_id": report_id,
        "source": "zsxq",
        "title": title,
        "batch_id": str(item.get("batch_id", "") or "").strip(),
        "pdf_path": str(pdf_path) if str(pdf_path) != "." else "",
        "pdf_sha256": pdf_sha256,
        "downloaded_at": str(item.get("downloaded_at", "") or item.get("modified_at", "") or "").strip(),
        "raw_md_path": str(item.get("raw_md_path", "") or "").strip(),
        "clean_md_path": str(item.get("clean_md_path", "") or "").strip(),
        "summary_md_path": str(item.get("summary_md_path", "") or "").strip(),
        "obsidian_note_path": str(item.get("obsidian_note_path", "") or "").strip(),
        "feishu_doc_url": feishu_doc_url or str(item.get("feishu_doc_url", "") or "").strip(),
        "index_status": index_status or str(item.get("index_status", "") or "").strip(),
        "error_message": error_message or str(item.get("error_message", "") or item.get("text_extract_error", "") or "").strip(),
    }


def artifact_path_for_status(item: dict[str, Any], status: str, feishu_doc_url: str) -> str:
    value = str(status or "").strip()
    if value.startswith("raw_md_"):
        return str(item.get("raw_md_path", "") or item.get("path", "") or item.get("pdf_path", "")).strip()
    if value.startswith("clean_md_"):
        return str(item.get("clean_md_path", "") or item.get("raw_md_path", "") or "").strip()
    if value.startswith("text_extract_"):
        return str(item.get("extracted_text_path", "") or item.get("path", "") or item.get("pdf_path", "")).strip()
    if value.startswith("summary_"):
        return str(item.get("summary_md_path", "") or "").strip()
    if value.startswith("feishu_"):
        return feishu_doc_url or str(item.get("feishu_doc_url", "") or item.get("summary_md_path", "") or "").strip()
    if value.startswith("obsidian_"):
        return str(item.get("obsidian_note_path", "") or item.get("summary_md_path", "") or "").strip()
    return str(item.get("path", "") or item.get("pdf_path", "") or item.get("archive_path", "")).strip()


def upsert_from_batch(
    db_path: Path,
    batch_file: Path,
    feishu_doc_url: str,
    index_status: str,
    error_message: str = "",
) -> list[dict[str, str]]:
    batch = load_json(batch_file)
    results: list[dict[str, str]] = []
    for item in batch.get("files", []):
        if not isinstance(item, dict):
            continue
        payload = item_payload_from_batch_item(
            item,
            feishu_doc_url=feishu_doc_url,
            index_status=index_status,
            error_message=error_message,
        )
        if not payload.get("report_id") and not payload.get("pdf_sha256"):
            continue
        results.append(upsert_report(db_path, payload))
    return results


def record_events_from_batch(
    db_path: Path,
    batch_file: Path,
    status: str,
    stage: str = "",
    artifact_path: str = "",
    feishu_doc_url: str = "",
    error_message: str = "",
    run_id: str = "",
    chunk_file: str = "",
) -> list[dict[str, str]]:
    batch = load_json(batch_file)
    results: list[dict[str, str]] = []
    for item in batch.get("files", []):
        if not isinstance(item, dict):
            continue
        base = item_payload_from_batch_item(
            item,
            feishu_doc_url=feishu_doc_url,
            index_status=status,
            error_message=error_message,
        )
        if not base.get("report_id") and not base.get("pdf_sha256"):
            continue
        results.append(
            record_event(
                db_path,
                {
                    "report_id": base.get("report_id", ""),
                    "pdf_sha256": base.get("pdf_sha256", ""),
                    "batch_id": base.get("batch_id", ""),
                    "stage": stage,
                    "status": status,
                    "artifact_path": artifact_path or artifact_path_for_status(item, status, feishu_doc_url),
                    "feishu_doc_url": feishu_doc_url or base.get("feishu_doc_url", ""),
                    "error_message": error_message or base.get("error_message", ""),
                    "run_id": run_id,
                    "batch_file": str(batch_file),
                    "chunk_file": chunk_file or str(batch_file),
                },
            )
        )
    return results


def record_text_extract_events_from_batch(
    db_path: Path,
    batch_file: Path,
    run_id: str = "",
    chunk_file: str = "",
) -> list[dict[str, str]]:
    batch = load_json(batch_file)
    results: list[dict[str, str]] = []
    for item in batch.get("files", []):
        if not isinstance(item, dict):
            continue
        text_status = str(item.get("text_extract_status", "") or "").strip()
        text_source = str(item.get("text_source", "") or "").strip()
        if text_status == "success" and text_source and text_source != "markitdown_clean":
            status = "text_extract_fallback_success"
        elif text_status == "failed":
            status = "text_extract_failed"
        else:
            continue
        base = item_payload_from_batch_item(
            item,
            feishu_doc_url="",
            index_status=status,
            error_message=str(item.get("text_extract_error", "") or "").strip(),
        )
        if not base.get("report_id") and not base.get("pdf_sha256"):
            continue
        results.append(
            record_event(
                db_path,
                {
                    "report_id": base.get("report_id", ""),
                    "pdf_sha256": base.get("pdf_sha256", ""),
                    "batch_id": base.get("batch_id", ""),
                    "status": status,
                    "artifact_path": artifact_path_for_status(item, status, ""),
                    "error_message": base.get("error_message", ""),
                    "run_id": run_id,
                    "batch_file": str(batch_file),
                    "chunk_file": chunk_file or str(batch_file),
                },
            )
        )
    return results


def needs_text_extract_fallback(item: dict[str, Any]) -> bool:
    text_status = str(item.get("text_extract_status", "") or "").strip()
    text_source = str(item.get("text_source", "") or "").strip()
    prepared_text_path = Path(str(item.get("extracted_text_path", "") or "")).expanduser()
    return not (text_status == "success" and text_source == "markitdown_clean" and prepared_text_path.exists())


def record_text_extract_started_events_from_batch(
    db_path: Path,
    batch_file: Path,
    run_id: str = "",
    chunk_file: str = "",
) -> list[dict[str, str]]:
    batch = load_json(batch_file)
    results: list[dict[str, str]] = []
    for item in batch.get("files", []):
        if not isinstance(item, dict) or not needs_text_extract_fallback(item):
            continue
        base = item_payload_from_batch_item(
            item,
            feishu_doc_url="",
            index_status="text_extract_fallback_started",
            error_message="",
        )
        if not base.get("report_id") and not base.get("pdf_sha256"):
            continue
        results.append(
            record_event(
                db_path,
                {
                    "report_id": base.get("report_id", ""),
                    "pdf_sha256": base.get("pdf_sha256", ""),
                    "batch_id": base.get("batch_id", ""),
                    "status": "text_extract_fallback_started",
                    "artifact_path": artifact_path_for_status(item, "text_extract_fallback_started", ""),
                    "run_id": run_id,
                    "batch_file": str(batch_file),
                    "chunk_file": chunk_file or str(batch_file),
                },
            )
        )
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Maintain the local ResearchLibrary SQLite index.")
    parser.add_argument("--library-root", default="")
    parser.add_argument(
        "--database",
        default="",
        help="Optional absolute index path outside TCC-protected folders for background runtimes.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="Create or migrate the index database.")

    upsert_parser = subparsers.add_parser("upsert", help="Upsert one report metadata row.")
    upsert_parser.add_argument("--report-id", default="")
    upsert_parser.add_argument("--pdf-sha256", default="")
    upsert_parser.add_argument("--title", default="")
    upsert_parser.add_argument("--pdf-path", default="")
    upsert_parser.add_argument("--raw-md-path", default="")
    upsert_parser.add_argument("--clean-md-path", default="")
    upsert_parser.add_argument("--summary-md-path", default="")
    upsert_parser.add_argument("--obsidian-note-path", default="")
    upsert_parser.add_argument("--feishu-doc-url", default="")
    upsert_parser.add_argument("--index-status", default="")
    upsert_parser.add_argument("--error-message", default="")

    batch_parser = subparsers.add_parser("upsert-from-batch", help="Upsert report rows from a batch JSON file.")
    batch_parser.add_argument("--batch-file", required=True)
    batch_parser.add_argument("--feishu-doc-url", default="")
    batch_parser.add_argument("--index-status", default="")
    batch_parser.add_argument("--error-message", default="")

    event_parser = subparsers.add_parser("record-event", help="Append one report trace event.")
    event_parser.add_argument("--report-id", default="")
    event_parser.add_argument("--pdf-sha256", default="")
    event_parser.add_argument("--batch-id", default="")
    event_parser.add_argument("--stage", default="")
    event_parser.add_argument("--status", required=True)
    event_parser.add_argument("--artifact-path", default="")
    event_parser.add_argument("--feishu-doc-url", default="")
    event_parser.add_argument("--error-message", default="")
    event_parser.add_argument("--run-id", default="")
    event_parser.add_argument("--batch-file", default="")
    event_parser.add_argument("--chunk-file", default="")

    event_batch_parser = subparsers.add_parser("record-events-from-batch", help="Append one trace event for each report in a batch JSON file.")
    event_batch_parser.add_argument("--batch-file", required=True)
    event_batch_parser.add_argument("--stage", default="")
    event_batch_parser.add_argument("--status", required=True)
    event_batch_parser.add_argument("--artifact-path", default="")
    event_batch_parser.add_argument("--feishu-doc-url", default="")
    event_batch_parser.add_argument("--error-message", default="")
    event_batch_parser.add_argument("--run-id", default="")
    event_batch_parser.add_argument("--chunk-file", default="")

    text_event_parser = subparsers.add_parser("record-text-extract-events", help="Append text extraction trace events from a batch JSON file.")
    text_event_parser.add_argument("--batch-file", required=True)
    text_event_parser.add_argument("--run-id", default="")
    text_event_parser.add_argument("--chunk-file", default="")

    text_started_parser = subparsers.add_parser("record-text-extract-started-events", help="Append fallback-started trace events for reports that still need the existing extractor.")
    text_started_parser.add_argument("--batch-file", required=True)
    text_started_parser.add_argument("--run-id", default="")
    text_started_parser.add_argument("--chunk-file", default="")

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    library_root = library_root_from_arg(args.library_root)
    db_path = (
        Path(args.database).expanduser().resolve(strict=False)
        if str(args.database).strip()
        else db_path_for_library(library_root)
    )

    if args.command == "init":
        ensure_schema(db_path)
        print(json.dumps({"ok": True, "db_path": str(db_path)}, ensure_ascii=False))
        return 0

    if args.command == "upsert":
        payload = {
            "report_id": args.report_id,
            "pdf_sha256": args.pdf_sha256,
            "title": args.title,
            "pdf_path": args.pdf_path,
            "raw_md_path": args.raw_md_path,
            "clean_md_path": args.clean_md_path,
            "summary_md_path": args.summary_md_path,
            "obsidian_note_path": args.obsidian_note_path,
            "feishu_doc_url": args.feishu_doc_url,
            "index_status": args.index_status,
            "error_message": args.error_message,
        }
        result = upsert_report(db_path, payload)
        print(json.dumps({"ok": True, "db_path": str(db_path), "report": result}, ensure_ascii=False))
        return 0

    if args.command == "upsert-from-batch":
        results = upsert_from_batch(
            db_path=db_path,
            batch_file=Path(args.batch_file).expanduser().resolve(),
            feishu_doc_url=args.feishu_doc_url.strip(),
            index_status=args.index_status.strip(),
            error_message=args.error_message.strip(),
        )
        print(json.dumps({"ok": True, "db_path": str(db_path), "count": len(results)}, ensure_ascii=False))
        return 0

    if args.command == "record-event":
        result = record_event(
            db_path,
            {
                "report_id": args.report_id,
                "pdf_sha256": args.pdf_sha256,
                "batch_id": args.batch_id,
                "stage": args.stage,
                "status": args.status,
                "artifact_path": args.artifact_path,
                "feishu_doc_url": args.feishu_doc_url,
                "error_message": args.error_message,
                "run_id": args.run_id,
                "batch_file": args.batch_file,
                "chunk_file": args.chunk_file,
            },
        )
        print(json.dumps({"ok": True, "db_path": str(db_path), "event": result}, ensure_ascii=False))
        return 0

    if args.command == "record-events-from-batch":
        results = record_events_from_batch(
            db_path=db_path,
            batch_file=Path(args.batch_file).expanduser().resolve(),
            stage=args.stage.strip(),
            status=args.status.strip(),
            artifact_path=args.artifact_path.strip(),
            feishu_doc_url=args.feishu_doc_url.strip(),
            error_message=args.error_message.strip(),
            run_id=args.run_id.strip(),
            chunk_file=args.chunk_file.strip(),
        )
        print(json.dumps({"ok": True, "db_path": str(db_path), "count": len(results)}, ensure_ascii=False))
        return 0

    if args.command == "record-text-extract-events":
        results = record_text_extract_events_from_batch(
            db_path=db_path,
            batch_file=Path(args.batch_file).expanduser().resolve(),
            run_id=args.run_id.strip(),
            chunk_file=args.chunk_file.strip(),
        )
        print(json.dumps({"ok": True, "db_path": str(db_path), "count": len(results)}, ensure_ascii=False))
        return 0

    if args.command == "record-text-extract-started-events":
        results = record_text_extract_started_events_from_batch(
            db_path=db_path,
            batch_file=Path(args.batch_file).expanduser().resolve(),
            run_id=args.run_id.strip(),
            chunk_file=args.chunk_file.strip(),
        )
        print(json.dumps({"ok": True, "db_path": str(db_path), "count": len(results)}, ensure_ascii=False))
        return 0

    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
