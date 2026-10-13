"""Writer-owned summary index. MCP readers must never import this module.

The catalog is the inventory, Markdown is the evidence, and report_search is
candidate discovery. No Obsidian note or remote publication is required.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_SOURCE_BYTES = 5_000_000
SCHEMA_VERSION = 2
IDENTITY_FIELDS = (
    "report_id",
    "title",
    "broker",
    "report_date",
    "companies_json",
    "tickers_json",
    "pdf_sha256",
)


def identity_sha256(row: dict) -> str:
    """Catalog binding, not semantic/PDF verification. Shared wire format with reader."""
    return hashlib.sha256(
        json.dumps(
            {key: str(row.get(key) or "") for key in IDENTITY_FIELDS},
            sort_keys=True,
            ensure_ascii=True,
        ).encode()
    ).hexdigest()


SENSITIVE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:(?-i:sk-)[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|AKIA[A-Z0-9]{16})\b|"
    r"^\s*(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|private[_-]?key)\s*[:=]\s*[^\s#]{8,}",
    re.IGNORECASE | re.MULTILINE,
)


def ensure_evidence_schema(conn: sqlite3.Connection) -> None:
    columns = {r[1] for r in conn.execute("PRAGMA table_info(reports)")}
    for name in (
        "broker",
        "report_date",
        "companies_json",
        "tickers_json",
        "themes_json",
        "subthemes_json",
    ):
        if columns and name not in columns:
            conn.execute(
                f"ALTER TABLE reports ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
            )
    conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS report_search USING fts5(
        report_id UNINDEXED,title,broker,report_date UNINDEXED,companies,themes,
        subthemes,core_conclusions,core_questions_answers,summary_text,
        note_path UNINDEXED,pdf_path UNINDEXED,feishu_doc_url UNINDEXED,
        tokenize='trigram')""")
    conn.execute("""CREATE TABLE IF NOT EXISTS report_evidence_state (
        report_id TEXT PRIMARY KEY,source_kind TEXT NOT NULL,source_path TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,signature TEXT NOT NULL,status TEXT NOT NULL,
        reason TEXT NOT NULL,indexed_at TEXT NOT NULL,checked_at TEXT NOT NULL,
        first_pending_at TEXT NOT NULL,fts_rowid INTEGER,schema_version INTEGER NOT NULL)""")
    state_columns = {
        r[1] for r in conn.execute("PRAGMA table_info(report_evidence_state)")
    }
    for name, declaration in {
        "identity_sha256": "TEXT NOT NULL DEFAULT ''",
        "error_kind": "TEXT NOT NULL DEFAULT ''",
        "retry_count": "INTEGER NOT NULL DEFAULT 0",
        "next_retry_at": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in state_columns:
            conn.execute(
                f"ALTER TABLE report_evidence_state ADD COLUMN {name} {declaration}"
            )


def _needs_refresh(conn, old, row, signature, now):
    if not old or old["signature"] != signature:
        return True
    if old["status"] == "error":
        # Pre-migration I/O errors also receive one automatic retry.
        retryable = old["error_kind"] == "retryable_io" or (
            not old["error_kind"]
            and "Error:" in old["reason"]
            and "ValueError:" not in old["reason"]
            and "Unicode" not in old["reason"]
        )
        return retryable and (not old["next_retry_at"] or old["next_retry_at"] <= now)
    if old["status"] == "ready":
        if not old["identity_sha256"]:
            return True
        owned = conn.execute(
            "SELECT report_id FROM report_search WHERE rowid=? AND note_path=''",
            (old["fts_rowid"],),
        ).fetchone()
        return not (owned and owned[0] == row["report_id"])
    return False


def _signature(row: dict) -> str:
    paths = []
    for key in ("summary_md_path", "obsidian_note_path", "pdf_path"):
        raw = row.get(key, "")
        try:
            s = Path(raw).stat() if raw else None
            sig = (
                [s.st_dev, s.st_ino, s.st_mtime_ns, s.st_ctime_ns, s.st_size]
                if s
                else []
            )
        except OSError:
            sig = []
        paths.append([raw, sig])
    metadata = {
        k: row.get(k, "")
        for k in (
            "title",
            "broker",
            "report_date",
            "companies_json",
            "tickers_json",
            "themes_json",
            "subthemes_json",
            "pdf_sha256",
            "feishu_doc_url",
        )
    }
    return hashlib.sha256(
        json.dumps([paths, metadata], sort_keys=True).encode()
    ).hexdigest()


def _source(row: dict) -> tuple[str, str, str, str, str]:
    errors = []
    for key, kind in (("summary_md_path", "summary"), ("obsidian_note_path", "note")):
        raw = str(row.get(key) or "")
        if not raw:
            continue
        try:
            path = Path(raw)
            if path.suffix.lower() not in (".md", ".markdown"):
                raise ValueError("source_invalid")
            if path.stat().st_size > MAX_SOURCE_BYTES:
                raise ValueError("source_too_large")
            before = path.stat()
            data = path.read_bytes()
            after = path.stat()
            if (
                before.st_dev,
                before.st_ino,
                before.st_mtime_ns,
                before.st_ctime_ns,
                before.st_size,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_mtime_ns,
                after.st_ctime_ns,
                after.st_size,
            ) or len(data) > MAX_SOURCE_BYTES:
                raise OSError("source_changed_during_read")
            text = data.decode("utf-8")
            unescaped = re.sub(r"\\([\\`*_{}\[\]()#+\-.!])", r"\1", text)
            if not text.strip() or "\x00" in text or SENSITIVE.search(unescaped):
                raise ValueError("source_empty_or_unsafe")
            return kind, raw, text, hashlib.sha256(data).hexdigest(), ""
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(
                ("io:" if isinstance(exc, OSError) else "quarantine:")
                + type(exc).__name__
                + ":"
                + str(exc)[:100]
            )
            # Do not silently replace a declared failed primary with another source.
            break
    pdf = str(row.get("pdf_path") or "")
    if errors:
        return "", "", "", "", "; ".join(errors)
    return (
        "",
        "",
        "",
        "",
        "pdf_only" if pdf and Path(pdf).is_file() else "files_missing",
    )


def refresh_catalog_source(
    conn: sqlite3.Connection,
    row: dict,
    *,
    force: bool = False,
    expected_source_sha256: str = "",
    now: datetime | None = None,
) -> dict:
    """Upsert only the FTS row owned by this writer; preserve distinct old notes."""
    ensure_evidence_schema(conn)
    conn.row_factory = sqlite3.Row
    report_id = row["report_id"]
    old = conn.execute(
        "SELECT * FROM report_evidence_state WHERE report_id=?", (report_id,)
    ).fetchone()
    signature = _signature(row)
    checked_time = now or datetime.now(timezone.utc)
    checked_at = checked_time.isoformat(timespec="seconds")
    if (
        not force
        and not expected_source_sha256
        and not _needs_refresh(conn, old, row, signature, checked_at)
    ):
        return {"changed": False, "status": old["status"], "reason": old["reason"]}
    kind, path, text, digest, reason = _source(row)
    # Derive missing date/broker from the report filename, never the import date.
    if text and (not row.get("broker") or not row.get("report_date")):
        try:
            from kb_common import BROKER_ALIASES, extract_report_date
        except ModuleNotFoundError:
            from scripts.kb_common import BROKER_ALIASES, extract_report_date
        identity = str(row.get("title") or "") + "\n" + Path(path).name
        row = dict(row)
        inferred_broker = ""
        for name in (str(row.get("title") or ""), Path(path).name):
            prefix = re.split(r"[-_]", re.sub(r"^(?:20\d{6}|2\d{5})[-_ ]+", "", name))[
                0
            ].strip()
            inferred_broker = next(
                (
                    broker
                    for broker, aliases in BROKER_ALIASES.items()
                    if prefix.casefold() in {a.casefold() for a in aliases}
                ),
                "",
            )
            if inferred_broker:
                break
        row["broker"] = row.get("broker") or inferred_broker
        row["report_date"] = row.get("report_date") or extract_report_date({}, identity)
        conn.execute(
            "UPDATE reports SET broker=?,report_date=? WHERE report_id=?",
            (row["broker"], row["report_date"], report_id),
        )
        signature = _signature(row)
    binding = identity_sha256(row)
    attested = bool(expected_source_sha256 and expected_source_sha256 == digest)
    changed_binding = (
        old and old["identity_sha256"] and old["identity_sha256"] != binding
    )
    changed_content = old and old["content_sha256"] and old["content_sha256"] != digest
    legacy_binding_changed = (
        old
        and old["content_sha256"]
        and not old["identity_sha256"]
        and old["signature"] != signature
    )
    if text and (expected_source_sha256 and not attested):
        text, reason = "", "source_attestation_mismatch"
    elif (
        text
        and old
        and old["content_sha256"]
        and (changed_binding or changed_content or legacy_binding_changed)
        and not attested
    ):
        text, reason = "", "source_changed_requires_writer_attestation"
    status = (
        "ready"
        if text
        else reason
        if reason in ("pdf_only", "files_missing")
        else "source_changed"
        if reason.startswith("source_") and "attestation" in reason
        else "error"
    )
    error_kind = (
        "retryable_io"
        if status == "error" and reason.startswith("io:")
        else "quarantined"
        if status in ("error", "source_changed")
        else ""
    )
    retry_count = (
        (old["retry_count"] if old and old["error_kind"] == "retryable_io" else 0) + 1
        if error_kind == "retryable_io"
        else 0
    )
    next_retry_at = (
        (
            checked_time
            + timedelta(seconds=min(3600, 60 * 2 ** min(retry_count - 1, 6)))
        ).isoformat(timespec="seconds")
        if retry_count
        else ""
    )
    # Keep the accepted binding through errors. A forced reconcile is not approval.
    if status != "ready" and old and old["content_sha256"]:
        kind, path, digest, binding = (
            old["source_kind"],
            old["source_path"],
            old["content_sha256"],
            old["identity_sha256"],
        )
    if old and old["fts_rowid"] is not None:
        conn.execute(
            "DELETE FROM report_search WHERE rowid=? AND report_id=? AND note_path=''",
            (old["fts_rowid"], report_id),
        )
    fts_id = None
    if text:

        def tags(key: str) -> str:
            try:
                value = json.loads(row.get(key) or "[]")
                return (
                    " ".join(str(v) for v in value) if isinstance(value, list) else ""
                )
            except (ValueError, TypeError):
                return ""

        cur = conn.execute(
            """INSERT INTO report_search(
            report_id,title,broker,report_date,companies,themes,subthemes,
            core_conclusions,core_questions_answers,summary_text,note_path,pdf_path,feishu_doc_url)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                report_id,
                row.get("title", ""),
                row.get("broker", ""),
                row.get("report_date", ""),
                tags("companies_json"),
                tags("themes_json"),
                tags("subthemes_json"),
                "",
                "",
                text,
                "",
                row.get("pdf_path", ""),
                row.get("feishu_doc_url", ""),
            ),
        )
        fts_id = cur.lastrowid
    pending_at = (
        ""
        if status == "ready"
        else (
            old["first_pending_at"] if old and old["first_pending_at"] else checked_at
        )
    )
    conn.execute(
        """INSERT INTO report_evidence_state(report_id,source_kind,source_path,content_sha256,signature,status,reason,indexed_at,checked_at,first_pending_at,fts_rowid,schema_version,identity_sha256,error_kind,retry_count,next_retry_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(report_id) DO UPDATE SET source_kind=excluded.source_kind,
        source_path=excluded.source_path,content_sha256=excluded.content_sha256,
        signature=excluded.signature,status=excluded.status,reason=excluded.reason,
        indexed_at=excluded.indexed_at,checked_at=excluded.checked_at,
        first_pending_at=excluded.first_pending_at,fts_rowid=excluded.fts_rowid,
        schema_version=excluded.schema_version,identity_sha256=excluded.identity_sha256,
        error_kind=excluded.error_kind,retry_count=excluded.retry_count,next_retry_at=excluded.next_retry_at""",
        (
            report_id,
            kind,
            path,
            digest,
            signature,
            status,
            reason,
            checked_at if status == "ready" else (old["indexed_at"] if old else ""),
            checked_at,
            pending_at,
            fts_id,
            SCHEMA_VERSION,
            binding,
            error_kind,
            retry_count,
            next_retry_at,
        ),
    )
    return {"changed": True, "status": status, "reason": reason}


def refresh_report(db_path: Path, report_id: str) -> dict:
    with sqlite3.connect(str(db_path), timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM reports WHERE report_id=?", (report_id,)
        ).fetchone()
        if row is None:
            raise ValueError("catalog_report_missing")
        return refresh_catalog_source(conn, dict(row))


def reconcile_catalog(
    db_path: Path, *, max_updates: int = 250, now: datetime | None = None
) -> dict:
    if max_updates < 1:
        raise ValueError("max_updates must be positive")
    updated = pending = 0
    oldest_waiting = []
    checked_time = now or datetime.now(timezone.utc)
    now = checked_time.isoformat(timespec="seconds")
    with sqlite3.connect(str(db_path), timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        ensure_evidence_schema(conn)
        states = {
            r["report_id"]: dict(r)
            for r in conn.execute("SELECT * FROM report_evidence_state")
        }
        rows = [
            dict(r) for r in conn.execute("SELECT * FROM reports ORDER BY updated_at")
        ]
        rows.sort(key=lambda r: r["report_id"] in states)
        for row in rows:
            old = states.get(row["report_id"])
            sig = _signature(row)
            if not _needs_refresh(conn, old, row, sig, now):
                continue
            pending += 1
            if updated >= max_updates:
                oldest_waiting.append(
                    (old or {}).get("first_pending_at") or row.get("updated_at") or now
                )
                continue
            refresh_catalog_source(conn, row, force=True, now=checked_time)
            updated += 1
        current_ids = {r["report_id"] for r in rows}
        deleted = 0
        for report_id, state in states.items():
            if report_id not in current_ids:
                conn.execute(
                    "DELETE FROM report_search WHERE rowid=? AND report_id=? AND note_path=''",
                    (state["fts_rowid"], report_id),
                )
                conn.execute(
                    "DELETE FROM report_evidence_state WHERE report_id=?", (report_id,)
                )
                deleted += 1
        stats = dict(
            conn.execute(
                "SELECT status,count(*) FROM report_evidence_state GROUP BY status"
            ).fetchall()
        )
        oldest = conn.execute(
            "SELECT min(first_pending_at) FROM report_evidence_state WHERE status IN ('error','source_changed')"
        ).fetchone()[0]
        if oldest:
            oldest_waiting.append(oldest)
        latest = conn.execute(
            "SELECT max(indexed_at) FROM report_evidence_state WHERE status='ready'"
        ).fetchone()[0]
        errors = stats.get("error", 0) + stats.get("source_changed", 0)
    return {
        "catalog_seen": len(rows),
        "catalog_updated": updated,
        "catalog_pending": pending - updated,
        "catalog_errors": errors,
        "catalog_deleted": deleted,
        "source_status_counts": stats,
        "last_checked_at": now,
        "last_successful_index_at": latest,
        "oldest_pending_at": min(oldest_waiting, default=None),
        "catalog_complete": pending == updated and errors == 0,
    }
