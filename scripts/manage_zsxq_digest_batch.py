#!/usr/bin/env python3
"""
This file handles the small helper steps around the ZSXQ PDF digest batch.

Relation to other files:
- `scan_new_zsxq_pdfs.py` finds which PDFs are still waiting to be summarized.
- `run.sh` calls this helper to split a big batch into small chunks.
- `summary_prompt.md` is the template; this helper fills in the real file paths.
- `extract_pdf_text.py` writes the cleaned text path that this helper checks before summary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import tempfile
import unicodedata
from pathlib import Path
from typing import Any
from datetime import datetime, timedelta
from urllib.parse import urlparse

try:
    from research_library_index import db_path_for_library, record_event, upsert_report
except ModuleNotFoundError:  # pragma: no cover
    try:
        from scripts.research_library_index import db_path_for_library, record_event, upsert_report
    except ModuleNotFoundError:  # pragma: no cover
        db_path_for_library = None  # type: ignore[assignment]
        record_event = None  # type: ignore[assignment]
        upsert_report = None  # type: ignore[assignment]

SUMMARY_PREFIX = "ZSXQ_SUMMARY_JSON:"
SUMMARY_CACHE_VERSION = os.environ.get("ZSXQ_SUMMARY_CACHE_VERSION", "2026-03-28-v1").strip() or "2026-03-28-v1"
MAX_SUMMARY_INPUT_LINE_CHARS = 1200


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Helper commands for ZSXQ PDF digest batches.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    split_parser = subparsers.add_parser("split", help="Split one batch JSON into small chunk JSON files.")
    split_parser.add_argument("--batch-file", required=True)
    split_parser.add_argument("--output-dir", required=True)
    split_parser.add_argument("--chunk-size", type=int, required=True)

    render_parser = subparsers.add_parser("render-prompt", help="Fill the prompt template for one chunk.")
    render_parser.add_argument("--template", required=True)
    render_parser.add_argument("--batch-file", required=True)
    render_parser.add_argument("--system-prompt-file", default="")
    render_parser.add_argument("--output", required=True)

    validate_summary_parser = subparsers.add_parser("validate-summary", help="Validate one local-summary result file.")
    validate_summary_parser.add_argument("--batch-file", required=True)
    validate_summary_parser.add_argument("--result-file", required=True)

    persist_summary_parser = subparsers.add_parser("persist-summary", help="Save one summary result into local markdown/json artifacts.")
    persist_summary_parser.add_argument("--batch-file", required=True)
    persist_summary_parser.add_argument("--result-file", required=True)
    persist_summary_parser.add_argument("--summary-cache-dir", required=True)
    persist_summary_parser.add_argument("--output-json", required=True)
    persist_summary_parser.add_argument("--output-markdown", required=True)

    materialize_summary_parser = subparsers.add_parser("materialize-summary-cache", help="Build one chunk summary file from cached per-file summaries.")
    materialize_summary_parser.add_argument("--batch-file", required=True)
    materialize_summary_parser.add_argument("--summary-cache-dir", required=True)
    materialize_summary_parser.add_argument("--output-json", required=True)
    materialize_summary_parser.add_argument("--output-markdown", required=True)

    publish_groups_parser = subparsers.add_parser(
        "build-publish-groups",
        help="Build Feishu publish groups from chunks with local summaries.",
    )
    publish_groups_parser.add_argument("--output-dir", required=True)
    publish_groups_parser.add_argument("--doc-group-size", type=int, required=True)
    publish_groups_parser.add_argument("--doc-group-threshold", type=int, required=True)
    publish_groups_parser.add_argument("--total-file-count", type=int, required=True)
    publish_groups_parser.add_argument("--group-start-index", type=int, default=0)
    publish_groups_parser.add_argument("--group-total", type=int, default=0)
    publish_groups_parser.add_argument("chunk_files", nargs="+")

    ready_parser = subparsers.add_parser("check-text-ready", help="Check whether one chunk already has usable text files.")
    ready_parser.add_argument("--batch-file", required=True)

    inspect_parser = subparsers.add_parser("inspect-output", help="Extract normalized text and usage from one agent output file.")
    inspect_parser.add_argument("--result-file", required=True)

    create_markdown_parser = subparsers.add_parser(
        "build-lark-cli-create-markdown",
        help="Build markdown for lark-cli docs create.",
    )
    create_markdown_parser.add_argument("--batch-file", required=True)
    create_markdown_parser.add_argument("--summary-markdown", required=True)
    create_markdown_parser.add_argument("--output", required=True)

    doc_title_parser = subparsers.add_parser("build-doc-title", help="Print the fixed Feishu document title.")
    doc_title_parser.add_argument("--batch-file", required=True)

    publish_key_parser = subparsers.add_parser("build-publish-key", help="Build a stable publish dedupe key.")
    publish_key_parser.add_argument("--batch-file", required=True)
    publish_key_parser.add_argument("--summary-markdown", required=True)
    publish_key_parser.add_argument("--target-doc-url", default="")

    lookup_publish_parser = subparsers.add_parser("lookup-publish-record", help="Find a successful publish record.")
    lookup_publish_parser.add_argument("--records-file", required=True)
    lookup_publish_parser.add_argument("--publish-key", required=True)

    lookup_publish_recovery_parser = subparsers.add_parser(
        "lookup-publish-recovery",
        help="Find a successful or remote-written publish transition for the same batch and summary.",
    )
    lookup_publish_recovery_parser.add_argument("--records-file", required=True)
    lookup_publish_recovery_parser.add_argument("--batch-hash", required=True)
    lookup_publish_recovery_parser.add_argument("--summary-hash", required=True)

    lookup_same_day_parser = subparsers.add_parser(
        "lookup-latest-same-day-doc",
        help="Find the latest same-day document with enough remaining file capacity.",
    )
    lookup_same_day_parser.add_argument("--records-file", required=True)
    lookup_same_day_parser.add_argument("--batch-file", required=True)
    lookup_same_day_parser.add_argument("--incoming-file-count", type=int, required=True)
    lookup_same_day_parser.add_argument("--max-file-count", type=int, default=20)
    lookup_same_day_parser.add_argument("--legacy-file-count", type=int, default=10)

    append_publish_parser = subparsers.add_parser("append-publish-record", help="Append one publish record to JSONL.")
    append_publish_parser.add_argument("--records-file", required=True)
    append_publish_parser.add_argument("--publish-key", required=True)
    append_publish_parser.add_argument("--batch-file", required=True)
    append_publish_parser.add_argument("--summary-markdown", required=True)
    append_publish_parser.add_argument("--target-doc-url", default="")
    append_publish_parser.add_argument("--doc-url", default="")
    append_publish_parser.add_argument("--mode", required=True)
    append_publish_parser.add_argument("--publisher", required=True)
    append_publish_parser.add_argument(
        "--status",
        choices=["intent", "remote_written", "success", "failed"],
        default="success",
    )
    append_publish_parser.add_argument("--error", default="")

    parse_doc_parser = subparsers.add_parser("parse-lark-cli-doc-url", help="Parse doc URL or token from lark-cli output.")
    parse_doc_parser.add_argument("--output-file", required=True)
    parse_doc_parser.add_argument("--error-file", default="")
    parse_doc_parser.add_argument("--fallback-doc-url", default="")
    parse_doc_parser.add_argument("--doc-url-base", default="https://www.feishu.cn/docx")

    update_quarantine_parser = subparsers.add_parser("update-quarantine", help="Write non-retryable content failures into quarantine.json.")
    update_quarantine_parser.add_argument("--batch-file", required=True)
    update_quarantine_parser.add_argument("--quarantine-file", required=True)
    update_quarantine_parser.add_argument("--run-at", required=True)

    clear_quarantine_parser = subparsers.add_parser("clear-quarantine", help="Remove handled files from quarantine.json.")
    clear_quarantine_parser.add_argument("--batch-file", required=True)
    clear_quarantine_parser.add_argument("--quarantine-file", required=True)
    clear_quarantine_parser.add_argument("--run-at", required=True)

    inspect_quarantine_parser = subparsers.add_parser("inspect-quarantine", help="Render quarantine.json as a simple manual checklist.")
    inspect_quarantine_parser.add_argument("--quarantine-file", required=True)
    inspect_quarantine_parser.add_argument("--output", default="")

    record_retry_parser = subparsers.add_parser(
        "record-stage-retry",
        help="Record per-file, per-stage failures without resetting unrelated files.",
    )
    record_retry_parser.add_argument("--batch-file", required=True)
    record_retry_parser.add_argument("--ledger-file", required=True)
    record_retry_parser.add_argument("--stage", required=True)
    record_retry_parser.add_argument("--run-at", required=True)
    record_retry_parser.add_argument("--workflow-version", required=True)
    record_retry_parser.add_argument("--error-code", default="")
    record_retry_parser.add_argument("--error-type", default="")
    record_retry_parser.add_argument("--retryable", default="")
    record_retry_parser.add_argument("--message", default="")
    record_retry_parser.add_argument("--max-attempts", type=int, default=4)
    record_retry_parser.add_argument("--retry-delays-minutes", default="5,10,20")

    resolve_retry_parser = subparsers.add_parser(
        "resolve-stage-retry",
        help="Mark matching per-file stage failures as resolved.",
    )
    resolve_retry_parser.add_argument("--batch-file", required=True)
    resolve_retry_parser.add_argument("--ledger-file", required=True)
    resolve_retry_parser.add_argument("--stage", required=True)
    resolve_retry_parser.add_argument("--run-at", required=True)
    resolve_retry_parser.add_argument("--workflow-version", required=True)

    filter_retry_parser = subparsers.add_parser(
        "filter-stage-retries",
        help="Keep due/new files eligible while deferring files in retry cooldown or manual-action states.",
    )
    filter_retry_parser.add_argument("--batch-file", required=True)
    filter_retry_parser.add_argument("--ledger-file", required=True)
    filter_retry_parser.add_argument("--output", required=True)
    filter_retry_parser.add_argument("--stage", default="any")
    filter_retry_parser.add_argument("--run-at", required=True)
    filter_retry_parser.add_argument("--workflow-version", required=True)

    retry_status_parser = subparsers.add_parser(
        "stage-retry-status",
        help="Summarize retry ledger state for one workflow version.",
    )
    retry_status_parser.add_argument("--ledger-file", required=True)
    retry_status_parser.add_argument("--workflow-version", required=True)

    outbox_enqueue_parser = subparsers.add_parser(
        "notification-outbox-enqueue",
        help="Atomically enqueue one idempotent notification transition.",
    )
    outbox_enqueue_parser.add_argument("--outbox-file", required=True)
    outbox_enqueue_parser.add_argument("--idempotency-key", required=True)
    outbox_enqueue_parser.add_argument("--supersede-scope", default="")
    outbox_enqueue_parser.add_argument("--event", required=True)
    outbox_enqueue_parser.add_argument("--format", default="text")
    outbox_enqueue_parser.add_argument("--message-file", required=True)
    outbox_enqueue_parser.add_argument("--run-id", required=True)
    outbox_enqueue_parser.add_argument("--run-at", required=True)

    outbox_next_parser = subparsers.add_parser(
        "notification-outbox-next-due",
        help="Return the next due notification, if any.",
    )
    outbox_next_parser.add_argument("--outbox-file", required=True)
    outbox_next_parser.add_argument("--run-at", required=True)

    outbox_record_parser = subparsers.add_parser(
        "notification-outbox-record",
        help="Record notification delivery success or schedule 5/10/20 minute retry.",
    )
    outbox_record_parser.add_argument("--outbox-file", required=True)
    outbox_record_parser.add_argument("--idempotency-key", required=True)
    outbox_record_parser.add_argument("--run-at", required=True)
    outbox_record_parser.add_argument("--status", choices=["success", "failed"], required=True)
    outbox_record_parser.add_argument("--message-id", default="")
    outbox_record_parser.add_argument("--error", default="")

    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        if path.exists() and path.read_text(encoding="utf-8") == rendered:
            return
    except OSError:
        pass
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        tmp_path = Path(handle.name)
        handle.write(rendered)
        handle.flush()
        os.fsync(handle.fileno())
    tmp_path.replace(path)


def parse_bool(value: Any, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(str(value or "").strip())


def file_sha256_for_item(item: dict[str, Any]) -> str:
    cached = str(item.get("text_extract_cache_key", "")).strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", cached):
        return cached

    path_text = str(item.get("path", "")).strip()
    path = Path(path_text).expanduser() if path_text else None
    if path is not None and path.exists() and path.is_file():
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    identity = {
        "path": path_text,
        "size_bytes": int(item.get("size_bytes", 0) or 0),
        "modified_at": str(item.get("modified_at", "")).strip(),
    }
    return hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def load_retry_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "updated_at": "", "entries": {}}
    try:
        payload = load_json(path)
    except Exception:
        payload = {}
    entries = payload.get("entries") if isinstance(payload, dict) else {}
    if not isinstance(entries, dict):
        entries = {}
    return {
        "schema_version": 1,
        "updated_at": str(payload.get("updated_at", "")).strip() if isinstance(payload, dict) else "",
        "entries": entries,
    }


def retry_entry_key(file_sha256: str, stage: str, error_code: str, workflow_version: str) -> str:
    raw = json.dumps(
        {
            "file_sha256": file_sha256,
            "stage": stage,
            "error_code": error_code,
            "workflow_version": workflow_version,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def retry_delays(raw: str) -> list[int]:
    values: list[int] = []
    for part in str(raw or "").split(","):
        try:
            value = int(part.strip())
        except Exception:
            continue
        if value > 0:
            values.append(value)
    return values or [5, 10, 20]


def record_stage_retry(
    batch_file: Path,
    ledger_file: Path,
    *,
    stage: str,
    run_at: str,
    workflow_version: str,
    error_code_override: str = "",
    error_type_override: str = "",
    retryable_override: str = "",
    message_override: str = "",
    max_attempts: int = 4,
    delays_minutes: str = "5,10,20",
) -> int:
    batch = load_json(batch_file)
    ledger = load_retry_ledger(ledger_file)
    entries = dict(ledger.get("entries") or {})
    now = parse_datetime(run_at)
    delays = retry_delays(delays_minutes)
    recorded: list[dict[str, Any]] = []

    for item in batch.get("files", []):
        if not isinstance(item, dict):
            continue
        if stage == "text_extract":
            message = message_override or str(item.get("text_extract_error", "")).strip()
            error_code = error_code_override or str(item.get("text_extract_error_code", "")).strip()
            error_type = error_type_override or str(item.get("text_extract_error_type", "")).strip()
            retryable = parse_bool(
                retryable_override,
                bool(item.get("text_extract_retryable", False)),
            )
        else:
            message = message_override
            error_code = error_code_override
            error_type = error_type_override
            retryable = parse_bool(retryable_override, False)
        if not message and not error_code:
            continue

        error_code = error_code or f"{stage}_failed"
        error_type = error_type or ("transient_failure" if retryable else "unknown_failure")
        file_sha = file_sha256_for_item(item)
        extractor_profile = str(item.get("text_extract_profile", "")).strip() or None
        entry_key = retry_entry_key(file_sha, stage, error_code, workflow_version)
        previous = entries.get(entry_key) if isinstance(entries.get(entry_key), dict) else {}
        failure_count = int(previous.get("failure_count", 0) or 0) + 1
        next_retry_at: str | None = None
        if not retryable and error_type == "content_failure":
            status = "needs_transform"
        elif not retryable:
            status = "needs_review"
        elif failure_count >= max(max_attempts, 1):
            status = "retry_exhausted"
        else:
            status = "retry_pending"
            delay_index = min(failure_count - 1, len(delays) - 1)
            next_retry_at = (now + timedelta(minutes=delays[delay_index])).isoformat()

        entry = {
            "entry_key": entry_key,
            "file_sha256": file_sha,
            "path": str(item.get("path", "")).strip(),
            "filename": str(item.get("filename", "")).strip(),
            "stage": stage,
            "error_code": error_code,
            "error_type": error_type,
            "message": message[:1000],
            "retryable": retryable,
            "status": status,
            "workflow_version": workflow_version,
            "extractor_profile": extractor_profile,
            "failure_count": failure_count,
            "max_attempts": max(max_attempts, 1),
            "first_failed_at": str(previous.get("first_failed_at", "")).strip() or run_at,
            "last_failed_at": run_at,
            "next_retry_at": next_retry_at,
            "resolved_at": None,
        }
        entries[entry_key] = entry
        recorded.append(entry)

    ledger["updated_at"] = run_at
    ledger["entries"] = entries
    save_json(ledger_file, ledger)
    next_retries = [
        str(item.get("next_retry_at"))
        for item in recorded
        if str(item.get("next_retry_at") or "").strip()
    ]
    print(
        json.dumps(
            {
                "recorded_count": len(recorded),
                "next_retry_at": min(next_retries) if next_retries else None,
                "entries": recorded,
            },
            ensure_ascii=False,
        )
    )
    return 0


def resolve_stage_retry(
    batch_file: Path,
    ledger_file: Path,
    *,
    stage: str,
    run_at: str,
    workflow_version: str,
) -> int:
    batch = load_json(batch_file)
    file_hashes = {
        file_sha256_for_item(item)
        for item in batch.get("files", [])
        if isinstance(item, dict)
    }
    ledger = load_retry_ledger(ledger_file)
    entries = dict(ledger.get("entries") or {})
    resolved = 0
    for key, raw_entry in list(entries.items()):
        if not isinstance(raw_entry, dict):
            continue
        if str(raw_entry.get("file_sha256", "")).strip() not in file_hashes:
            continue
        if str(raw_entry.get("workflow_version", "")).strip() != workflow_version:
            continue
        if stage != "any" and str(raw_entry.get("stage", "")).strip() != stage:
            continue
        if str(raw_entry.get("status", "")).strip() == "resolved":
            continue
        entry = dict(raw_entry)
        entry["status"] = "resolved"
        entry["next_retry_at"] = None
        entry["resolved_at"] = run_at
        entries[key] = entry
        resolved += 1
    ledger["updated_at"] = run_at
    ledger["entries"] = entries
    save_json(ledger_file, ledger)
    print(json.dumps({"resolved_count": resolved}, ensure_ascii=False))
    return 0


def active_retry_for_file(
    entries: dict[str, Any],
    file_sha: str,
    workflow_version: str,
    stage: str,
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for raw_entry in entries.values():
        if not isinstance(raw_entry, dict):
            continue
        if str(raw_entry.get("file_sha256", "")).strip() != file_sha:
            continue
        if str(raw_entry.get("workflow_version", "")).strip() != workflow_version:
            continue
        if stage != "any" and str(raw_entry.get("stage", "")).strip() != stage:
            continue
        if str(raw_entry.get("status", "")).strip() == "resolved":
            continue
        candidates.append(raw_entry)
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda entry: str(entry.get("last_failed_at", "")),
        reverse=True,
    )[0]


def filter_stage_retries(
    batch_file: Path,
    ledger_file: Path,
    output: Path,
    *,
    stage: str,
    run_at: str,
    workflow_version: str,
) -> int:
    batch = load_json(batch_file)
    ledger = load_retry_ledger(ledger_file)
    entries = dict(ledger.get("entries") or {})
    now = parse_datetime(run_at)
    eligible: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []

    for item in batch.get("files", []):
        if not isinstance(item, dict):
            continue
        file_sha = file_sha256_for_item(item)
        entry = active_retry_for_file(entries, file_sha, workflow_version, stage)
        if entry is None:
            eligible.append(item)
            continue
        status = str(entry.get("status", "")).strip()
        next_retry_raw = str(entry.get("next_retry_at") or "").strip()
        if status == "retry_pending" and next_retry_raw:
            try:
                if parse_datetime(next_retry_raw) <= now:
                    eligible.append(item)
                    continue
            except Exception:
                eligible.append(item)
                continue
        deferred.append(
            {
                "filename": str(item.get("filename", "")).strip(),
                "path": str(item.get("path", "")).strip(),
                "stage": str(entry.get("stage", "")).strip(),
                "error_code": str(entry.get("error_code", "")).strip(),
                "status": status,
                "next_retry_at": next_retry_raw or None,
            }
        )

    output_payload = dict(batch)
    output_payload["files"] = eligible
    output_payload["new_pdf_count"] = len(eligible)
    output_payload["deferred_retry_count"] = len(deferred)
    output_payload["deferred_retries"] = deferred
    save_json(output, output_payload)
    next_retry_values = [
        str(item.get("next_retry_at"))
        for item in deferred
        if str(item.get("next_retry_at") or "").strip()
    ]
    print(
        json.dumps(
            {
                "eligible_count": len(eligible),
                "deferred_count": len(deferred),
                "next_retry_at": min(next_retry_values) if next_retry_values else None,
                "deferred": deferred,
            },
            ensure_ascii=False,
        )
    )
    return 0


def stage_retry_status(ledger_file: Path, workflow_version: str) -> int:
    ledger = load_retry_ledger(ledger_file)
    counts: dict[str, int] = {}
    next_values: list[str] = []
    for entry in (ledger.get("entries") or {}).values():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("workflow_version", "")).strip() != workflow_version:
            continue
        status = str(entry.get("status", "")).strip() or "unknown"
        if status == "resolved":
            continue
        counts[status] = counts.get(status, 0) + 1
        next_value = str(entry.get("next_retry_at") or "").strip()
        if next_value:
            next_values.append(next_value)
    print(
        json.dumps(
            {
                "counts": counts,
                "active_count": sum(counts.values()),
                "next_retry_at": min(next_values) if next_values else None,
            },
            ensure_ascii=False,
        )
    )
    return 0


def load_notification_outbox(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "updated_at": "", "items": {}}
    try:
        payload = load_json(path)
    except Exception:
        payload = {}
    items = payload.get("items") if isinstance(payload, dict) else {}
    if not isinstance(items, dict):
        items = {}
    return {
        "schema_version": 1,
        "updated_at": str(payload.get("updated_at", "")).strip() if isinstance(payload, dict) else "",
        "items": items,
    }


def notification_outbox_enqueue(
    outbox_file: Path,
    *,
    idempotency_key: str,
    supersede_scope: str,
    event: str,
    message_format: str,
    message_file: Path,
    run_id: str,
    run_at: str,
) -> int:
    outbox = load_notification_outbox(outbox_file)
    items = dict(outbox.get("items") or {})
    superseded_keys: list[str] = []
    if supersede_scope:
        for existing_key, existing in list(items.items()):
            if existing_key == idempotency_key or not isinstance(existing, dict):
                continue
            if str(existing.get("supersede_scope", "")).strip() != supersede_scope:
                continue
            if str(existing.get("status", "")).strip() != "pending":
                continue
            replacement = dict(existing)
            replacement.update(
                {
                    "status": "superseded",
                    "next_attempt_at": None,
                    "superseded_at": run_at,
                    "superseded_by": idempotency_key,
                }
            )
            items[existing_key] = replacement
            superseded_keys.append(existing_key)
    previous = items.get(idempotency_key) if isinstance(items.get(idempotency_key), dict) else {}
    message = message_file.read_text(encoding="utf-8", errors="replace")
    item = dict(previous)
    item.update(
        {
            "idempotency_key": idempotency_key,
            "event": event,
            "format": message_format,
            "message": message,
            "run_id": run_id,
            "supersede_scope": supersede_scope or None,
            "last_enqueued_at": run_at,
            "created_at": str(previous.get("created_at", "")).strip() or run_at,
            "attempt_count": int(previous.get("attempt_count", 0) or 0),
        }
    )
    if str(previous.get("status", "")).strip() != "sent":
        item["status"] = str(previous.get("status", "")).strip() or "pending"
        item["next_attempt_at"] = previous.get("next_attempt_at")
    items[idempotency_key] = item
    outbox["updated_at"] = run_at
    outbox["items"] = items
    save_json(outbox_file, outbox)
    print(
        json.dumps(
            {
                "queued": item.get("status") != "sent",
                "item": item,
                "superseded_keys": superseded_keys,
            },
            ensure_ascii=False,
        )
    )
    return 0


def notification_outbox_next_due(outbox_file: Path, run_at: str) -> int:
    outbox = load_notification_outbox(outbox_file)
    now = parse_datetime(run_at)
    items = [item for item in (outbox.get("items") or {}).values() if isinstance(item, dict)]
    pending_document_run_ids = {
        str(item.get("run_id", "")).strip()
        for item in items
        if str(item.get("status", "")).strip() == "pending"
        and str(item.get("event", "")).strip() == "doc-completed"
        and str(item.get("run_id", "")).strip()
    }
    due: list[dict[str, Any]] = []
    for item in items:
        if str(item.get("status", "")).strip() != "pending":
            continue
        item_run_id = str(item.get("run_id", "")).strip()
        if (
            str(item.get("event", "")).strip() != "doc-completed"
            and item_run_id
            and item_run_id in pending_document_run_ids
        ):
            # Preserve the user-facing order "document links first, terminal
            # summary last".  A document item that eventually reaches
            # dead_letter no longer blocks the terminal fallback.
            continue
        next_attempt_raw = str(item.get("next_attempt_at") or "").strip()
        if next_attempt_raw:
            try:
                if parse_datetime(next_attempt_raw) > now:
                    continue
            except Exception:
                pass
        due.append(item)
    due.sort(key=lambda item: str(item.get("created_at", "")))
    print(json.dumps({"found": bool(due), "item": due[0] if due else None}, ensure_ascii=False))
    return 0


def notification_outbox_record(
    outbox_file: Path,
    *,
    idempotency_key: str,
    run_at: str,
    status: str,
    message_id: str,
    error: str,
) -> int:
    outbox = load_notification_outbox(outbox_file)
    items = dict(outbox.get("items") or {})
    previous = items.get(idempotency_key) if isinstance(items.get(idempotency_key), dict) else {}
    if not previous:
        print(json.dumps({"updated": False, "reason": "missing"}, ensure_ascii=False))
        return 0
    item = dict(previous)
    if status == "success":
        item["status"] = "sent"
        item["sent_at"] = run_at
        item["message_id"] = message_id or None
        item["next_attempt_at"] = None
        item["last_error"] = None
    else:
        attempts = int(item.get("attempt_count", 0) or 0) + 1
        item["attempt_count"] = attempts
        item["last_attempt_at"] = run_at
        item["last_error"] = error[:1000] or None
        delays = [5, 10, 20]
        if attempts > len(delays):
            item["status"] = "dead_letter"
            item["next_attempt_at"] = None
        else:
            item["status"] = "pending"
            item["next_attempt_at"] = (
                parse_datetime(run_at) + timedelta(minutes=delays[attempts - 1])
            ).isoformat()
    items[idempotency_key] = item
    outbox["updated_at"] = run_at
    outbox["items"] = items
    save_json(outbox_file, outbox)
    print(json.dumps({"updated": True, "item": item}, ensure_ascii=False))
    return 0


def normalize_text_path(value: Any) -> str:
    return str(value or "").strip()


def max_line_length(text: str) -> int:
    return max((len(line) for line in str(text or "").splitlines()), default=0)


def build_overlong_input_error(text_path: Path, text_source: str, longest_line: int) -> str:
    if str(text_source or "").strip() == "markitdown_clean" or text_path.name.endswith(".clean.md"):
        prefix = "clean.md 格式不合格"
    else:
        prefix = "摘要输入格式不合格"
    return (
        f"{prefix}：存在超过 {MAX_SUMMARY_INPUT_LINE_CHARS} 字的单行"
        f"（最长 {longest_line} 字）"
    )


def build_quarantine_diagnosis(error_type: str, error_code: str, error: str) -> str:
    if error_code == "pdf_parse_failure":
        return "PDF 结构可能有损坏或页表异常，自动链路没法稳定解析。"
    if error_code == "no_usable_text":
        return "自动提取和 OCR 都没恢复出可用正文，继续盲重试收益很低。"
    if error_type == "content_failure":
        return "这份文件更像内容本身有问题，不像运行环境故障。"
    return str(error or "").strip() or "这份文件需要人工复核。"


def build_quarantine_action(error_type: str, error_code: str) -> str:
    if error_code == "pdf_parse_failure":
        return "先人工打开 PDF；如果文件损坏，重新下载或换源后再重跑。"
    if error_code == "no_usable_text":
        return "先人工检查正文；如果是纯图片或水印太重，考虑换源或手动 OCR。"
    if error_type == "content_failure":
        return "人工复核 PDF 内容，再决定是放弃还是手动重跑。"
    return "先人工复核，再决定下一步处理。"


def build_quarantine_next_step(error_code: str) -> str:
    if error_code == "pdf_parse_failure":
        return "确认文件是否损坏"
    if error_code == "no_usable_text":
        return "人工检查正文质量"
    return "人工复核这份 PDF"


def build_quarantine_command(path: str) -> str:
    value = str(path or "").strip()
    if not value:
        return ""
    quoted = shlex.quote(value)
    return (
        "bash ${OPENCLAW_TASKS_ROOT:-$HOME/.openclaw/workspace/tasks}/ZSXQ_pdf_digest/run.sh "
        f"--dry-run --file {quoted}"
    )


def normalize_quarantine_entry(entry: dict[str, Any]) -> dict[str, Any]:
    path = str(entry.get("path", "")).strip()
    filename = str(entry.get("filename", "")).strip() or Path(path).name
    error_type = str(entry.get("error_type", "")).strip() or "content_failure"
    error_code = str(entry.get("error_code", "")).strip() or "unknown_failure"
    latest_error = str(entry.get("latest_error", "")).strip() or str(entry.get("error", "")).strip()
    latest_warning = str(entry.get("latest_warning", "")).strip() or str(entry.get("warning", "")).strip()
    diagnosis = str(entry.get("diagnosis", "")).strip() or build_quarantine_diagnosis(error_type, error_code, latest_error)
    recommended_action = str(entry.get("recommended_action", "")).strip() or build_quarantine_action(error_type, error_code)
    next_step = str(entry.get("next_step", "")).strip() or build_quarantine_next_step(error_code)
    suggested_command = str(entry.get("suggested_command", "")).strip() or build_quarantine_command(path)

    return {
        "path": path,
        "filename": filename,
        "relative_path": str(entry.get("relative_path", "")).strip(),
        "cache_key": str(entry.get("cache_key", "")).strip(),
        "status": str(entry.get("status", "")).strip() or "needs_transform",
        "error_type": error_type,
        "error_code": error_code,
        "latest_error": latest_error,
        "latest_warning": latest_warning,
        "retryable": bool(entry.get("retryable", False)),
        "needs_manual_action": bool(entry.get("needs_manual_action", True)),
        "diagnosis": diagnosis,
        "recommended_action": recommended_action,
        "next_step": next_step,
        "suggested_command": suggested_command,
        "first_quarantined_at": str(entry.get("first_quarantined_at", "")).strip(),
        "last_quarantined_at": str(entry.get("last_quarantined_at", "")).strip(),
        "failure_count": int(entry.get("failure_count", 0) or 0),
    }


def build_quarantine_payload(items: dict[str, Any], updated_at: str) -> dict[str, Any]:
    normalized_items: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in items.items():
        if not isinstance(raw_value, dict):
            raw_value = {"path": str(raw_key or "").strip()}
        entry = normalize_quarantine_entry(raw_value)
        if not entry["path"]:
            continue
        normalized_items[entry["path"]] = entry

    entries = sorted(
        normalized_items.values(),
        key=lambda item: (
            str(item.get("last_quarantined_at", "")),
            str(item.get("filename", "")).casefold(),
            str(item.get("path", "")).casefold(),
        ),
        reverse=True,
    )

    by_error_type: dict[str, int] = {}
    by_error_code: dict[str, int] = {}
    by_recommended_action: dict[str, int] = {}
    for entry in entries:
        error_type = str(entry.get("error_type", "")).strip()
        error_code = str(entry.get("error_code", "")).strip()
        action = str(entry.get("next_step", "")).strip()
        if error_type:
            by_error_type[error_type] = by_error_type.get(error_type, 0) + 1
        if error_code:
            by_error_code[error_code] = by_error_code.get(error_code, 0) + 1
        if action:
            by_recommended_action[action] = by_recommended_action.get(action, 0) + 1

    summary = {
        "total_entries": len(entries),
        "needs_manual_action": sum(1 for entry in entries if bool(entry.get("needs_manual_action", False))),
        "latest_quarantined_at": entries[0]["last_quarantined_at"] if entries else "",
        "by_error_type": by_error_type,
        "by_error_code": by_error_code,
        "by_next_step": by_recommended_action,
    }

    return {
        "schema_version": 2,
        "updated_at": updated_at,
        "summary": summary,
        "entries": entries,
        "items": normalized_items,
    }


def load_quarantine_payload(quarantine_file: Path) -> dict[str, Any]:
    if not quarantine_file.exists():
        return build_quarantine_payload({}, "")

    try:
        raw = load_json(quarantine_file)
    except Exception:
        raw = {}

    items = raw.get("items") or {}
    if not isinstance(items, dict):
        items = {}
    updated_at = str(raw.get("updated_at", "")).strip()
    return build_quarantine_payload(items, updated_at)


def update_quarantine(batch_file: Path, quarantine_file: Path, run_at: str) -> int:
    batch = load_json(batch_file)
    payload = load_quarantine_payload(quarantine_file)
    items = dict(payload.get("items") or {})
    changed = False

    for entry in batch.get("files", []):
        path = str(entry.get("path", "")).strip()
        error_type = str(entry.get("text_extract_error_type", "")).strip()
        retryable = bool(entry.get("text_extract_retryable", False))
        if not path or error_type != "content_failure" or retryable:
            continue

        existing = items.get(path) if isinstance(items.get(path), dict) else {}
        normalized_existing = normalize_quarantine_entry(existing or {"path": path})
        normalized_existing.update(
            {
                "path": path,
                "filename": str(entry.get("filename", "")).strip() or normalized_existing.get("filename", ""),
                "relative_path": str(entry.get("relative_path", "")).strip(),
                "cache_key": str(entry.get("text_extract_cache_key", "")).strip(),
                "status": "needs_transform",
                "error_type": error_type,
                "error_code": str(entry.get("text_extract_error_code", "")).strip(),
                "latest_error": str(entry.get("text_extract_error", "")).strip(),
                "latest_warning": str(entry.get("text_extract_warning", "")).strip(),
                "retryable": retryable,
                "needs_manual_action": True,
                "first_quarantined_at": normalized_existing.get("first_quarantined_at") or run_at,
                "last_quarantined_at": run_at,
                "failure_count": int(normalized_existing.get("failure_count", 0) or 0) + 1,
                "diagnosis": build_quarantine_diagnosis(
                    error_type,
                    str(entry.get("text_extract_error_code", "")).strip(),
                    str(entry.get("text_extract_error", "")).strip(),
                ),
                "recommended_action": build_quarantine_action(
                    error_type,
                    str(entry.get("text_extract_error_code", "")).strip(),
                ),
                "next_step": build_quarantine_next_step(
                    str(entry.get("text_extract_error_code", "")).strip(),
                ),
                "suggested_command": build_quarantine_command(path),
            }
        )
        items[path] = normalize_quarantine_entry(normalized_existing)
        changed = True

    if changed or not quarantine_file.exists():
        save_json(quarantine_file, build_quarantine_payload(items, run_at))

    print(
        json.dumps(
            {
                "updated": changed,
                "entry_count": len(items),
                "quarantine_file": str(quarantine_file),
            },
            ensure_ascii=False,
        )
    )
    return 0


def clear_quarantine(batch_file: Path, quarantine_file: Path, run_at: str) -> int:
    if not batch_file.exists() or not quarantine_file.exists():
        print(json.dumps({"updated": False, "entry_count": 0}, ensure_ascii=False))
        return 0

    batch = load_json(batch_file)
    payload = load_quarantine_payload(quarantine_file)
    items = dict(payload.get("items") or {})
    removed_count = 0

    for entry in batch.get("files", []):
        path = str(entry.get("path", "")).strip()
        if path and path in items:
            items.pop(path, None)
            removed_count += 1

    if removed_count:
        save_json(quarantine_file, build_quarantine_payload(items, run_at))

    print(
        json.dumps(
            {
                "updated": bool(removed_count),
                "removed_count": removed_count,
                "entry_count": len(items),
                "quarantine_file": str(quarantine_file),
            },
            ensure_ascii=False,
        )
    )
    return 0


def render_quarantine_report(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    entries = payload.get("entries") or []
    lines = [
        "ZSXQ PDF 隔离清单",
        f"更新时间：{str(payload.get('updated_at', '')).strip() or '未记录'}",
        f"待人工处理：{int(summary.get('total_entries', 0) or 0)} 份",
    ]

    by_next_step = summary.get("by_next_step") or {}
    if by_next_step:
        step_text = "；".join(f"{step} {count} 份" for step, count in by_next_step.items())
        lines.append(f"动作分布：{step_text}")

    if not entries:
        lines.append("")
        lines.append("当前没有待人工处理的隔离文件。")
        return "\n".join(lines)

    for index, entry in enumerate(entries, start=1):
        filename = str(entry.get("filename", "")).strip() or str(entry.get("path", "")).strip()
        lines.extend(
            [
                "",
                f"{index}. {filename}",
                f"   结论：{str(entry.get('diagnosis', '')).strip() or '待人工复核'}",
                f"   建议：{str(entry.get('recommended_action', '')).strip() or '人工复核'}",
                f"   下一步：{str(entry.get('next_step', '')).strip() or '人工复核'}",
                f"   标记：{str(entry.get('error_type', '')).strip()} / {str(entry.get('error_code', '')).strip()}",
                f"   最近错误：{str(entry.get('latest_error', '')).strip() or '无'}",
                f"   最近提示：{str(entry.get('latest_warning', '')).strip() or '无'}",
                f"   失败次数：{int(entry.get('failure_count', 0) or 0)}",
                f"   首次隔离：{str(entry.get('first_quarantined_at', '')).strip() or '未记录'}",
                f"   最近隔离：{str(entry.get('last_quarantined_at', '')).strip() or '未记录'}",
                f"   文件路径：{str(entry.get('path', '')).strip()}",
                f"   建议命令：{str(entry.get('suggested_command', '')).strip() or '无'}",
            ]
        )

    return "\n".join(lines)


def inspect_quarantine(quarantine_file: Path, output_path: Path | None) -> int:
    payload = load_quarantine_payload(quarantine_file)
    report_text = render_quarantine_report(payload)
    if output_path is not None:
        rendered = report_text + "\n"
        if not output_path.exists() or output_path.read_text(encoding="utf-8") != rendered:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=output_path.parent,
                prefix=f".{output_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                tmp_path = Path(handle.name)
                handle.write(rendered)
                handle.flush()
                os.fsync(handle.fileno())
            tmp_path.replace(output_path)
    print(report_text)
    return 0


def build_doc_title(batch: dict[str, Any]) -> str:
    created_at_raw = str(batch.get("generated_at") or "").strip()
    created_date = "未知日期"
    if created_at_raw:
        try:
            created_date = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except ValueError:
            created_date = created_at_raw[:10] or "未知日期"

    chunk_index = int(batch.get("chunk_index", 1) or 1)
    chunk_total = int(batch.get("chunk_total", 1) or 1)
    file_count = int(batch.get("new_pdf_count", len(batch.get("files", []))) or 0)

    title = f"知识星球研报总结（{created_date}） {file_count} 篇"
    if chunk_total > 1:
        title = f"{title} {chunk_index}/{chunk_total}"
    return title


def build_chunk_payload(
    batch: dict[str, Any],
    chunk_files: list[dict[str, Any]],
    chunk_index: int,
    chunk_total: int,
    source_batch_file: Path,
) -> dict[str, Any]:
    payload = dict(batch)
    payload["source_batch_file"] = str(source_batch_file)
    payload["chunk_index"] = chunk_index
    payload["chunk_total"] = chunk_total
    payload["total_pdf_count"] = int(batch.get("new_pdf_count", len(batch.get("files", []))))
    payload["new_pdf_count"] = len(chunk_files)
    payload["files"] = chunk_files
    return payload


def split_batch(batch_file: Path, output_dir: Path, chunk_size: int) -> int:
    if chunk_size <= 0:
        raise SystemExit("chunk_size must be greater than 0")

    batch = load_json(batch_file)
    files = list(batch.get("files", []))
    output_dir.mkdir(parents=True, exist_ok=True)

    if not files:
        print(json.dumps({"chunk_count": 0, "chunk_files": []}, ensure_ascii=False))
        return 0

    chunk_total = math.ceil(len(files) / chunk_size)
    chunk_paths: list[str] = []

    for index in range(chunk_total):
        start = index * chunk_size
        end = start + chunk_size
        chunk_files = files[start:end]
        chunk_payload = build_chunk_payload(
            batch=batch,
            chunk_files=chunk_files,
            chunk_index=index + 1,
            chunk_total=chunk_total,
            source_batch_file=batch_file,
        )
        chunk_path = output_dir / f"chunk-{index + 1:03d}.json"
        save_json(chunk_path, chunk_payload)
        chunk_paths.append(str(chunk_path))

    print(json.dumps({"chunk_count": chunk_total, "chunk_files": chunk_paths}, ensure_ascii=False))
    return 0


def render_prompt(
    template_path: Path,
    batch_file: Path,
    system_prompt_file: Path | None = None,
    output_path: Path | None = None,
) -> int:
    if output_path is None:
        raise SystemExit("output_path is required")
    template = template_path.read_text(encoding="utf-8")
    batch = load_json(batch_file)
    system_prompt_text = ""
    if system_prompt_file is not None:
        system_prompt_text = system_prompt_file.read_text(encoding="utf-8").strip()
    files = list(batch.get("files", []))
    current_file_lines: list[str] = []
    current_path_list: list[str] = []
    for index, item in enumerate(files, start=1):
        filename = str(item.get("filename", "")).strip()
        path = str(item.get("path", "")).strip()
        text_path = str(item.get("extracted_text_path", "")).strip()
        text_source = str(item.get("text_source", "")).strip()
        text_error = str(item.get("text_extract_error", "")).strip()
        text_warning = str(item.get("text_extract_warning", "")).strip()
        title = str(item.get("title", "")).strip()
        report_id = str(item.get("report_id", "")).strip()
        text_chars = int(item.get("extracted_text_chars", 0) or 0)
        if filename and path:
            lines = [f"{index}. {filename}", f"   PDF路径：{path}"]
            if title:
                lines.append(f"   标题：{title}")
            if report_id:
                lines.append(f"   report_id：{report_id}")
            if text_path:
                lines.append(f"   优先读取文本：{text_path}")
            if text_source:
                lines.append(f"   文本来源：{text_source}")
            if text_chars > 0:
                lines.append(f"   文本长度：{text_chars} 字符")
            if text_warning:
                lines.append(f"   文本提示：{text_warning}")
            if text_error:
                lines.append(f"   文本提取失败：{text_error}")
            current_file_lines.append("\n".join(lines))
            current_path_list.append(path)
    current_file_manifest = "\n".join(current_file_lines)
    current_path_json = json.dumps(current_path_list, ensure_ascii=False)

    replacements = {
        "{{EDITOR_SYSTEM_PROMPT}}": system_prompt_text,
        "{{BATCH_JSON_PATH}}": str(batch_file),
        "{{CHUNK_INDEX}}": str(int(batch.get("chunk_index", 1))),
        "{{CHUNK_TOTAL}}": str(int(batch.get("chunk_total", 1))),
        "{{CHUNK_FILE_COUNT}}": str(int(batch.get("new_pdf_count", len(batch.get("files", []))))),
        "{{TOTAL_FILE_COUNT}}": str(int(batch.get("total_pdf_count", batch.get("new_pdf_count", 0)))),
        "{{CURRENT_FILE_MANIFEST}}": current_file_manifest,
        "{{CURRENT_PATH_JSON}}": current_path_json,
    }

    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)

    output_path.write_text(rendered, encoding="utf-8")
    return 0


def parse_agent_output_json(raw_text: str) -> dict[str, Any] | None:
    offsets: list[int] = []
    cursor = 0
    for line in raw_text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("{"):
            offsets.append(cursor + (len(line) - len(stripped)))
        cursor += len(line)

    for start in reversed(offsets):
        snippet = raw_text[start:].strip()
        if not snippet:
            continue
        try:
            data = json.loads(snippet)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _extract_payload_items(run_payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not run_payload:
        return []

    # OpenClaw 的 JSON 输出有过两种形状：
    # 旧版放在 result.payloads，新版直接放在顶层 payloads。
    candidate_lists = [
        run_payload.get("payloads"),
        ((run_payload.get("result") or {}).get("payloads") or []),
    ]
    for items in candidate_lists:
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def _extract_meta(run_payload: dict[str, Any] | None) -> dict[str, Any]:
    if not run_payload:
        return {}

    top_level_meta = run_payload.get("meta")
    if isinstance(top_level_meta, dict):
        return top_level_meta

    nested_meta = ((run_payload.get("result") or {}).get("meta") or {})
    return nested_meta if isinstance(nested_meta, dict) else {}


def extract_agent_payload_text(run_payload: dict[str, Any] | None, raw_text: str) -> str:
    if not run_payload:
        return raw_text

    payloads = _extract_payload_items(run_payload)
    texts: list[str] = []
    for item in payloads:
        text = str(item.get("text") or "").strip()
        if text:
            texts.append(text)
    normalized_text = "\n\n".join(texts).strip()
    return normalized_text or raw_text


def extract_doc_url(text: str) -> str:
    match = re.search(r"https?://[^\s)]+feishu\.cn/[^\s)]+", text)
    if not match:
        match = re.search(r"https?://[^\s)]+", text)
    return match.group(0) if match else ""


def inspect_agent_output(result_file: Path) -> int:
    raw_text = result_file.read_text(encoding="utf-8", errors="replace")
    run_payload = parse_agent_output_json(raw_text)
    normalized_text = extract_agent_payload_text(run_payload, raw_text)
    meta = _extract_meta(run_payload)
    agent_meta = meta.get("agentMeta") or {}
    usage = agent_meta.get("lastCallUsage") or agent_meta.get("usage") or {}
    system_prompt_report = meta.get("systemPromptReport") or {}
    system_prompt = system_prompt_report.get("systemPrompt") or {}
    skills = system_prompt_report.get("skills") or {}
    tools = system_prompt_report.get("tools") or {}

    payload = {
        "has_json_wrapper": run_payload is not None,
        "doc_url": extract_doc_url(normalized_text) or extract_doc_url(raw_text),
        "text_chars": len(normalized_text),
        "payload_count": len(_extract_payload_items(run_payload)),
        "session_id": str(agent_meta.get("sessionId") or "").strip(),
        "provider": str(agent_meta.get("provider") or "").strip(),
        "model": str(agent_meta.get("model") or "").strip(),
        "prompt_tokens": int(agent_meta.get("promptTokens") or 0),
        "usage": {
            "input": int(usage.get("input") or 0),
            "output": int(usage.get("output") or 0),
            "cacheRead": int(usage.get("cacheRead") or 0),
            "cacheWrite": int(usage.get("cacheWrite") or 0),
            "total": int(usage.get("total") or 0),
        },
        "system_prompt_chars": int(system_prompt.get("chars") or 0),
        "skills_prompt_chars": int(skills.get("promptChars") or 0),
        "tools_list_chars": int(tools.get("listChars") or 0),
        "tools_schema_chars": int(tools.get("schemaChars") or 0),
        "workspace_dir": str(system_prompt_report.get("workspaceDir") or "").strip(),
        "response_text": normalized_text,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def extract_prefixed_payload(result_file: Path, prefix: str, missing_message: str) -> dict[str, Any]:
    raw_text = result_file.read_text(encoding="utf-8", errors="replace")
    lines = extract_agent_payload_text(parse_agent_output_json(raw_text), raw_text).splitlines()
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        raw = stripped[len(prefix) :].strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"result json is invalid: {exc}") from exc
        if not isinstance(data, dict):
            raise SystemExit("result json must be an object")
        return data

    raise SystemExit(missing_message)


def extract_summary_payload(result_file: Path) -> dict[str, Any]:
    return extract_prefixed_payload(result_file, SUMMARY_PREFIX, "missing ZSXQ_SUMMARY_JSON line")


def check_batch_text_ready(batch_file: Path) -> int:
    batch = load_json(batch_file)
    failures: list[str] = []
    failure_details: list[dict[str, Any]] = []

    for item in batch.get("files", []):
        filename = str(item.get("filename", "")).strip() or "未命名文件"
        path = str(item.get("path", "")).strip()
        text_error = str(item.get("text_extract_error", "")).strip()
        text_path_raw = normalize_text_path(item.get("extracted_text_path"))
        text_source = str(item.get("text_source", "")).strip()
        error_type = str(item.get("text_extract_error_type", "")).strip()
        error_code = str(item.get("text_extract_error_code", "")).strip()
        retryable = bool(item.get("text_extract_retryable", False))

        if text_error:
            failures.append(f"{filename}: {text_error}")
            failure_details.append(
                {
                    "filename": filename,
                    "path": path,
                    "error": text_error,
                    "error_type": error_type,
                    "error_code": error_code,
                    "retryable": retryable,
                }
            )
            continue
        if not text_path_raw:
            failures.append(f"{filename}: 缺少 extracted_text_path")
            failure_details.append(
                {
                    "filename": filename,
                    "path": path,
                    "error": "缺少 extracted_text_path",
                    "error_type": error_type or "unknown_failure",
                    "error_code": error_code or "missing_text_path",
                    "retryable": retryable,
                }
            )
            continue

        text_path = Path(text_path_raw)
        if not text_path.exists():
            failures.append(f"{filename}: 文本文件不存在 {text_path}")
            failure_details.append(
                {
                    "filename": filename,
                    "path": path,
                    "error": f"文本文件不存在 {text_path}",
                    "error_type": error_type or "unknown_failure",
                    "error_code": error_code or "text_file_missing",
                    "retryable": retryable,
                }
            )
            continue

        text_body = ""
        try:
            text_body = text_path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            failures.append(f"{filename}: 文本文件不可读 {text_path} ({exc})")
            failure_details.append(
                {
                    "filename": filename,
                    "path": path,
                    "error": f"文本文件不可读 {text_path} ({exc})",
                    "error_type": error_type or "env_failure",
                    "error_code": error_code or "text_file_unreadable",
                    "retryable": retryable,
                }
            )
            continue

        longest_line = max_line_length(text_body)
        if longest_line > MAX_SUMMARY_INPUT_LINE_CHARS:
            error = build_overlong_input_error(text_path, text_source, longest_line)
            failures.append(f"{filename}: {error}")
            failure_details.append(
                {
                    "filename": filename,
                    "path": path,
                    "error": error,
                    "error_type": error_type or "env_failure",
                    "error_code": error_code or "summary_input_line_too_long",
                    "retryable": False,
                    "text_path": str(text_path),
                    "max_line_chars": longest_line,
                    "line_limit": MAX_SUMMARY_INPUT_LINE_CHARS,
                }
            )
            continue

        actual_chars = len(text_body.strip())
        if actual_chars <= 0:
            failures.append(f"{filename}: 文本文件为空 {text_path}")
            failure_details.append(
                {
                    "filename": filename,
                    "path": path,
                    "error": f"文本文件为空 {text_path}",
                    "error_type": error_type or "unknown_failure",
                    "error_code": error_code or "text_file_empty",
                    "retryable": retryable,
                }
            )

    failure_types = sorted(
        {
            str(detail.get("error_type", "")).strip()
            for detail in failure_details
            if str(detail.get("error_type", "")).strip()
        }
    )
    retryable_failure_count = sum(1 for detail in failure_details if bool(detail.get("retryable", False)))
    all_nonretryable_content_failures = bool(failure_details) and all(
        str(detail.get("error_type", "")).strip() == "content_failure"
        and not bool(detail.get("retryable", False))
        for detail in failure_details
    )
    has_env_failure = any(
        str(detail.get("error_type", "")).strip() == "env_failure"
        for detail in failure_details
    )

    payload = {
        "ok": not failures,
        "failure_count": len(failures),
        "message": "ok" if not failures else "；".join(failures),
        "failures": failures,
        "failure_details": failure_details,
        "failure_types": failure_types,
        "retryable_failure_count": retryable_failure_count,
        "all_nonretryable_content_failures": all_nonretryable_content_failures,
        "has_env_failure": has_env_failure,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def _normalize_path_for_compare(value: str) -> str:
    text = unicodedata.normalize("NFC", str(value or "").strip())
    text = re.sub(r"\s+", " ", text)
    text = text.replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    return text


def _validate_handled_paths(expected_paths: list[str], handled_paths: Any) -> list[str]:
    if not isinstance(handled_paths, list) or not all(isinstance(path, str) for path in handled_paths):
        raise SystemExit("handled_paths must be a string list")

    normalized_handled = [path.strip() for path in handled_paths if path.strip()]
    if len(normalized_handled) != len(expected_paths):
        raise SystemExit(
            f"handled path count mismatch: expected {len(expected_paths)}, got {len(normalized_handled)}"
        )

    expected_paths_normalized = sorted(_normalize_path_for_compare(path) for path in expected_paths)
    handled_paths_normalized = sorted(_normalize_path_for_compare(path) for path in normalized_handled)
    if handled_paths_normalized != expected_paths_normalized:
        expected_only = sorted(set(expected_paths_normalized) - set(handled_paths_normalized))
        handled_only = sorted(set(handled_paths_normalized) - set(expected_paths_normalized))
        debug_parts = []
        if expected_only:
            debug_parts.append(f"missing={expected_only[:2]}")
        if handled_only:
            debug_parts.append(f"unexpected={handled_only[:2]}")
        if not debug_parts and expected_paths and normalized_handled:
            debug_parts.append(f"expected_sample={expected_paths[0]!r}")
            debug_parts.append(f"handled_sample={normalized_handled[0]!r}")
        debug_suffix = f" ({'; '.join(debug_parts)})" if debug_parts else ""
        raise SystemExit(f"handled_paths do not match the current chunk{debug_suffix}")

    return normalized_handled


def summary_cache_paths(summary_cache_dir: Path, cache_key: str) -> tuple[Path, Path]:
    return (
        summary_cache_dir / "json" / f"{cache_key}.json",
        summary_cache_dir / "markdown" / f"{cache_key}.md",
    )


def research_library_root_from_env() -> Path | None:
    raw = os.environ.get("RESEARCH_LIBRARY_ROOT", "").strip()
    return Path(raw).expanduser() if raw else None


def report_id_for_summary(batch_item: dict[str, Any], cache_key: str) -> str:
    existing = str(batch_item.get("report_id", "") or "").strip()
    if existing:
        return existing
    pdf_sha256 = str(batch_item.get("pdf_sha256", "") or cache_key or "").strip().lower()
    if len(pdf_sha256) >= 16:
        return f"zsxq_{pdf_sha256[:16]}"
    stem = Path(str(batch_item.get("filename", "") or batch_item.get("path", "") or "report")).stem
    slug = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", stem).strip("_")[:80]
    return f"zsxq_{slug or 'report'}"


def artifact_batch_id(batch_item: dict[str, Any], library_root: Path) -> str:
    existing = str(batch_item.get("batch_id", "") or "").strip()
    if existing:
        return existing
    pdf_path = Path(str(batch_item.get("path", "") or batch_item.get("pdf_path", "") or "")).expanduser()
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
    raw = str(batch_item.get("modified_at", "") or batch_item.get("downloaded_at", "") or "").strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return "manual"
    return f"{dt.strftime('%Y-%m-%d_%H-%M-%S')}__to__{dt.strftime('%Y-%m-%d_%H-%M-%S')}"


def artifact_stem(batch_item: dict[str, Any]) -> str:
    filename = str(batch_item.get("filename", "") or "").strip()
    if filename:
        return Path(filename).stem
    path = str(batch_item.get("path", "") or batch_item.get("pdf_path", "") or "report").strip()
    return Path(path).stem or "report"


def permanent_summary_path(library_root: Path, batch_item: dict[str, Any], cache_key: str) -> Path:
    batch_id = artifact_batch_id(batch_item, library_root)
    stem = artifact_stem(batch_item)
    return library_root / "summaries" / batch_id / f"{stem}.summary.md"


def persist_permanent_summary(
    library_root: Path | None,
    batch_item: dict[str, Any],
    entry: dict[str, Any],
) -> str:
    if library_root is None:
        return ""
    cache_key = str(entry.get("cache_key", "") or batch_item.get("text_extract_cache_key", "") or "").strip()
    output_path = permanent_summary_path(library_root, batch_item, cache_key)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(str(entry.get("markdown", "")).strip() + "\n", encoding="utf-8")

    report_id = report_id_for_summary(batch_item, cache_key)
    batch_item["report_id"] = report_id
    batch_item["batch_id"] = artifact_batch_id(batch_item, library_root)
    batch_item["summary_md_path"] = str(output_path)
    if upsert_report is not None and db_path_for_library is not None:
        try:
            db_path = db_path_for_library(library_root)
            upsert_report(
                db_path,
                {
                    "report_id": report_id,
                    "pdf_sha256": str(batch_item.get("pdf_sha256", "") or cache_key).strip(),
                    "title": str(entry.get("title", "") or Path(str(batch_item.get("filename", ""))).stem).strip(),
                    "pdf_path": str(batch_item.get("path", "") or "").strip(),
                    "raw_md_path": str(batch_item.get("raw_md_path", "") or "").strip(),
                    "clean_md_path": str(batch_item.get("clean_md_path", "") or "").strip(),
                    "summary_md_path": str(output_path),
                    "downloaded_at": str(batch_item.get("modified_at", "") or "").strip(),
                    "index_status": "summary_created",
                },
            )
            if record_event is not None:
                record_event(
                    db_path,
                    {
                        "report_id": report_id,
                        "pdf_sha256": str(batch_item.get("pdf_sha256", "") or cache_key).strip(),
                        "batch_id": str(batch_item.get("batch_id", "") or ""),
                        "status": "summary_created",
                        "artifact_path": str(output_path),
                    },
                )
        except Exception:
            pass
    return str(output_path)


def build_chunk_summary_markdown(entries: list[dict[str, Any]]) -> str:
    parts = [str(entry.get("markdown", "")).strip() for entry in entries if str(entry.get("markdown", "")).strip()]
    return "\n\n".join(parts).strip()


def read_summary_markdown_for_publish(item: dict[str, Any]) -> tuple[str, str]:
    filename = str(item.get("filename", "") or item.get("path", "") or "未命名文件").strip()
    summary_path_raw = str(item.get("summary_md_path", "") or "").strip()
    if not summary_path_raw:
        raise SystemExit(f"missing summary_md_path for {filename}")

    summary_path = Path(summary_path_raw).expanduser()
    if not summary_path.exists():
        raise SystemExit(f"summary markdown missing for {filename}: {summary_path}")

    markdown = summary_path.read_text(encoding="utf-8", errors="replace").strip()
    if not markdown:
        raise SystemExit(f"summary markdown empty for {filename}: {summary_path}")
    return str(summary_path), markdown


def read_nonempty_markdown(path: Path) -> str:
    if not path.exists():
        raise SystemExit(f"summary markdown missing: {path}")
    markdown = path.read_text(encoding="utf-8", errors="replace").strip()
    if not markdown:
        raise SystemExit(f"summary markdown empty: {path}")
    return markdown


def build_lark_cli_create_markdown(batch_file: Path, summary_markdown: Path, output: Path) -> int:
    markdown = read_nonempty_markdown(summary_markdown)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(f"{markdown}\n", encoding="utf-8")
    print(str(output))
    return 0


def print_doc_title(batch_file: Path) -> int:
    batch = load_json(batch_file)
    title = build_doc_title(batch).strip()
    if not title:
        raise SystemExit(f"document title is empty: {batch_file}")
    print(title)
    return 0


def stable_publish_batch_identity(batch: dict[str, Any]) -> dict[str, Any]:
    files = []
    for item in batch.get("files", []):
        if not isinstance(item, dict):
            continue
        files.append(
            {
                "path": str(item.get("path", "")).strip(),
                "filename": str(item.get("filename", "")).strip(),
                "title": str(item.get("title", "")).strip(),
                "report_id": str(item.get("report_id", "")).strip(),
                "modified_at": str(item.get("modified_at", "")).strip(),
                "summary_md_path": str(item.get("summary_md_path", "")).strip(),
                "text_extract_cache_key": str(item.get("text_extract_cache_key", "")).strip(),
            }
        )
    return {
        "files": files,
        "new_pdf_count": int(batch.get("new_pdf_count", len(files)) or 0),
        "publish_group_index": int(batch.get("publish_group_index", batch.get("chunk_index", 1)) or 1),
        "publish_group_total": int(batch.get("publish_group_total", batch.get("chunk_total", 1)) or 1),
    }


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def build_publish_key_payload(batch_file: Path, summary_markdown: Path, target_doc_url: str) -> dict[str, Any]:
    batch = load_json(batch_file)
    summary = read_nonempty_markdown(summary_markdown)
    batch_hash = sha256_json(stable_publish_batch_identity(batch))
    summary_hash = sha256_text(summary)
    target_doc_url = str(target_doc_url or "").strip()
    publish_key = sha256_json(
        {
            "batch_hash": batch_hash,
            "summary_hash": summary_hash,
            "target_doc_url": target_doc_url,
        }
    )
    return {
        "publish_key": publish_key,
        "batch_hash": batch_hash,
        "summary_hash": summary_hash,
        "target_doc_url": target_doc_url,
    }


def build_publish_key(batch_file: Path, summary_markdown: Path, target_doc_url: str) -> int:
    print(json.dumps(build_publish_key_payload(batch_file, summary_markdown, target_doc_url), ensure_ascii=False))
    return 0


def iter_publish_records(records_file: Path) -> list[dict[str, Any]]:
    if not records_file.exists():
        return []
    records = []
    for line in records_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except Exception:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def report_date_from_batch(batch: dict[str, Any]) -> str:
    candidates = [
        batch.get("report_date"),
        batch.get("generated_at"),
        batch.get("latest_modified_at"),
    ]
    for item in batch.get("files", []):
        if isinstance(item, dict):
            candidates.append(item.get("modified_at"))
            break
    for raw in candidates:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text[:10]):
                return text[:10]
    return ""


def publish_record_files(batch: dict[str, Any]) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    for item in batch.get("files", []):
        if not isinstance(item, dict):
            continue
        files.append(
            {
                "path": str(item.get("path", "")).strip(),
                "filename": str(item.get("filename", "")).strip(),
                "report_id": str(item.get("report_id", "")).strip(),
                "pdf_sha256": str(
                    item.get("pdf_sha256", "") or item.get("text_extract_cache_key", "")
                ).strip(),
            }
        )
    return files


def publish_record_report_date(record: dict[str, Any]) -> str:
    explicit = str(record.get("report_date", "")).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", explicit):
        return explicit
    created_at = str(record.get("created_at", "")).strip()
    if created_at:
        try:
            return datetime.fromisoformat(created_at.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", created_at[:10]):
                return created_at[:10]
    return ""


def publish_record_file_count(record: dict[str, Any], legacy_file_count: int) -> tuple[int, bool]:
    raw_count = record.get("file_count")
    try:
        file_count = int(raw_count)
    except (TypeError, ValueError):
        file_count = 0
    if file_count > 0:
        return file_count, False
    files = record.get("files")
    if isinstance(files, list) and files:
        return len(files), False
    return max(int(legacy_file_count), 1), True


def lookup_publish_record(records_file: Path, publish_key: str) -> int:
    publish_key = str(publish_key or "").strip()
    for record in reversed(iter_publish_records(records_file)):
        if (
            str(record.get("publish_key", "")).strip() == publish_key
            and str(record.get("status", "")).strip() == "success"
            and str(record.get("doc_url", "")).strip()
        ):
            print(json.dumps({"found": True, **record}, ensure_ascii=False))
            return 0
    print(json.dumps({"found": False, "publish_key": publish_key}, ensure_ascii=False))
    return 0


def lookup_publish_recovery(records_file: Path, batch_hash: str, summary_hash: str) -> int:
    batch_hash = str(batch_hash or "").strip()
    summary_hash = str(summary_hash or "").strip()
    for record in reversed(iter_publish_records(records_file)):
        status = str(record.get("status", "")).strip()
        if (
            str(record.get("batch_hash", "")).strip() == batch_hash
            and str(record.get("summary_hash", "")).strip() == summary_hash
            and status in {"remote_written", "success"}
            and str(record.get("doc_url", "")).strip()
        ):
            print(json.dumps({"found": True, **record}, ensure_ascii=False))
            return 0
    print(
        json.dumps(
            {"found": False, "batch_hash": batch_hash, "summary_hash": summary_hash},
            ensure_ascii=False,
        )
    )
    return 0


def lookup_latest_same_day_doc(
    records_file: Path,
    batch_file: Path,
    *,
    incoming_file_count: int,
    max_file_count: int = 20,
    legacy_file_count: int = 10,
) -> int:
    report_date = report_date_from_batch(load_json(batch_file))
    incoming_file_count = max(int(incoming_file_count), 0)
    max_file_count = max(int(max_file_count), 1)
    legacy_file_count = max(int(legacy_file_count), 1)
    docs: dict[str, dict[str, Any]] = {}
    seen_publish_keys: set[str] = set()

    for index, record in enumerate(iter_publish_records(records_file)):
        if str(record.get("status", "")).strip() != "success":
            continue
        if publish_record_report_date(record) != report_date:
            continue
        doc_url = str(record.get("doc_url", "")).strip()
        if not doc_url:
            continue
        publish_key = str(record.get("publish_key", "")).strip() or f"legacy-record-{index}"
        if publish_key in seen_publish_keys:
            continue
        seen_publish_keys.add(publish_key)
        file_count, estimated = publish_record_file_count(record, legacy_file_count)
        aggregate = docs.setdefault(
            doc_url,
            {
                "doc_url": doc_url,
                "file_count": 0,
                "legacy_estimated_record_count": 0,
                "latest_created_at": "",
                "latest_index": -1,
            },
        )
        aggregate["file_count"] += file_count
        aggregate["legacy_estimated_record_count"] += 1 if estimated else 0
        created_at = str(record.get("created_at", "")).strip()
        if (created_at, index) >= (aggregate["latest_created_at"], aggregate["latest_index"]):
            aggregate["latest_created_at"] = created_at
            aggregate["latest_index"] = index

    if not report_date or not docs:
        print(
            json.dumps(
                {
                    "found": False,
                    "reason": "same_day_doc_missing",
                    "report_date": report_date,
                    "incoming_file_count": incoming_file_count,
                    "max_file_count": max_file_count,
                },
                ensure_ascii=False,
            )
        )
        return 0

    latest = max(
        docs.values(),
        key=lambda item: (str(item["latest_created_at"]), int(item["latest_index"])),
    )
    used = int(latest["file_count"])
    fits = used + incoming_file_count <= max_file_count
    payload = {
        "found": fits,
        "reason": "capacity_available" if fits else "latest_same_day_doc_full",
        "report_date": report_date,
        "doc_url": latest["doc_url"] if fits else "",
        "latest_doc_url": latest["doc_url"],
        "current_file_count": used,
        "incoming_file_count": incoming_file_count,
        "max_file_count": max_file_count,
        "remaining_after": max(max_file_count - used - incoming_file_count, 0) if fits else 0,
        "legacy_estimated_record_count": int(latest["legacy_estimated_record_count"]),
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def append_publish_record(
    records_file: Path,
    publish_key: str,
    batch_file: Path,
    summary_markdown: Path,
    target_doc_url: str,
    doc_url: str,
    mode: str,
    publisher: str,
    status: str = "success",
    error_message: str = "",
) -> int:
    key_payload = build_publish_key_payload(batch_file, summary_markdown, target_doc_url)
    publish_key = str(publish_key or "").strip()
    if publish_key and publish_key != key_payload["publish_key"]:
        raise SystemExit("publish_key mismatch")
    status = str(status or "success").strip()
    if status not in {"intent", "remote_written", "success", "failed"}:
        raise SystemExit(f"unsupported publish status: {status}")
    doc_url = str(doc_url or "").strip()
    if status in {"remote_written", "success"} and not doc_url:
        raise SystemExit(f"doc_url is empty for publish status: {status}")
    batch = load_json(batch_file)
    files = publish_record_files(batch)
    record = {
        "schema_version": 2,
        "created_at": datetime.now().astimezone().isoformat(),
        "status": status,
        "publisher": str(publisher or "").strip(),
        "mode": str(mode or "").strip(),
        "publish_key": key_payload["publish_key"],
        "batch_hash": key_payload["batch_hash"],
        "summary_hash": key_payload["summary_hash"],
        "target_doc_url": key_payload["target_doc_url"],
        "doc_url": doc_url,
        "batch_file": str(batch_file),
        "summary_markdown": str(summary_markdown),
        "report_date": report_date_from_batch(batch),
        "file_count": len(files) if files else int(batch.get("new_pdf_count", 0) or 0),
        "files": files,
        "error": str(error_message or "").strip()[:1000] or None,
    }
    records_file.parent.mkdir(parents=True, exist_ok=True)
    with records_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    print(json.dumps(record, ensure_ascii=False))
    return 0


def normalize_doc_url_or_token(value: str, doc_url_base: str) -> str:
    value = str(value or "").strip().strip("\"'")
    if not value:
        return ""
    if re.match(r"https?://", value):
        return value
    if re.fullmatch(r"[A-Za-z0-9_-]{8,}", value):
        return f"{doc_url_base.rstrip('/')}/{value}"
    return ""


def collect_doc_candidates(value: Any) -> list[str]:
    candidates: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).lower()
            if any(part in key_text for part in ("url", "token", "document_id", "doc_id", "documentid")):
                if isinstance(item, (str, int, float)):
                    candidates.append(str(item))
            candidates.extend(collect_doc_candidates(item))
    elif isinstance(value, list):
        for item in value:
            candidates.extend(collect_doc_candidates(item))
    elif isinstance(value, str):
        candidates.extend(re.findall(r"https?://[^\s\"')<>]+", value))
    return candidates


def parse_lark_cli_doc_url(output_file: Path, error_file: Path | None, fallback_doc_url: str, doc_url_base: str) -> int:
    fallback = normalize_doc_url_or_token(fallback_doc_url, doc_url_base)
    if fallback:
        print(json.dumps({"doc_url": fallback, "source": "fallback"}, ensure_ascii=False))
        return 0

    texts = []
    for path in [output_file, error_file]:
        if path is not None and path.exists():
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
    raw_text = "\n".join(texts)
    candidates: list[str] = []
    for match in re.finditer(r"\{[\s\S]*\}", raw_text):
        try:
            candidates.extend(collect_doc_candidates(json.loads(match.group(0))))
        except Exception:
            continue
    candidates.extend(re.findall(r"https?://[^\s\"')<>]+", raw_text))

    for candidate in candidates:
        doc_url = normalize_doc_url_or_token(candidate, doc_url_base)
        if doc_url:
            print(json.dumps({"doc_url": doc_url, "source": "cli_output"}, ensure_ascii=False))
            return 0
    print(json.dumps({"doc_url": "", "source": ""}, ensure_ascii=False))
    return 0


def build_publish_group_payload(
    base_batch: dict[str, Any],
    files: list[dict[str, Any]],
    group_index: int,
    group_total: int,
    total_file_count: int,
    source_chunk_files: list[str],
) -> dict[str, Any]:
    payload = dict(base_batch)
    payload["publish_group_index"] = group_index
    payload["publish_group_total"] = group_total
    payload["source_chunk_files"] = source_chunk_files
    payload["chunk_index"] = group_index
    payload["chunk_total"] = group_total
    payload["total_pdf_count"] = total_file_count
    payload["new_pdf_count"] = len(files)
    payload["files"] = files
    return payload


def build_publish_groups(
    chunk_files: list[Path],
    output_dir: Path,
    doc_group_size: int,
    doc_group_threshold: int,
    total_file_count: int,
    group_start_index: int = 0,
    group_total_override: int = 0,
) -> int:
    if doc_group_size <= 0:
        raise SystemExit("doc_group_size must be greater than 0")
    if doc_group_threshold < 0:
        raise SystemExit("doc_group_threshold must be greater than or equal to 0")

    loaded_chunks: list[tuple[Path, dict[str, Any]]] = []
    publish_files: list[dict[str, Any]] = []
    for chunk_path in chunk_files:
        chunk = load_json(chunk_path)
        loaded_chunks.append((chunk_path, chunk))
        for item in chunk.get("files", []):
            if not isinstance(item, dict):
                continue
            copied = dict(item)
            summary_path, markdown = read_summary_markdown_for_publish(copied)
            copied["_publish_summary_md_path"] = summary_path
            copied["_publish_summary_markdown"] = markdown
            publish_files.append(copied)

    output_dir.mkdir(parents=True, exist_ok=True)
    if not publish_files:
        print(json.dumps({"group_count": 0, "groups": []}, ensure_ascii=False))
        return 0

    effective_total = total_file_count if total_file_count > 0 else len(publish_files)
    group_size = doc_group_size if effective_total > doc_group_threshold else len(publish_files)
    local_group_total = math.ceil(len(publish_files) / group_size)
    display_group_start = group_start_index if group_start_index > 0 else 1
    display_group_total = group_total_override if group_total_override > 0 else local_group_total
    if display_group_start + local_group_total - 1 > display_group_total:
        raise SystemExit("group_total must cover all generated publish groups")
    base_batch = loaded_chunks[0][1]
    source_chunk_files = [str(path) for path, _chunk in loaded_chunks]
    groups: list[dict[str, Any]] = []

    for index in range(local_group_total):
        start = index * group_size
        end = start + group_size
        display_index = display_group_start + index
        group_files = [dict(item) for item in publish_files[start:end]]
        entries: list[dict[str, Any]] = []
        for item in group_files:
            markdown = str(item.pop("_publish_summary_markdown", "")).strip()
            summary_path = str(item.pop("_publish_summary_md_path", "")).strip()
            entries.append(
                {
                    "path": str(item.get("path", "")).strip(),
                    "filename": str(item.get("filename", "")).strip(),
                    "title": str(item.get("title", "") or Path(str(item.get("filename", ""))).stem).strip(),
                    "quality_hint": str(item.get("quality_hint", "")).strip(),
                    "markdown_path": summary_path,
                    "permanent_markdown_path": summary_path,
                    "markdown": markdown,
                }
            )

        batch_path = output_dir / f"publish-group-{display_index:03d}.batch.json"
        summary_json_path = output_dir / f"publish-group-{display_index:03d}.summary.json"
        summary_markdown_path = output_dir / f"publish-group-{display_index:03d}.summary.md"

        batch_payload = build_publish_group_payload(
            base_batch=base_batch,
            files=group_files,
            group_index=display_index,
            group_total=display_group_total,
            total_file_count=effective_total,
            source_chunk_files=source_chunk_files,
        )
        summary_payload = {
            "schema_version": 1,
            "summary_cache_version": SUMMARY_CACHE_VERSION,
            "generated_at": datetime.now().astimezone().isoformat(),
            "batch_file": str(batch_path),
            "chunk_index": display_index,
            "chunk_total": display_group_total,
            "handled_count": len(entries),
            "handled_paths": [entry["path"] for entry in entries],
            "entries": [
                {
                    "path": entry["path"],
                    "filename": entry["filename"],
                    "title": entry["title"],
                    "quality_hint": entry["quality_hint"],
                    "markdown_path": entry["markdown_path"],
                    "permanent_markdown_path": entry["permanent_markdown_path"],
                }
                for entry in entries
            ],
        }

        save_json(batch_path, batch_payload)
        save_json(summary_json_path, summary_payload)
        summary_markdown_path.write_text(build_chunk_summary_markdown(entries) + "\n", encoding="utf-8")
        groups.append(
            {
                "batch_file": str(batch_path),
                "summary_json": str(summary_json_path),
                "summary_markdown": str(summary_markdown_path),
                "file_count": len(group_files),
                "group_index": display_index,
                "group_total": display_group_total,
            }
        )

    print(json.dumps({"group_count": len(groups), "groups": groups}, ensure_ascii=False))
    return 0


def normalize_summary_entry(entry: dict[str, Any], batch_item: dict[str, Any]) -> dict[str, Any]:
    path = str(entry.get("path", "")).strip()
    filename = str(entry.get("filename", "")).strip() or str(batch_item.get("filename", "")).strip()
    title = str(entry.get("title", "")).strip()
    quality_hint = str(entry.get("quality_hint", "")).strip()
    markdown = str(entry.get("markdown", "")).strip()

    if not path:
        raise SystemExit("summary entry path is empty")
    if not filename:
        raise SystemExit(f"summary entry filename is empty: {path}")
    if not title:
        raise SystemExit(f"summary entry title is empty: {filename}")
    if not markdown:
        raise SystemExit(f"summary entry markdown is empty: {filename}")

    return {
        "path": path,
        "filename": filename,
        "title": title,
        "quality_hint": quality_hint,
        "markdown": markdown,
        "cache_key": str(batch_item.get("text_extract_cache_key", "")).strip(),
        "source_text_path": normalize_text_path(batch_item.get("extracted_text_path")),
        "source_text_chars": int(batch_item.get("extracted_text_chars", 0) or 0),
        "text_source": str(batch_item.get("text_source", "")).strip(),
        "text_warning": str(batch_item.get("text_extract_warning", "")).strip(),
    }


def validate_summary_result(batch_file: Path, result_file: Path) -> int:
    batch = load_json(batch_file)
    result = extract_summary_payload(result_file)

    status = str(result.get("status", "")).strip().lower()
    if status != "success":
        error = str(result.get("error", "")).strip() or "summary agent did not report success"
        raise SystemExit(error)

    batch_items = list(batch.get("files", []))
    expected_paths = [str(item.get("path", "")).strip() for item in batch_items]
    expected_paths = [path for path in expected_paths if path]
    handled_paths = _validate_handled_paths(expected_paths, result.get("handled_paths", []))

    handled_count = int(result.get("handled_count", -1))
    if handled_count != len(expected_paths):
        raise SystemExit(
            f"handled_count mismatch: expected {len(expected_paths)}, got {handled_count}"
        )

    summaries = result.get("summaries", [])
    if not isinstance(summaries, list):
        raise SystemExit("summaries must be a list")
    if len(summaries) != len(expected_paths):
        raise SystemExit(
            f"summary entry count mismatch: expected {len(expected_paths)}, got {len(summaries)}"
        )

    batch_by_path = {str(item.get("path", "")).strip(): item for item in batch_items if str(item.get("path", "")).strip()}
    seen_paths: set[str] = set()
    normalized_entries: list[dict[str, Any]] = []
    for entry in summaries:
        if not isinstance(entry, dict):
            raise SystemExit("each summary entry must be an object")
        path = str(entry.get("path", "")).strip()
        if path not in batch_by_path:
            raise SystemExit(f"summary entry path is outside current chunk: {path}")
        if path in seen_paths:
            raise SystemExit(f"duplicate summary entry path: {path}")
        seen_paths.add(path)
        normalized_entries.append(normalize_summary_entry(entry, batch_by_path[path]))

    payload = {
        "status": "success",
        "handled_count": handled_count,
        "handled_paths": handled_paths,
        "summary_count": len(normalized_entries),
        "summaries": [
            {
                "path": entry["path"],
                "filename": entry["filename"],
                "title": entry["title"],
                "quality_hint": entry["quality_hint"],
            }
            for entry in normalized_entries
        ],
        "chunk_index": int(batch.get("chunk_index", 1)),
        "chunk_total": int(batch.get("chunk_total", 1)),
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def persist_summary_artifacts(
    batch_file: Path,
    result_file: Path,
    summary_cache_dir: Path,
    output_json: Path,
    output_markdown: Path,
) -> int:
    batch = load_json(batch_file)
    result = extract_summary_payload(result_file)

    if str(result.get("status", "")).strip().lower() != "success":
        raise SystemExit("cannot persist failed summary result")

    batch_items = list(batch.get("files", []))
    batch_by_path = {str(item.get("path", "")).strip(): item for item in batch_items if str(item.get("path", "")).strip()}
    ordered_entries: list[dict[str, Any]] = []

    for path in [str(item.get("path", "")).strip() for item in batch_items]:
        if not path:
            continue
        matching = next(
            (
                entry
                for entry in result.get("summaries", [])
                if isinstance(entry, dict) and str(entry.get("path", "")).strip() == path
            ),
            None,
        )
        if matching is None:
            raise SystemExit(f"missing summary entry for path: {path}")
        ordered_entries.append(normalize_summary_entry(matching, batch_by_path[path]))

    summary_cache_dir.mkdir(parents=True, exist_ok=True)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_markdown.parent.mkdir(parents=True, exist_ok=True)

    saved_entries: list[dict[str, Any]] = []
    generated_at = datetime.now().astimezone().isoformat()
    library_root = research_library_root_from_env()
    for entry in ordered_entries:
        cache_key = str(entry.get("cache_key", "")).strip()
        if not cache_key:
            raise SystemExit(f"missing text_extract_cache_key for {entry['filename']}")

        cache_json_path, cache_markdown_path = summary_cache_paths(summary_cache_dir, cache_key)
        cache_json_path.parent.mkdir(parents=True, exist_ok=True)
        cache_markdown_path.parent.mkdir(parents=True, exist_ok=True)

        cache_markdown_path.write_text(entry["markdown"].strip() + "\n", encoding="utf-8")
        batch_item = batch_by_path[entry["path"]]
        permanent_markdown_path = persist_permanent_summary(library_root, batch_item, entry)
        cache_payload = {
            "schema_version": 1,
            "summary_cache_version": SUMMARY_CACHE_VERSION,
            "generated_at": generated_at,
            "cache_key": cache_key,
            "path": entry["path"],
            "filename": entry["filename"],
            "title": entry["title"],
            "quality_hint": entry["quality_hint"],
            "markdown_path": str(cache_markdown_path),
            "permanent_markdown_path": permanent_markdown_path,
            "markdown_chars": len(entry["markdown"]),
            "source_text_path": entry["source_text_path"],
            "source_text_chars": entry["source_text_chars"],
            "text_source": entry["text_source"],
            "text_warning": entry["text_warning"],
        }
        save_json(cache_json_path, cache_payload)
        saved_entries.append(
            {
                "path": entry["path"],
                "filename": entry["filename"],
                "title": entry["title"],
                "quality_hint": entry["quality_hint"],
                "cache_key": cache_key,
                "json_path": str(cache_json_path),
                "markdown_path": str(cache_markdown_path),
                "permanent_markdown_path": permanent_markdown_path,
                "markdown": entry["markdown"],
            }
        )

    chunk_payload = {
        "schema_version": 1,
        "summary_cache_version": SUMMARY_CACHE_VERSION,
        "generated_at": generated_at,
        "batch_file": str(batch_file),
        "chunk_index": int(batch.get("chunk_index", 1)),
        "chunk_total": int(batch.get("chunk_total", 1)),
        "handled_count": len(saved_entries),
        "handled_paths": [entry["path"] for entry in saved_entries],
        "entries": [
            {
                "path": entry["path"],
                "filename": entry["filename"],
                "title": entry["title"],
                "quality_hint": entry["quality_hint"],
                "cache_key": entry["cache_key"],
                "json_path": entry["json_path"],
                "markdown_path": entry["markdown_path"],
                "permanent_markdown_path": entry.get("permanent_markdown_path", ""),
            }
            for entry in saved_entries
        ],
    }
    chunk_markdown = build_chunk_summary_markdown(saved_entries)
    save_json(output_json, chunk_payload)
    output_markdown.write_text(chunk_markdown + "\n", encoding="utf-8")
    save_json(batch_file, batch)

    print(
        json.dumps(
            {
                "ok": True,
                "handled_count": len(saved_entries),
                "output_json": str(output_json),
                "output_markdown": str(output_markdown),
            },
            ensure_ascii=False,
        )
    )
    return 0


def materialize_summary_cache(
    batch_file: Path,
    summary_cache_dir: Path,
    output_json: Path,
    output_markdown: Path,
) -> int:
    batch = load_json(batch_file)
    batch_items = list(batch.get("files", []))
    loaded_entries: list[dict[str, Any]] = []
    library_root = research_library_root_from_env()

    for item in batch_items:
        path = str(item.get("path", "")).strip()
        filename = str(item.get("filename", "")).strip() or path
        cache_key = str(item.get("text_extract_cache_key", "")).strip()
        if not path or not cache_key:
            print(json.dumps({"ok": False, "reason": f"missing cache key for {filename}"}, ensure_ascii=False))
            return 0

        cache_json_path, cache_markdown_path = summary_cache_paths(summary_cache_dir, cache_key)
        if not cache_json_path.exists() or not cache_markdown_path.exists():
            print(json.dumps({"ok": False, "reason": f"summary cache missing for {filename}"}, ensure_ascii=False))
            return 0

        try:
            cache_payload = load_json(cache_json_path)
        except Exception:
            print(json.dumps({"ok": False, "reason": f"summary cache unreadable for {filename}"}, ensure_ascii=False))
            return 0

        if str(cache_payload.get("summary_cache_version", "")).strip() != SUMMARY_CACHE_VERSION:
            print(json.dumps({"ok": False, "reason": f"summary cache version mismatch for {filename}"}, ensure_ascii=False))
            return 0
        if _normalize_path_for_compare(str(cache_payload.get("path", "")).strip()) != _normalize_path_for_compare(path):
            print(json.dumps({"ok": False, "reason": f"summary cache path mismatch for {filename}"}, ensure_ascii=False))
            return 0

        markdown = cache_markdown_path.read_text(encoding="utf-8", errors="replace").strip()
        if not markdown:
            print(json.dumps({"ok": False, "reason": f"summary markdown empty for {filename}"}, ensure_ascii=False))
            return 0

        permanent_markdown_path = persist_permanent_summary(
            library_root,
            item,
            {
                "cache_key": cache_key,
                "title": str(cache_payload.get("title", "")).strip(),
                "markdown": markdown,
            },
        )

        loaded_entries.append(
            {
                "path": path,
                "filename": filename,
                "title": str(cache_payload.get("title", "")).strip(),
                "quality_hint": str(cache_payload.get("quality_hint", "")).strip(),
                "cache_key": cache_key,
                "json_path": str(cache_json_path),
                "markdown_path": str(cache_markdown_path),
                "permanent_markdown_path": permanent_markdown_path,
                "markdown": markdown,
            }
        )

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_markdown.parent.mkdir(parents=True, exist_ok=True)
    chunk_payload = {
        "schema_version": 1,
        "summary_cache_version": SUMMARY_CACHE_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(),
        "batch_file": str(batch_file),
        "chunk_index": int(batch.get("chunk_index", 1)),
        "chunk_total": int(batch.get("chunk_total", 1)),
        "handled_count": len(loaded_entries),
        "handled_paths": [entry["path"] for entry in loaded_entries],
        "entries": [
            {
                "path": entry["path"],
                "filename": entry["filename"],
                "title": entry["title"],
                "quality_hint": entry["quality_hint"],
                "cache_key": entry["cache_key"],
                "json_path": entry["json_path"],
                "markdown_path": entry["markdown_path"],
                "permanent_markdown_path": entry.get("permanent_markdown_path", ""),
            }
            for entry in loaded_entries
        ],
    }
    save_json(output_json, chunk_payload)
    output_markdown.write_text(build_chunk_summary_markdown(loaded_entries) + "\n", encoding="utf-8")
    save_json(batch_file, batch)
    print(
        json.dumps(
            {
                "ok": True,
                "handled_count": len(loaded_entries),
                "output_json": str(output_json),
                "output_markdown": str(output_markdown),
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    args = parse_args()

    if args.command == "split":
        return split_batch(
            batch_file=Path(args.batch_file).expanduser().resolve(),
            output_dir=Path(args.output_dir).expanduser().resolve(),
            chunk_size=args.chunk_size,
        )

    if args.command == "render-prompt":
        return render_prompt(
            template_path=Path(args.template).expanduser().resolve(),
            batch_file=Path(args.batch_file).expanduser().resolve(),
            system_prompt_file=(
                Path(args.system_prompt_file).expanduser().resolve()
                if args.system_prompt_file.strip()
                else None
            ),
            output_path=Path(args.output).expanduser().resolve(),
        )

    if args.command == "validate-summary":
        return validate_summary_result(
            batch_file=Path(args.batch_file).expanduser().resolve(),
            result_file=Path(args.result_file).expanduser().resolve(),
        )

    if args.command == "check-text-ready":
        return check_batch_text_ready(
            batch_file=Path(args.batch_file).expanduser().resolve(),
        )

    if args.command == "inspect-output":
        return inspect_agent_output(
            result_file=Path(args.result_file).expanduser().resolve(),
        )

    if args.command == "build-lark-cli-create-markdown":
        return build_lark_cli_create_markdown(
            batch_file=Path(args.batch_file).expanduser().resolve(),
            summary_markdown=Path(args.summary_markdown).expanduser().resolve(),
            output=Path(args.output).expanduser().resolve(),
        )

    if args.command == "build-doc-title":
        return print_doc_title(
            batch_file=Path(args.batch_file).expanduser().resolve(),
        )

    if args.command == "build-publish-key":
        return build_publish_key(
            batch_file=Path(args.batch_file).expanduser().resolve(),
            summary_markdown=Path(args.summary_markdown).expanduser().resolve(),
            target_doc_url=args.target_doc_url.strip(),
        )

    if args.command == "lookup-publish-record":
        return lookup_publish_record(
            records_file=Path(args.records_file).expanduser().resolve(),
            publish_key=args.publish_key.strip(),
        )

    if args.command == "lookup-publish-recovery":
        return lookup_publish_recovery(
            records_file=Path(args.records_file).expanduser().resolve(),
            batch_hash=args.batch_hash.strip(),
            summary_hash=args.summary_hash.strip(),
        )

    if args.command == "lookup-latest-same-day-doc":
        return lookup_latest_same_day_doc(
            records_file=Path(args.records_file).expanduser().resolve(),
            batch_file=Path(args.batch_file).expanduser().resolve(),
            incoming_file_count=args.incoming_file_count,
            max_file_count=args.max_file_count,
            legacy_file_count=args.legacy_file_count,
        )

    if args.command == "append-publish-record":
        return append_publish_record(
            records_file=Path(args.records_file).expanduser().resolve(),
            publish_key=args.publish_key.strip(),
            batch_file=Path(args.batch_file).expanduser().resolve(),
            summary_markdown=Path(args.summary_markdown).expanduser().resolve(),
            target_doc_url=args.target_doc_url.strip(),
            doc_url=args.doc_url.strip(),
            mode=args.mode.strip(),
            publisher=args.publisher.strip(),
            status=args.status.strip(),
            error_message=args.error.strip(),
        )

    if args.command == "parse-lark-cli-doc-url":
        return parse_lark_cli_doc_url(
            output_file=Path(args.output_file).expanduser().resolve(),
            error_file=Path(args.error_file).expanduser().resolve() if args.error_file.strip() else None,
            fallback_doc_url=args.fallback_doc_url.strip(),
            doc_url_base=args.doc_url_base.strip(),
        )

    if args.command == "persist-summary":
        return persist_summary_artifacts(
            batch_file=Path(args.batch_file).expanduser().resolve(),
            result_file=Path(args.result_file).expanduser().resolve(),
            summary_cache_dir=Path(args.summary_cache_dir).expanduser().resolve(),
            output_json=Path(args.output_json).expanduser().resolve(),
            output_markdown=Path(args.output_markdown).expanduser().resolve(),
        )

    if args.command == "materialize-summary-cache":
        return materialize_summary_cache(
            batch_file=Path(args.batch_file).expanduser().resolve(),
            summary_cache_dir=Path(args.summary_cache_dir).expanduser().resolve(),
            output_json=Path(args.output_json).expanduser().resolve(),
            output_markdown=Path(args.output_markdown).expanduser().resolve(),
        )

    if args.command == "build-publish-groups":
        return build_publish_groups(
            chunk_files=[Path(item).expanduser().resolve() for item in args.chunk_files],
            output_dir=Path(args.output_dir).expanduser().resolve(),
            doc_group_size=args.doc_group_size,
            doc_group_threshold=args.doc_group_threshold,
            total_file_count=args.total_file_count,
            group_start_index=args.group_start_index,
            group_total_override=args.group_total,
        )

    if args.command == "update-quarantine":
        return update_quarantine(
            batch_file=Path(args.batch_file).expanduser().resolve(),
            quarantine_file=Path(args.quarantine_file).expanduser().resolve(),
            run_at=args.run_at.strip(),
        )

    if args.command == "clear-quarantine":
        return clear_quarantine(
            batch_file=Path(args.batch_file).expanduser().resolve(),
            quarantine_file=Path(args.quarantine_file).expanduser().resolve(),
            run_at=args.run_at.strip(),
        )

    if args.command == "inspect-quarantine":
        return inspect_quarantine(
            quarantine_file=Path(args.quarantine_file).expanduser().resolve(),
            output_path=Path(args.output).expanduser().resolve() if args.output.strip() else None,
        )

    if args.command == "record-stage-retry":
        return record_stage_retry(
            batch_file=Path(args.batch_file).expanduser().resolve(),
            ledger_file=Path(args.ledger_file).expanduser().resolve(),
            stage=args.stage.strip(),
            run_at=args.run_at.strip(),
            workflow_version=args.workflow_version.strip(),
            error_code_override=args.error_code.strip(),
            error_type_override=args.error_type.strip(),
            retryable_override=args.retryable.strip(),
            message_override=args.message.strip(),
            max_attempts=args.max_attempts,
            delays_minutes=args.retry_delays_minutes.strip(),
        )

    if args.command == "resolve-stage-retry":
        return resolve_stage_retry(
            batch_file=Path(args.batch_file).expanduser().resolve(),
            ledger_file=Path(args.ledger_file).expanduser().resolve(),
            stage=args.stage.strip(),
            run_at=args.run_at.strip(),
            workflow_version=args.workflow_version.strip(),
        )

    if args.command == "filter-stage-retries":
        return filter_stage_retries(
            batch_file=Path(args.batch_file).expanduser().resolve(),
            ledger_file=Path(args.ledger_file).expanduser().resolve(),
            output=Path(args.output).expanduser().resolve(),
            stage=args.stage.strip(),
            run_at=args.run_at.strip(),
            workflow_version=args.workflow_version.strip(),
        )

    if args.command == "stage-retry-status":
        return stage_retry_status(
            ledger_file=Path(args.ledger_file).expanduser().resolve(),
            workflow_version=args.workflow_version.strip(),
        )

    if args.command == "notification-outbox-enqueue":
        return notification_outbox_enqueue(
            outbox_file=Path(args.outbox_file).expanduser().resolve(),
            idempotency_key=args.idempotency_key.strip(),
            supersede_scope=args.supersede_scope.strip(),
            event=args.event.strip(),
            message_format=args.format.strip(),
            message_file=Path(args.message_file).expanduser().resolve(),
            run_id=args.run_id.strip(),
            run_at=args.run_at.strip(),
        )

    if args.command == "notification-outbox-next-due":
        return notification_outbox_next_due(
            outbox_file=Path(args.outbox_file).expanduser().resolve(),
            run_at=args.run_at.strip(),
        )

    if args.command == "notification-outbox-record":
        return notification_outbox_record(
            outbox_file=Path(args.outbox_file).expanduser().resolve(),
            idempotency_key=args.idempotency_key.strip(),
            run_at=args.run_at.strip(),
            status=args.status.strip(),
            message_id=args.message_id.strip(),
            error=args.error.strip(),
        )

    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
