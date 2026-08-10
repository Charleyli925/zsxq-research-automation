#!/usr/bin/env python3
"""Historical result parser and renderer for archived ZSXQ download runs.

The direct Python download pipeline writes its canonical result itself and
does not import this module. The Codex/Agent markers below are retained only
to render or inspect legacy run logs during migration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from zsxq_result_mapping import (
        REASON_TEXT_BY_CODE,
        classify_report_status,
        core_reason_from_no_download_reason,
        no_download_reason_from_core_reason,
        normalize_no_download_reason,
        normalize_reason_code,
    )
except ModuleNotFoundError:  # pragma: no cover - package import fallback
    from scripts.zsxq_result_mapping import (
        REASON_TEXT_BY_CODE,
        classify_report_status,
        core_reason_from_no_download_reason,
        no_download_reason_from_core_reason,
        normalize_no_download_reason,
        normalize_reason_code,
    )

REPORT_MARKER = "ZSXQ_REPORT_JSON:"
SCAN_ALERT_MARKER = "ZSXQ_SCAN_ALERT:"
PREFLIGHT_REASON_CODES = frozenset(
    {
        "blocked_browser_endpoint_unavailable",
        "blocked_browser_cdp_unresponsive",
        "need_reauth",
        "zsxq_page_unavailable",
        "zsxq_page_state_unrecognized",
    }
)
MANIFEST_MISSING_REASON_CODES = frozenset(
    {
        "source_content_protected",
        "playwright_action_timeout",
        "browser_download_unstable",
    }
)


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def iter_result_candidate_lines(text: str) -> list[str]:
    lines = text.splitlines()
    if not any(line.strip() == "codex" for line in lines):
        return lines

    selected: list[str] = []
    in_codex_output = False
    for line in lines:
        normalized = line.strip()
        if normalized == "codex":
            in_codex_output = True
            continue
        if normalized in {"user", "exec", "tokens used"} or normalized.startswith("mcp: "):
            in_codex_output = False
            continue
        if in_codex_output:
            selected.append(line)
    return selected


def iter_plain_marker_payloads(text: str, marker: str) -> list[str]:
    values: list[str] = []
    candidate_lines = iter_result_candidate_lines(text)
    recent_nonempty: list[str] = []
    for line in candidate_lines:
        normalized = line.strip()
        if not normalized or normalized.startswith("`") or normalized.endswith("`"):
            if normalized:
                recent_nonempty.append(normalized)
                recent_nonempty = recent_nonempty[-3:]
            continue
        if not normalized.startswith(marker):
            recent_nonempty.append(normalized)
            recent_nonempty = recent_nonempty[-3:]
            continue
        recent_context = " ".join(recent_nonempty).casefold()
        if "example" in recent_context or "示例" in recent_context:
            recent_nonempty.append(normalized)
            recent_nonempty = recent_nonempty[-3:]
            continue
        payload = normalized[len(marker):].strip()
        if payload:
            values.append(payload)
        recent_nonempty.append(normalized)
        recent_nonempty = recent_nonempty[-3:]
    return values


def parse_machine_report_text(text: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for raw in iter_plain_marker_payloads(text, REPORT_MARKER):
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if isinstance(payload, dict):
            parsed = payload
    return parsed


def parse_scan_alert_text(text: str) -> str:
    alert = ""
    for value in iter_plain_marker_payloads(text, SCAN_ALERT_MARKER):
        alert = value
    return alert


def looks_like_self_kill(exit_code: int, raw_text: str) -> bool:
    lowered = raw_text.casefold()
    return (
        exit_code == 143
        and "terminated: 15" in lowered
        and (
            "pgrep -af '/applications/codex.app/contents/resources/codex exec'" in lowered
            or 'pgrep -af "/applications/codex.app/contents/resources/codex exec"' in lowered
        )
    )


def has_cloud_requirements_timeout(raw_text: str) -> bool:
    lowered = raw_text.casefold()
    return "timed out waiting for cloud requirements after" in lowered


def has_codex_exec_timeout(raw_text: str) -> bool:
    return "ZSXQ_EXEC_TIMEOUT_JSON:" in str(raw_text or "")


def parse_preflight_reason_code(raw_text: str) -> str:
    """Return the last launcher-emitted diagnosis, ignoring prompt examples."""
    reason_code = ""
    pattern = re.compile(r"^\[BLOCKED\]\s+([a-z0-9_]+):")
    for line in str(raw_text or "").splitlines():
        match = pattern.match(line.strip())
        if match and match.group(1) in PREFLIGHT_REASON_CODES:
            reason_code = match.group(1)
    return reason_code


def derive_no_download_reason(*, codex_rc: int, downloaded_count: int, candidate_reason: str) -> str:
    if codex_rc == 0 and downloaded_count > 0:
        return "downloaded"
    if codex_rc == 0:
        return candidate_reason or "unknown"
    if codex_rc == 23:
        return "busy_locked"
    if codex_rc == 20:
        return "need_reauth"
    if codex_rc in {21, 22}:
        return "blocked_browser"
    if codex_rc == 143:
        return "task_interrupted"
    return "unknown"


def derive_reason_code(*, codex_rc: int, downloaded_count: int, no_download_reason: str, candidate_reason: str, raw_text: str) -> str:
    if candidate_reason:
        return candidate_reason
    lowered = raw_text.casefold()
    preflight_reason = parse_preflight_reason_code(raw_text)
    if preflight_reason:
        return preflight_reason
    if codex_rc == 20:
        return "need_reauth"
    if codex_rc == 21:
        return "blocked_browser_missing"
    if codex_rc == 22:
        if "browsertype.connect_over_cdp" in lowered and "timeout" in lowered:
            return "blocked_browser_cdp_unresponsive"
        if "cft cdp endpoint not ready" in lowered:
            return "blocked_browser_endpoint_unavailable"
        return "blocked_browser_unavailable_or_interrupted"
    if codex_rc == 23:
        return "busy_locked"
    if codex_rc == 143 and looks_like_self_kill(codex_rc, raw_text):
        return "self_terminated_codex_runner"
    if codex_rc == 143:
        return "task_interrupted_sigterm"
    if codex_rc == 124 or has_codex_exec_timeout(raw_text):
        return "codex_exec_timeout"
    if (
        codex_rc == 126
        and "operation not permitted" in lowered
        and "run_zsxq_task_via_codex.sh" in lowered
    ):
        # Historical Agent-launcher log marker; never a direct-pipeline input.
        return "blocked_documents_permission"
    if codex_rc != 0 and has_cloud_requirements_timeout(raw_text):
        return "cloud_requirements_timeout"
    if codex_rc != 0:
        return "task_failed"
    if downloaded_count > 0:
        return "download_completed"
    return core_reason_from_no_download_reason(no_download_reason) or "no_download_reason_unknown"


def build_manifest_canonical_result(
    *,
    state_path: Path,
    raw_output_path: Path,
    run_manifest_path: Path,
    scan_plan_path: Path | None,
    run_id: str,
    run_started_at: str,
    run_finished_at: str,
    requested_window_start: str,
    requested_window_end: str,
    pre_last_successful_check_at: str,
    codex_rc: int,
) -> dict[str, Any]:
    state = load_json(state_path)
    manifest = load_json(run_manifest_path)
    raw_output_text = load_text(raw_output_path)
    machine_report = parse_machine_report_text(raw_output_text) if codex_rc == 0 else {}
    plan = load_json(scan_plan_path) if scan_plan_path is not None else {}
    if not plan and isinstance(manifest.get("scan_snapshot"), dict):
        plan = manifest["scan_snapshot"]

    invariant_errors = [str(value) for value in (manifest.get("invariant_errors") or []) if str(value)]
    manifest_run_id = str(manifest.get("run_id") or "").strip()
    if not manifest:
        invariant_errors.append("run_manifest_missing_or_invalid")
    elif manifest_run_id != run_id:
        invariant_errors.append("run_id_mismatch")
    if plan and str(manifest.get("scan_plan_sha256") or "").strip():
        plan_sha256 = hashlib.sha256(
            json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if plan_sha256 != str(manifest.get("scan_plan_sha256") or "").strip():
            invariant_errors.append("scan_plan_changed_after_finalize")

    window_start = str(plan.get("window_start") or manifest.get("scan_window_start") or requested_window_start).strip()
    window_end = str(plan.get("window_end") or manifest.get("scan_window_end") or requested_window_end).strip()
    if not window_start or not window_end:
        invariant_errors.append("frozen_scan_window_missing")

    expected_candidates = plan.get("download_candidates")
    if not isinstance(expected_candidates, list):
        expected_candidates = manifest.get("expected_candidates")
    if not isinstance(expected_candidates, list):
        expected_candidates = []
        invariant_errors.append("scan_plan_candidates_missing")
    candidate_count = to_int(plan.get("download_candidate_count", len(expected_candidates)), len(expected_candidates))
    if candidate_count != len(expected_candidates):
        invariant_errors.append("scan_plan_candidate_count_mismatch")

    downloaded_entries = manifest.get("downloaded_entries")
    if not isinstance(downloaded_entries, list):
        downloaded_entries = []
    downloaded_files: list[str] = []
    seen_files: set[str] = set()
    for entry in downloaded_entries:
        if not isinstance(entry, dict):
            continue
        filename = str(entry.get("filename") or "").strip()
        if not filename or filename in seen_files:
            continue
        seen_files.add(filename)
        downloaded_files.append(filename)
    downloaded_count = len(downloaded_files)
    if downloaded_count != to_int(manifest.get("downloaded_count", downloaded_count), downloaded_count):
        invariant_errors.append("manifest_downloaded_count_mismatch")
    if downloaded_count > candidate_count:
        invariant_errors.append("downloaded_count_exceeds_candidate_count")

    satisfied_entries = manifest.get("satisfied_entries")
    if not isinstance(satisfied_entries, list):
        satisfied_entries = []
    satisfied_candidates: list[dict[str, Any]] = []
    satisfied_identities: set[str] = set()
    downloaded_identities = {
        str(entry.get("candidate_identity") or "").strip()
        for entry in downloaded_entries
        if isinstance(entry, dict) and str(entry.get("candidate_identity") or "").strip()
    }
    for entry in satisfied_entries:
        if not isinstance(entry, dict):
            continue
        identity = str(entry.get("candidate_identity") or "").strip()
        if not identity or identity in satisfied_identities:
            continue
        if identity in downloaded_identities:
            invariant_errors.append("candidate_both_downloaded_and_satisfied")
            continue
        satisfied_identities.add(identity)
        satisfied_candidates.append(entry)
    satisfied_count = len(satisfied_candidates)
    if satisfied_count != to_int(manifest.get("satisfied_count", satisfied_count), satisfied_count):
        invariant_errors.append("manifest_satisfied_count_mismatch")
    reconciled_count = downloaded_count + satisfied_count
    if reconciled_count > candidate_count:
        invariant_errors.append("reconciled_count_exceeds_candidate_count")

    missing_candidates = manifest.get("missing_candidates")
    if not isinstance(missing_candidates, list):
        missing_candidates = []
    if candidate_count - reconciled_count != len(missing_candidates):
        invariant_errors.append("missing_candidate_count_mismatch")

    archive_dirs = manifest.get("archive_dirs")
    if not isinstance(archive_dirs, list):
        archive_dirs = []
    archive_dirs = [str(value) for value in archive_dirs if str(value).strip()]

    post_checkpoint = str(state.get("last_successful_check_at") or "").strip()
    state_updated = (
        codex_rc == 0
        and bool(window_end)
        and post_checkpoint == window_end
        and str(state.get("last_run_id") or "").strip() == run_id
    )
    if codex_rc == 0 and not state_updated:
        invariant_errors.append("state_checkpoint_not_committed")

    blocked_reason = str(plan.get("blocked_reason") or "").strip()
    if blocked_reason:
        invariant_errors.append(f"scan_blocked:{blocked_reason}")
    # Preserve order while removing duplicates from independently checked layers.
    invariant_errors = list(dict.fromkeys(invariant_errors))

    scan_mode = str(plan.get("scan_mode") or machine_report.get("scan_mode") or "").strip()
    api_probe_status = str(plan.get("api_probe_status") or machine_report.get("api_probe_status") or "").strip()
    scan_alert = ""
    if scan_mode == "dom_fallback" and api_probe_status in {"failed", "unavailable"}:
        scan_alert = "api_unavailable_dom_fallback"

    if codex_rc != 0:
        no_download_reason = derive_no_download_reason(
            codex_rc=codex_rc,
            downloaded_count=0,
            candidate_reason="",
        )
        reason_code = derive_reason_code(
            codex_rc=codex_rc,
            downloaded_count=0,
            no_download_reason=no_download_reason,
            candidate_reason="",
            raw_text=raw_output_text,
        )
        status = classify_report_status(
            codex_rc=codex_rc,
            downloaded_count=0,
            no_download_reason=no_download_reason,
            core_reason=reason_code,
            scan_alert="",
        )
    elif invariant_errors:
        no_download_reason = "download_incomplete" if candidate_count else "unknown"
        reported_missing_reason = normalize_reason_code(
            str(machine_report.get("core_reason") or "").strip()
        )
        reported_no_download_reason = normalize_no_download_reason(
            str(machine_report.get("no_download_reason") or "").strip()
        )
        if reported_missing_reason not in MANIFEST_MISSING_REASON_CODES:
            reported_missing_reason = core_reason_from_no_download_reason(
                reported_no_download_reason
            )
        if missing_candidates and reported_missing_reason in MANIFEST_MISSING_REASON_CODES:
            reason_code = reported_missing_reason
            if reported_no_download_reason:
                no_download_reason = reported_no_download_reason
        else:
            reason_code = (
                "download_candidates_not_completed"
                if missing_candidates
                else "download_manifest_invariant_failed"
            )
        status = "partial"
    elif downloaded_count > 0:
        no_download_reason = "downloaded"
        reason_code = "download_completed"
        status = "success"
    elif satisfied_count > 0:
        no_download_reason = "already_archived_duplicates"
        reason_code = "window_candidates_already_archived"
        status = "success"
    else:
        window_new_docs_count = to_int(plan.get("window_new_docs_count", -1), -1)
        keyword_matched_docs_count = to_int(plan.get("keyword_matched_docs_count", -1), -1)
        if window_new_docs_count <= 0:
            no_download_reason = "no_new_documents"
            reason_code = "window_has_no_new_documents"
        elif keyword_matched_docs_count <= 0:
            no_download_reason = "no_keyword_match"
            reason_code = "window_has_updates_but_no_keyword_match"
        else:
            no_download_reason = "no_new_documents"
            reason_code = "window_has_no_new_documents"
        status = "success"

    reason_code = normalize_reason_code(reason_code)
    no_download_reason = normalize_no_download_reason(no_download_reason)
    return {
        "schema_version": 3,
        "run_id": run_id,
        "run_manifest_path": str(run_manifest_path),
        "run_started_at": run_started_at,
        "run_finished_at": run_finished_at,
        "codex_exit_code": codex_rc,
        "status": status,
        "reason_code": reason_code,
        "reason_text": REASON_TEXT_BY_CODE.get(reason_code, "原因暂未能结构化判断"),
        "no_download_reason": no_download_reason,
        "state_updated": state_updated,
        "requested_window_start": requested_window_start,
        "requested_window_end": requested_window_end,
        "reported_window_start": str(machine_report.get("window_start") or "").strip(),
        "reported_window_end": str(machine_report.get("window_end") or "").strip(),
        "window_start": window_start,
        "window_end": window_end,
        "effective_window_start": window_start or pre_last_successful_check_at,
        "effective_window_end": window_end or run_finished_at,
        "downloaded_count": downloaded_count,
        "downloaded_files": downloaded_files,
        "archive_dir": archive_dirs[-1] if archive_dirs else None,
        "archive_dirs": archive_dirs,
        "window_new_docs_count": to_int(plan.get("window_new_docs_count", -1), -1),
        "keyword_matched_docs_count": to_int(plan.get("keyword_matched_docs_count", -1), -1),
        "download_candidate_count": candidate_count,
        "download_success_count": reconciled_count,
        "satisfied_candidate_count": satisfied_count,
        "satisfied_candidates": satisfied_candidates,
        "missing_candidate_count": len(missing_candidates),
        "missing_candidates": missing_candidates,
        "manifest_invariant_errors": invariant_errors,
        "invariants_ok": not invariant_errors,
        "scan_mode": scan_mode or None,
        "api_probe_status": api_probe_status or None,
        "scan_alert": scan_alert or None,
    }


def build_canonical_result(
    *,
    state_path: Path,
    raw_output_path: Path,
    run_started_at: str,
    run_finished_at: str,
    requested_window_start: str,
    requested_window_end: str,
    pre_last_successful_check_at: str,
    codex_rc: int,
    run_manifest_path: Path | None = None,
    scan_plan_path: Path | None = None,
    run_id: str = "",
) -> dict[str, Any]:
    if run_manifest_path is not None:
        return build_manifest_canonical_result(
            state_path=state_path,
            raw_output_path=raw_output_path,
            run_manifest_path=run_manifest_path,
            scan_plan_path=scan_plan_path,
            run_id=run_id,
            run_started_at=run_started_at,
            run_finished_at=run_finished_at,
            requested_window_start=requested_window_start,
            requested_window_end=requested_window_end,
            pre_last_successful_check_at=pre_last_successful_check_at,
            codex_rc=codex_rc,
        )
    state = load_json(state_path)
    raw_output_text = load_text(raw_output_path)
    machine_report = parse_machine_report_text(raw_output_text)
    if codex_rc != 0:
        machine_report = {}
    scan_alert = parse_scan_alert_text(raw_output_text)
    if codex_rc != 0:
        scan_alert = ""
    scan_mode = str(machine_report.get("scan_mode") or "").strip()
    api_probe_status = str(machine_report.get("api_probe_status") or "").strip()
    reported_window_start = str(machine_report.get("window_start") or "").strip()
    reported_window_end = str(machine_report.get("window_end") or "").strip()
    if not scan_alert and scan_mode == "dom_fallback" and api_probe_status in {"failed", "unavailable"}:
        scan_alert = "api_unavailable_dom_fallback"

    post_last_successful_check_at = str(state.get("last_successful_check_at") or "").strip()
    state_updated = (
        codex_rc == 0
        and bool(post_last_successful_check_at)
        and post_last_successful_check_at != pre_last_successful_check_at
    )

    fallback_window_start = reported_window_start or requested_window_start or pre_last_successful_check_at
    fallback_window_end = reported_window_end or requested_window_end or run_finished_at

    if state_updated:
        downloaded_files = state.get("last_batch_files") or []
        if not isinstance(downloaded_files, list):
            downloaded_files = []
        downloaded_count = to_int(state.get("last_batch_file_count", len(downloaded_files)), len(downloaded_files))
        archive_dir = state.get("last_batch_dir")
        effective_window_start = str(state.get("last_window_start") or "").strip() or fallback_window_start
        effective_window_end = str(state.get("last_window_end") or "").strip() or fallback_window_end
    else:
        downloaded_files = []
        downloaded_count = 0
        archive_dir = None
        effective_window_start = fallback_window_start
        effective_window_end = fallback_window_end

    no_download_reason = derive_no_download_reason(
        codex_rc=codex_rc,
        downloaded_count=downloaded_count,
        candidate_reason=str(machine_report.get("no_download_reason") or "").strip(),
    )
    no_download_reason = normalize_no_download_reason(no_download_reason)
    reason_code = derive_reason_code(
        codex_rc=codex_rc,
        downloaded_count=downloaded_count,
        no_download_reason=no_download_reason,
        candidate_reason=str(machine_report.get("core_reason") or "").strip(),
        raw_text=raw_output_text,
    )
    reason_code = normalize_reason_code(reason_code)
    canonical_no_download_reason = no_download_reason_from_core_reason(reason_code)
    if canonical_no_download_reason:
        no_download_reason = canonical_no_download_reason
    status = classify_report_status(
        codex_rc=codex_rc,
        downloaded_count=downloaded_count,
        no_download_reason=no_download_reason,
        core_reason=reason_code,
        scan_alert=scan_alert,
    )
    reason_text = REASON_TEXT_BY_CODE.get(reason_code, "原因暂未能结构化判断")

    return {
        "schema_version": 2,
        "run_started_at": run_started_at,
        "run_finished_at": run_finished_at,
        "codex_exit_code": codex_rc,
        "status": status,
        "reason_code": reason_code,
        "reason_text": reason_text,
        "no_download_reason": no_download_reason,
        "state_updated": state_updated,
        "requested_window_start": requested_window_start,
        "requested_window_end": requested_window_end,
        "reported_window_start": reported_window_start,
        "reported_window_end": reported_window_end,
        "window_start": reported_window_start or fallback_window_start,
        "window_end": reported_window_end or fallback_window_end,
        "effective_window_start": effective_window_start,
        "effective_window_end": effective_window_end,
        "downloaded_count": downloaded_count,
        "downloaded_files": downloaded_files,
        "archive_dir": archive_dir,
        "window_new_docs_count": to_int(machine_report.get("window_new_docs_count", -1), -1),
        "keyword_matched_docs_count": to_int(machine_report.get("keyword_matched_docs_count", -1), -1),
        "download_candidate_count": to_int(machine_report.get("download_candidate_count", -1), -1),
        "download_success_count": to_int(machine_report.get("download_success_count", -1), -1),
        "scan_mode": scan_mode or None,
        "api_probe_status": api_probe_status or None,
        "scan_alert": scan_alert or None,
    }


def build_stub_result(
    *,
    run_id: str = "",
    run_started_at: str,
    run_finished_at: str,
    requested_window_start: str,
    requested_window_end: str,
    pre_last_successful_check_at: str,
    codex_rc: int,
    no_download_reason: str,
    reason_code: str,
    status: str,
    scan_alert: str,
    scan_mode: str,
    api_probe_status: str,
) -> dict[str, Any]:
    effective_window_start = requested_window_start or pre_last_successful_check_at
    effective_window_end = requested_window_end or run_finished_at
    final_reason = normalize_reason_code(reason_code or core_reason_from_no_download_reason(no_download_reason) or "task_failed")
    final_no_download_reason = normalize_no_download_reason(no_download_reason) or "unknown"
    return {
        "schema_version": 2,
        "run_id": run_id,
        "run_started_at": run_started_at,
        "run_finished_at": run_finished_at,
        "codex_exit_code": codex_rc,
        "status": status,
        "reason_code": final_reason,
        "reason_text": REASON_TEXT_BY_CODE.get(final_reason, "原因暂未能结构化判断"),
        "no_download_reason": final_no_download_reason,
        "state_updated": False,
        "requested_window_start": requested_window_start,
        "requested_window_end": requested_window_end,
        "reported_window_start": "",
        "reported_window_end": "",
        "window_start": effective_window_start,
        "window_end": effective_window_end,
        "effective_window_start": effective_window_start,
        "effective_window_end": effective_window_end,
        "downloaded_count": 0,
        "downloaded_files": [],
        "archive_dir": None,
        "window_new_docs_count": -1,
        "keyword_matched_docs_count": -1,
        "download_candidate_count": -1,
        "download_success_count": -1,
        "scan_mode": scan_mode or None,
        "api_probe_status": api_probe_status or None,
        "scan_alert": scan_alert or None,
    }


def ensure_current_result(
    *,
    canonical_path: Path,
    run_id: str,
    run_started_at: str,
    run_finished_at: str,
    requested_window_start: str,
    requested_window_end: str,
    pre_last_successful_check_at: str,
    process_exit_code: int,
) -> tuple[dict[str, Any], bool]:
    """Replace a stale prior-run result when a launcher exits before finalizing."""
    current = load_json(canonical_path)
    current_run_id = str(current.get("run_id") or "").strip()
    current_exit_code = to_int(current.get("codex_exit_code"), -999999)
    if run_id and current_run_id == run_id and current_exit_code == process_exit_code:
        return current, False

    effective_exit_code = process_exit_code if process_exit_code != 0 else 1
    no_download_reason = derive_no_download_reason(
        codex_rc=effective_exit_code,
        downloaded_count=0,
        candidate_reason="",
    )
    reason_code = derive_reason_code(
        codex_rc=effective_exit_code,
        downloaded_count=0,
        no_download_reason=no_download_reason,
        candidate_reason="",
        raw_text="",
    )
    status = classify_report_status(
        codex_rc=effective_exit_code,
        downloaded_count=0,
        no_download_reason=no_download_reason,
        core_reason=reason_code,
        scan_alert="",
    )
    replacement = build_stub_result(
        run_id=run_id,
        run_started_at=run_started_at,
        run_finished_at=run_finished_at,
        requested_window_start=requested_window_start,
        requested_window_end=requested_window_end,
        pre_last_successful_check_at=pre_last_successful_check_at,
        codex_rc=effective_exit_code,
        no_download_reason=no_download_reason,
        reason_code=reason_code,
        status=status,
        scan_alert="",
        scan_mode="",
        api_probe_status="",
    )
    replacement["recovered_stale_result"] = True
    replacement["reported_process_exit_code"] = process_exit_code
    replacement["replaced_run_id"] = current_run_id or None
    write_json(canonical_path, replacement)
    return replacement, True


def format_display_time(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "未知时间"
    candidates = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    ]
    for fmt in candidates:
        try:
            dt = datetime.strptime(text, fmt)
            return dt.strftime("%m-%d %H:%M")
        except Exception:
            continue
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return text


def render_summary_lines(result: dict[str, Any], *, execute_time: str) -> list[str]:
    status = str(result.get("status") or "failed").strip() or "failed"
    reason_text = str(result.get("reason_text") or "原因暂未能结构化判断").strip()
    downloaded_files = result.get("downloaded_files") if isinstance(result.get("downloaded_files"), list) else []
    downloaded_count = to_int(result.get("downloaded_count", len(downloaded_files)), len(downloaded_files))
    no_download_reason = normalize_no_download_reason(str(result.get("no_download_reason") or "unknown").strip() or "unknown")
    reason_code = normalize_reason_code(str(result.get("reason_code") or "").strip())
    scan_alert = str(result.get("scan_alert") or "").strip()

    display_time = format_display_time(execute_time)
    if status == "busy":
        window_range_display = "未执行 - 未执行"
    else:
        window_start_display = format_display_time(str(result.get("effective_window_start") or ""))
        window_end_display = format_display_time(str(result.get("effective_window_end") or ""))
        window_range_display = f"{window_start_display} - {window_end_display}"

    lines: list[str] = []
    if status == "success" and downloaded_count > 0:
        lines.append("知识星球研报自动下载：✅ 成功")
        lines.append(f"执行时间：{display_time}")
        lines.append(f"检查区间：{window_range_display}")
        lines.append(f"结果：发现并下载 {downloaded_count} 篇新文档")
        preview_files = downloaded_files[:8]
        if preview_files:
            lines.append("文件：")
            lines.extend([f"- {name}" for name in preview_files])
            remaining = max(len(downloaded_files) - len(preview_files), 0)
            if remaining:
                lines.append(f"- 另有 {remaining} 篇，已省略")
    elif status == "success":
        lines.append("知识星球研报自动下载：✅ 成功")
        lines.append(f"执行时间：{display_time}")
        lines.append(f"检查区间：{window_range_display}")
        if no_download_reason == "no_new_documents":
            lines.append("结果：这段时间没有新文档")
        elif no_download_reason == "no_keyword_match":
            lines.append("结果：有新内容，但没有命中你的关键词")
        elif no_download_reason == "no_window_updates":
            lines.append("结果：这段时间没有新更新")
        elif no_download_reason == "already_archived_duplicates":
            lines.append("结果：候选内容已在资料库中，无需重复下载")
        elif no_download_reason == "download_incomplete":
            lines.append("结果：发现了候选文档，但下载没完成")
        else:
            lines.append("结果：本轮没有下载到文档")
    elif status == "partial":
        lines.append("知识星球研报自动下载：⚠️ 部分完成")
        lines.append(f"执行时间：{display_time}")
        lines.append(f"检查区间：{window_range_display}")
        lines.append("结果：只完成了部分下载")
        lines.append(f"原因：{reason_text}")
    elif status == "busy":
        lines.append("知识星球研报自动下载：⏳ 未执行")
        lines.append(f"执行时间：{display_time}")
        lines.append(f"检查区间：{window_range_display}")
        lines.append(f"原因：{reason_text}")
    else:
        lines.append("知识星球研报自动下载：❌ 失败")
        lines.append(f"执行时间：{display_time}")
        lines.append(f"检查区间：{window_range_display}")
        lines.append(f"失败原因：{reason_text}")
        if reason_code == "blocked_documents_permission":
            lines.append("补充说明：后台进程缺少 Documents 目录访问权限")
        elif reason_code == "task_interrupted_sigterm":
            lines.append("补充说明：先检查任务平台的超时设置，以及是否有人手动停止了任务")
        elif reason_code == "self_terminated_codex_runner":
            lines.append("补充说明：本次日志里出现了对 codex 进程的排查，任务很像是把自己的主进程杀掉了")

    if scan_alert == "api_unavailable_dom_fallback":
        lines.append("补充说明：知识星球接口异常，本次改用页面扫描完成检查")

    return lines


def render_feishu_message(canonical_result: dict[str, Any]) -> str:
    execute_time = str(canonical_result.get("run_finished_at") or "").strip()
    return "\n".join(render_summary_lines(canonical_result, execute_time=execute_time))


def render_last_result(
    *,
    canonical_result: dict[str, Any],
    window_mode: str,
    explicit_window_start: str,
    explicit_window_end: str,
    window_note: str,
    log_path: str,
    result_md_path: str,
    canonical_result_path: str,
) -> dict[str, Any]:
    execute_time = str(canonical_result.get("run_finished_at") or "").strip()
    return {
        "run_id": canonical_result.get("run_id"),
        "run_started_at": canonical_result.get("run_started_at"),
        "run_finished_at": canonical_result.get("run_finished_at"),
        "execute_time": execute_time,
        "status": canonical_result.get("status"),
        "exit_code": canonical_result.get("codex_exit_code"),
        "window_mode": window_mode,
        "window_start": canonical_result.get("effective_window_start"),
        "window_end": canonical_result.get("effective_window_end"),
        "requested_window_start": canonical_result.get("requested_window_start"),
        "requested_window_end": canonical_result.get("requested_window_end"),
        "explicit_window_start": explicit_window_start,
        "explicit_window_end": explicit_window_end,
        "window_note": window_note,
        "downloaded_count": canonical_result.get("downloaded_count"),
        "downloaded_files": canonical_result.get("downloaded_files"),
        "archive_dir": canonical_result.get("archive_dir"),
        "archive_dirs": canonical_result.get("archive_dirs") or ([canonical_result.get("archive_dir")] if canonical_result.get("archive_dir") else []),
        "no_download_reason": canonical_result.get("no_download_reason"),
        "core_reason_code": canonical_result.get("reason_code"),
        "core_reason_text": canonical_result.get("reason_text"),
        "window_new_docs_count": canonical_result.get("window_new_docs_count"),
        "keyword_matched_docs_count": canonical_result.get("keyword_matched_docs_count"),
        "download_candidate_count": canonical_result.get("download_candidate_count"),
        "download_success_count": canonical_result.get("download_success_count"),
        "satisfied_candidate_count": canonical_result.get("satisfied_candidate_count", 0),
        "satisfied_candidates": canonical_result.get("satisfied_candidates") or [],
        "missing_candidate_count": canonical_result.get("missing_candidate_count", 0),
        "missing_candidates": canonical_result.get("missing_candidates") or [],
        "scan_mode": canonical_result.get("scan_mode"),
        "api_probe_status": canonical_result.get("api_probe_status"),
        "scan_alert": canonical_result.get("scan_alert"),
        "log_path": log_path,
        "result_md_path": result_md_path,
        "canonical_result_path": canonical_result_path,
        "recovered_stale_result": bool(canonical_result.get("recovered_stale_result", False)),
    }


def build_last_result(
    *,
    canonical_result: dict[str, Any],
    window_mode: str,
    explicit_window_start: str,
    explicit_window_end: str,
    window_note: str,
    log_path: str,
    result_md_path: str,
    canonical_result_path: str,
) -> tuple[dict[str, Any], str]:
    payload = render_last_result(
        canonical_result=canonical_result,
        window_mode=window_mode,
        explicit_window_start=explicit_window_start,
        explicit_window_end=explicit_window_end,
        window_note=window_note,
        log_path=log_path,
        result_md_path=result_md_path,
        canonical_result_path=canonical_result_path,
    )
    return payload, render_feishu_message(canonical_result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build or render ZSXQ autodownload results.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--state", required=True)
    build_parser.add_argument("--raw-output", required=True)
    build_parser.add_argument("--output", required=True)
    build_parser.add_argument("--run-started-at", required=True)
    build_parser.add_argument("--run-finished-at", required=True)
    build_parser.add_argument("--requested-window-start", default="")
    build_parser.add_argument("--requested-window-end", default="")
    build_parser.add_argument("--pre-last-successful-check-at", default="")
    build_parser.add_argument("--codex-rc", required=True, type=int)
    build_parser.add_argument("--run-id", default="")
    build_parser.add_argument("--run-manifest", default="")
    build_parser.add_argument("--scan-plan", default="")

    stub_parser = subparsers.add_parser("stub")
    stub_parser.add_argument("--output", required=True)
    stub_parser.add_argument("--run-id", default="")
    stub_parser.add_argument("--run-started-at", required=True)
    stub_parser.add_argument("--run-finished-at", required=True)
    stub_parser.add_argument("--requested-window-start", default="")
    stub_parser.add_argument("--requested-window-end", default="")
    stub_parser.add_argument("--pre-last-successful-check-at", default="")
    stub_parser.add_argument("--codex-rc", required=True, type=int)
    stub_parser.add_argument("--no-download-reason", default="unknown")
    stub_parser.add_argument("--reason-code", default="task_failed")
    stub_parser.add_argument("--status", default="failed")
    stub_parser.add_argument("--scan-alert", default="")
    stub_parser.add_argument("--scan-mode", default="")
    stub_parser.add_argument("--api-probe-status", default="")

    render_parser = subparsers.add_parser("render")
    render_parser.add_argument("--canonical", required=True)
    render_parser.add_argument("--output", required=True)
    render_parser.add_argument("--window-mode", required=True)
    render_parser.add_argument("--explicit-window-start", default="")
    render_parser.add_argument("--explicit-window-end", default="")
    render_parser.add_argument("--window-note", default="")
    render_parser.add_argument("--log-path", required=True)
    render_parser.add_argument("--result-md-path", required=True)

    ensure_parser = subparsers.add_parser("ensure-current")
    ensure_parser.add_argument("--canonical", required=True)
    ensure_parser.add_argument("--run-id", required=True)
    ensure_parser.add_argument("--run-started-at", required=True)
    ensure_parser.add_argument("--run-finished-at", required=True)
    ensure_parser.add_argument("--requested-window-start", default="")
    ensure_parser.add_argument("--requested-window-end", default="")
    ensure_parser.add_argument("--pre-last-successful-check-at", default="")
    ensure_parser.add_argument("--process-exit-code", required=True, type=int)

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.command == "build":
        payload = build_canonical_result(
            state_path=Path(args.state),
            raw_output_path=Path(args.raw_output),
            run_started_at=args.run_started_at,
            run_finished_at=args.run_finished_at,
            requested_window_start=args.requested_window_start,
            requested_window_end=args.requested_window_end,
            pre_last_successful_check_at=args.pre_last_successful_check_at,
            codex_rc=args.codex_rc,
            run_manifest_path=Path(args.run_manifest) if args.run_manifest else None,
            scan_plan_path=Path(args.scan_plan) if args.scan_plan else None,
            run_id=args.run_id,
        )
        write_json(Path(args.output), payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "stub":
        payload = build_stub_result(
            run_id=args.run_id,
            run_started_at=args.run_started_at,
            run_finished_at=args.run_finished_at,
            requested_window_start=args.requested_window_start,
            requested_window_end=args.requested_window_end,
            pre_last_successful_check_at=args.pre_last_successful_check_at,
            codex_rc=args.codex_rc,
            no_download_reason=args.no_download_reason,
            reason_code=args.reason_code,
            status=args.status,
            scan_alert=args.scan_alert,
            scan_mode=args.scan_mode,
            api_probe_status=args.api_probe_status,
        )
        write_json(Path(args.output), payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "ensure-current":
        payload, replaced = ensure_current_result(
            canonical_path=Path(args.canonical),
            run_id=args.run_id,
            run_started_at=args.run_started_at,
            run_finished_at=args.run_finished_at,
            requested_window_start=args.requested_window_start,
            requested_window_end=args.requested_window_end,
            pre_last_successful_check_at=args.pre_last_successful_check_at,
            process_exit_code=args.process_exit_code,
        )
        print(
            json.dumps(
                {"replaced": replaced, "result": payload},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    canonical_path = Path(args.canonical)
    canonical_result = load_json(canonical_path)
    if not canonical_result:
        raise SystemExit(f"Canonical result is missing or invalid: {canonical_path}")

    last_result, summary = build_last_result(
        canonical_result=canonical_result,
        window_mode=args.window_mode,
        explicit_window_start=args.explicit_window_start,
        explicit_window_end=args.explicit_window_end,
        window_note=args.window_note,
        log_path=args.log_path,
        result_md_path=args.result_md_path,
        canonical_result_path=str(canonical_path),
    )
    write_json(Path(args.output), last_result)
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
