#!/usr/bin/env python3
"""Low-noise Feishu notification policy for ZSXQ download pipelines.

The download job result remains the source of truth.  This module only decides
whether that result deserves a user-facing message and delivers it through a
small persistent outbox, so a notification failure never re-runs a scan or
download.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


PIPELINE_LABELS = {
    "foreign_download": "知识星球研报",
    "domestic_cicc": "中金研报",
}
PIPELINE_KEY_LABELS = {
    "foreign_download": "foreign",
    "domestic_cicc": "cicc",
}
INCIDENT_REMINDER_HOURS = 4
TRANSIENT_ALERT_COUNT = 2
TRANSIENT_ALERT_MINUTES = 60
OUTBOX_RETRY_MINUTES = (5, 10, 20)

FAILURE_GUIDANCE_BY_REASON = {
    "blocked_browser_missing": (
        "没有找到可执行的 Chrome for Testing。",
        "专用浏览器被移动、删除，或安装路径发生变化。",
        "请恢复 Chrome for Testing；下载游标未推进，恢复后可以安全重跑。",
    ),
    "blocked_browser_endpoint_unavailable": (
        "专用浏览器没有提供可用的 9223 调试端口。",
        "Chrome for Testing 没有成功启动、启动后退出，或调试端口没有监听。",
        "系统会在后续窗口再次尝试启动；若连续失败，请检查专用浏览器进程和 9223 端口。",
    ),
    "blocked_browser_cdp_unresponsive": (
        "浏览器进程和 9223 端口仍在，但 Playwright 接管浏览器超时。",
        "专用浏览器会话可能卡死，或 DevTools/CDP 通道处于假在线状态。",
        "系统会在后续窗口重试；若连续失败，请重启专用 Chrome for Testing。",
    ),
    "blocked_browser_unavailable_or_interrupted": (
        "Chrome 会话在下载前检查阶段异常中断。",
        "浏览器进程、调试连接或页面会话可能短暂失效。",
        "系统会在后续窗口重试；若连续失败，请检查专用 Chrome for Testing。",
    ),
    "need_reauth": (
        "浏览器可以使用，但知识星球页面要求重新登录。",
        "知识星球登录态已过期、账号被退出，或专用 profile 的登录信息失效。",
        "请重新登录知识星球（使用专用 Chrome for Testing）；下载游标未推进，登录后可以安全重跑。",
    ),
    "zsxq_page_unavailable": (
        "浏览器可以连接，但知识星球页面加载失败。",
        "可能是当前网络、DNS/代理、知识星球站点服务或页面请求异常。",
        "系统会在后续窗口重试；若连续失败，请先确认浏览器能正常打开知识星球。",
    ),
    "zsxq_page_state_unrecognized": (
        "知识星球页面已打开，但没有识别到星球内容或登录提示。",
        "页面可能卡在加载、续费/风控弹窗，或知识星球页面结构发生变化。",
        "请查看专用浏览器当前页面；下载游标未推进，页面恢复后可以安全重跑。",
    ),
    "source_content_protected": (
        "知识星球页面已打开，但星主开启了内容保护，网页端不提供这些文件下载。",
        "这是源站权限策略，不是登录、网络或 Playwright 点击故障。",
        "系统不会绕过内容保护；如需该文件，请在有权限的知识星球 App 内查看或由来源方提供可下载版本。",
    ),
    "blocked_documents_permission": (
        "后台任务无法读取下载和归档所需的 Documents 目录。",
        "cron 或后台进程可能没有 Full Disk Access。",
        "请补齐后台进程的目录权限；下载游标未推进，修复后可以安全重跑。",
    ),
    "cloud_requirements_timeout": (
        "Codex 云端依赖检查超时，任务尚未进入知识星球扫描。",
        "可能是 Codex 服务连接或当前网络链路短暂异常。",
        "系统会在后续窗口重试；若连续失败，请检查 Codex 网络连接。",
    ),
    "codex_exec_timeout": (
        "Codex 执行超过硬超时，系统已终止本轮完整进程组并释放共享锁。",
        "运行层、模型目录刷新或浏览器工具调用长时间没有返回。",
        "下载游标未推进，后续窗口会安全重试；本轮不会继续阻塞其他下载和摘要任务。",
    ),
    "busy_locked": (
        "共享下载锁持续被上一轮任务占用，本轮触发被跳过。",
        "上一轮可能仍在正常下载，也可能已经失去进展。",
        "系统会对超时进程执行整组终止，并自动回收已失去所有者的锁；若仍重复出现，请检查运行状态。",
    ),
}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def now_local() -> datetime:
    return datetime.now().astimezone()


def parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.astimezone()
    except ValueError:
        return None


def display_time(value: Any) -> str:
    parsed = parse_datetime(value)
    if parsed is not None:
        return parsed.astimezone().strftime("%m-%d %H:%M")
    text = str(value or "").strip()
    return text or "未知"


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def result_reason(result: dict[str, Any]) -> tuple[str, str]:
    code = str(
        result.get("core_reason_code")
        or result.get("reason_code")
        or result.get("no_download_reason")
        or "unknown"
    ).strip()
    text = str(
        result.get("core_reason_text")
        or result.get("reason_text")
        or "原因暂未能结构化判断"
    ).strip()
    return code, text


def normalized_files(result: dict[str, Any]) -> list[str]:
    raw = result.get("downloaded_files")
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def event_key(pipeline: str, event: str, seed: Any) -> str:
    raw = json.dumps(seed, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"zsxq-{PIPELINE_KEY_LABELS[pipeline]}-{event}-{digest}"


def incident_signature(result: dict[str, Any]) -> str:
    code, _ = result_reason(result)
    seed = {
        "status": str(result.get("status") or "failed"),
        "reason": code,
        "exit_code": result.get("exit_code", result.get("codex_exit_code")),
        "scan_alert": result.get("scan_alert"),
        "candidate_count": result.get("download_candidate_count"),
        "success_count": result.get("download_success_count", result.get("downloaded_count")),
    }
    return hashlib.sha256(json.dumps(seed, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:24]


def incident_occurrence(result: dict[str, Any], now: datetime) -> str:
    """Stable identity for one run, distinct from later repeats of the same fault."""
    for field in (
        "run_id",
        "run_started_at",
        "run_finished_at",
        "execute_time",
        "effective_window_end",
        "window_end",
    ):
        value = str(result.get(field) or "").strip()
        if value:
            return f"{field}:{value}"
    return f"observed_at:{now.isoformat()}"


def window_line(result: dict[str, Any]) -> str:
    start = result.get("window_start") or result.get("effective_window_start")
    end = result.get("window_end") or result.get("effective_window_end")
    return f"{display_time(start)} → {display_time(end)}"


def render_completion(result: dict[str, Any], pipeline: str) -> str:
    label = PIPELINE_LABELS[pipeline]
    files = normalized_files(result)
    count = to_int(result.get("downloaded_count"), len(files))
    lines = [
        f"## ✅ {label}｜本轮新增 {count} 篇",
        f"**检查区间**：{window_line(result)}",
    ]
    if files:
        lines.append("**文件**：")
        lines.extend(f"- {name}" for name in files[:6])
        if len(files) > 6:
            lines.append(f"- 另有 {len(files) - 6} 篇已省略")
    return "\n".join(lines)


def failure_guidance(reason_code: str, reason_text: str) -> tuple[str, str, str]:
    normalized = str(reason_code or "").strip().casefold()
    configured = FAILURE_GUIDANCE_BY_REASON.get(normalized)
    if configured is not None:
        return configured
    return (
        reason_text or "任务已经启动，但当前证据不足以确认单一原因。",
        "可能是任务进程、网络、页面或下载对账阶段的临时异常。",
        "下载游标未推进，系统会在后续窗口重试；若连续失败，再结合下一次诊断结果处理。",
    )


def render_alert(result: dict[str, Any], pipeline: str) -> str:
    label = PIPELINE_LABELS[pipeline]
    status = str(result.get("status") or "failed").strip()
    reason_code, reason_text = result_reason(result)
    candidate_count = to_int(result.get("download_candidate_count"), -1)
    success_count = to_int(result.get("download_success_count", result.get("downloaded_count")), -1)
    progress_label = "部分完成" if status == "partial" and success_count > 0 else "未完成"
    diagnosis, possible_cause, recommended_action = failure_guidance(reason_code, reason_text)
    lines = [
        f"## ⚠️ {label}｜下载异常",
        f"**检查区间**：{window_line(result)}",
        f"**状态**：{progress_label}",
        f"**定位结果**：{diagnosis}",
        f"**可能原因**：{possible_cause}",
    ]
    if candidate_count >= 0 or success_count >= 0:
        candidate_display = "未知" if candidate_count < 0 else str(candidate_count)
        success_display = "未知" if success_count < 0 else str(success_count)
        lines.append(f"**进度**：候选 {candidate_display}｜已下载 {success_display}")
    lines.append(f"**建议处理**：{recommended_action}")
    return "\n".join(lines)


def render_recovery(result: dict[str, Any], pipeline: str) -> str:
    label = PIPELINE_LABELS[pipeline]
    count = to_int(result.get("downloaded_count"), len(normalized_files(result)))
    detail = f"并下载 {count} 篇" if count > 0 else "本轮检查正常"
    return "\n".join(
        [
            f"## ✅ {label}｜链路已恢复",
            f"**检查区间**：{window_line(result)}",
            f"**结果**：历史异常已解除，{detail}。",
        ]
    )


def render_transient_alert(
    result: dict[str, Any],
    pipeline: str,
    consecutive_count: int,
    first_seen_at: datetime | None,
) -> str:
    label = PIPELINE_LABELS[pipeline]
    reason_code, reason_text = result_reason(result)
    diagnosis, possible_cause, recommended_action = failure_guidance(reason_code, reason_text)
    status = str(result.get("status") or "waiting").strip().casefold()
    status_label = {"busy": "任务占用", "waiting": "持续等待", "paused": "重试暂停"}.get(status, status)
    lines = [
        f"## ⚠️ {label}｜持续阻塞",
        f"**检查区间**：{window_line(result)}",
        f"**状态**：{status_label}，已连续出现 {consecutive_count} 次",
        f"**定位结果**：{diagnosis}",
        f"**可能原因**：{possible_cause}",
    ]
    if first_seen_at is not None:
        lines.append(f"**首次出现**：{first_seen_at.astimezone().strftime('%m-%d %H:%M')}")
    lines.append(f"**建议处理**：{recommended_action}")
    return "\n".join(lines)


@dataclass(frozen=True)
class PlannedEvent:
    event: str
    key: str
    message: str
    severity: str


def default_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "incident": {"active": False},
        "transient_state": {},
        "sent_event_keys": [],
    }


def default_outbox() -> dict[str, Any]:
    return {"schema_version": 1, "entries": []}


def decide(
    result: dict[str, Any],
    pipeline: str,
    state: dict[str, Any],
    now: datetime,
) -> tuple[PlannedEvent | None, str]:
    status = str(result.get("status") or "failed").strip().casefold()
    downloaded_count = to_int(result.get("downloaded_count"), len(normalized_files(result)))
    incident = state.get("incident") if isinstance(state.get("incident"), dict) else {"active": False}

    if status in {"busy", "waiting", "paused"}:
        signature = incident_signature(result)
        transient = state.get("transient_state") if isinstance(state.get("transient_state"), dict) else {}
        same_transient = transient.get("signature") == signature
        consecutive_count = to_int(transient.get("consecutive_count"), 0) + 1 if same_transient else 1
        first_seen_at = parse_datetime(transient.get("first_seen_at")) if same_transient else now
        elapsed = now - first_seen_at.astimezone(now.tzinfo) if first_seen_at is not None else timedelta(0)
        if consecutive_count < TRANSIENT_ALERT_COUNT and elapsed < timedelta(minutes=TRANSIENT_ALERT_MINUTES):
            return None, "routine_transient_state"

        same_incident = bool(incident.get("active")) and incident.get("signature") == signature
        last_alert_at = parse_datetime(incident.get("last_alert_at"))
        if same_incident and last_alert_at is not None:
            if now - last_alert_at.astimezone(now.tzinfo) < timedelta(hours=INCIDENT_REMINDER_HOURS):
                return None, "incident_already_alerted"
        key_seed: dict[str, Any] = {
            "signature": signature,
            "transient": True,
            "occurrence": str(transient.get("first_seen_at") or incident_occurrence(result, now)),
        }
        if same_incident:
            key_seed["reminder_bucket"] = int(now.timestamp()) // (INCIDENT_REMINDER_HOURS * 3600)
        key = event_key(pipeline, "alert", key_seed)
        return (
            PlannedEvent(
                "alert",
                key,
                render_transient_alert(result, pipeline, consecutive_count, first_seen_at),
                "warning",
            ),
            "persistent_transient_state",
        )

    if status == "success":
        sent_keys = state.get("sent_event_keys") if isinstance(state.get("sent_event_keys"), list) else []
        last_alert_key = str(incident.get("last_alert_key") or "").strip()
        if bool(incident.get("active")) and last_alert_key and last_alert_key in sent_keys:
            previous_signature = str(incident.get("signature") or "unknown")
            key = event_key(
                pipeline,
                "recovery",
                {
                    "incident": previous_signature,
                    "alert_key": last_alert_key,
                    "window_end": result.get("window_end"),
                },
            )
            return PlannedEvent("recovery", key, render_recovery(result, pipeline), "recovery"), "incident_recovered"
        if pipeline == "domestic_cicc" and downloaded_count > 0:
            key = event_key(
                pipeline,
                "completion",
                {"window_end": result.get("window_end"), "files": normalized_files(result)},
            )
            return PlannedEvent("completion", key, render_completion(result, pipeline), "success"), "new_domestic_reports"
        return None, "routine_success_silent"

    signature = incident_signature(result)
    same_incident = bool(incident.get("active")) and incident.get("signature") == signature
    last_alert_at = parse_datetime(incident.get("last_alert_at"))
    if same_incident and last_alert_at is not None:
        if now - last_alert_at.astimezone(now.tzinfo) < timedelta(hours=INCIDENT_REMINDER_HOURS):
            return None, "incident_already_alerted"

    key_seed = {
        "signature": signature,
        "occurrence": incident_occurrence(result, now),
    }
    if same_incident:
        key_seed["reminder_bucket"] = int(now.timestamp()) // (INCIDENT_REMINDER_HOURS * 3600)
    key = event_key(pipeline, "alert", key_seed)
    return PlannedEvent("alert", key, render_alert(result, pipeline), "warning"), "actionable_failure"


def update_incident_state(
    result: dict[str, Any],
    state: dict[str, Any],
    planned: PlannedEvent | None,
    now: datetime,
) -> None:
    status = str(result.get("status") or "failed").strip().casefold()
    current = state.get("incident") if isinstance(state.get("incident"), dict) else {"active": False}
    now_text = now.isoformat()
    if status == "success":
        if current.get("active"):
            state["last_recovered_incident"] = {**current, "recovered_at": now_text}
        state["incident"] = {"active": False, "recovered_at": now_text}
        return
    if status in {"busy", "waiting", "paused"} and not (
        planned is not None and planned.event == "alert"
    ):
        return

    signature = incident_signature(result)
    _, reason_text = result_reason(result)
    if current.get("active") and current.get("signature") == signature:
        current["last_seen_at"] = now_text
    else:
        current = {
            "active": True,
            "signature": signature,
            "first_seen_at": now_text,
            "last_seen_at": now_text,
            "reason": reason_text,
        }
    if planned is not None and planned.event == "alert":
        current["last_alert_at"] = now_text
        current["last_alert_key"] = planned.key
    state["incident"] = current


def update_transient_state(
    result: dict[str, Any],
    state: dict[str, Any],
    planned: PlannedEvent | None,
    now: datetime,
) -> None:
    status = str(result.get("status") or "failed").strip().casefold()
    if status not in {"busy", "waiting", "paused"}:
        state["transient_state"] = {}
        return

    signature = incident_signature(result)
    current = state.get("transient_state") if isinstance(state.get("transient_state"), dict) else {}
    if current.get("signature") == signature:
        current["consecutive_count"] = to_int(current.get("consecutive_count"), 0) + 1
        current["last_seen_at"] = now.isoformat()
    else:
        current = {
            "signature": signature,
            "status": status,
            "reason": result_reason(result)[0],
            "consecutive_count": 1,
            "first_seen_at": now.isoformat(),
            "last_seen_at": now.isoformat(),
        }
    if planned is not None and planned.event == "alert":
        current["last_alert_at"] = now.isoformat()
        current["last_alert_key"] = planned.key
    state["transient_state"] = current


def redact_error(text: str) -> str:
    value = re.sub(r"cli_[A-Za-z0-9_-]+", "cli_***", str(text or ""))
    value = re.sub(r"(appSecret[\"'=:\s]+)[^\"',\s}]+", r"\1***", value, flags=re.IGNORECASE)
    return value[-1200:]


def extract_message_id(payload: Any) -> str:
    if isinstance(payload, dict):
        value = str(payload.get("message_id") or "").strip()
        if value:
            return value
        for child in payload.values():
            found = extract_message_id(child)
            if found:
                return found
    elif isinstance(payload, list):
        for child in payload:
            found = extract_message_id(child)
            if found:
                return found
    return ""


def send_lark_message(
    *,
    lark_cli: str,
    chat_id: str,
    message: str,
    idempotency_key: str,
    attempts: int,
) -> tuple[bool, str, str]:
    command = [
        lark_cli,
        "im",
        "+messages-send",
        "--chat-id",
        chat_id,
        "--idempotency-key",
        idempotency_key,
        "--as",
        "bot",
        "--markdown",
        message,
        "--json",
    ]
    last_error = ""
    for attempt in range(max(attempts, 1)):
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            last_error = redact_error(str(exc))
        else:
            if completed.returncode == 0:
                try:
                    payload = json.loads(completed.stdout or "{}")
                except json.JSONDecodeError:
                    payload = {}
                return True, extract_message_id(payload), ""
            last_error = redact_error("\n".join(part for part in (completed.stdout, completed.stderr) if part))
        if attempt + 1 < attempts:
            time.sleep((2, 5, 10)[min(attempt, 2)])
    return False, "", last_error or "lark-cli send failed"


def enqueue(outbox: dict[str, Any], planned: PlannedEvent, now: datetime) -> bool:
    entries = outbox.setdefault("entries", [])
    if not isinstance(entries, list):
        entries = []
        outbox["entries"] = entries
    if any(isinstance(item, dict) and item.get("idempotency_key") == planned.key for item in entries):
        return False
    entries.append(
        {
            "event": planned.event,
            "severity": planned.severity,
            "idempotency_key": planned.key,
            "message": planned.message,
            "status": "pending",
            "attempt_count": 0,
            "created_at": now.isoformat(),
            "next_attempt_at": now.isoformat(),
        }
    )
    return True


def cancel_undelivered_alerts(outbox: dict[str, Any], now: datetime) -> int:
    """Do not deliver a stale outage after a newer successful run recovered."""
    cancelled = 0
    entries = outbox.get("entries") if isinstance(outbox.get("entries"), list) else []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("event") != "alert" or entry.get("status") != "pending":
            continue
        entry.update(
            {
                "status": "cancelled",
                "cancelled_at": now.isoformat(),
                "cancel_reason": "recovered_before_delivery",
            }
        )
        cancelled += 1
    return cancelled


def cancel_pending_recoveries(outbox: dict[str, Any], now: datetime) -> int:
    """A newer failure invalidates an undelivered recovery transition."""
    cancelled = 0
    entries = outbox.get("entries") if isinstance(outbox.get("entries"), list) else []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("event") != "recovery" or entry.get("status") != "pending":
            continue
        entry.update(
            {
                "status": "cancelled",
                "cancelled_at": now.isoformat(),
                "cancel_reason": "new_failure_before_delivery",
            }
        )
        cancelled += 1
    return cancelled


def cancel_superseded_pending_transitions(
    outbox: dict[str, Any],
    planned: PlannedEvent,
    now: datetime,
) -> int:
    """Keep only the newest undelivered alert/recovery conclusion for one pipeline."""
    cancelled = 0
    entries = outbox.get("entries") if isinstance(outbox.get("entries"), list) else []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") != "pending":
            continue
        if entry.get("event") not in {"alert", "recovery"}:
            continue
        if str(entry.get("idempotency_key") or "") == planned.key:
            continue
        entry.update(
            {
                "status": "cancelled",
                "cancelled_at": now.isoformat(),
                "cancel_reason": f"superseded_by_{planned.event}",
                "superseded_by": planned.key,
            }
        )
        cancelled += 1
    return cancelled


def normalize_pending_keys(outbox: dict[str, Any], state: dict[str, Any], pipeline: str) -> int:
    """Migrate legacy keys that exceed Feishu's field limit."""
    migrated = 0
    entries = outbox.get("entries") if isinstance(outbox.get("entries"), list) else []
    incident = state.get("incident") if isinstance(state.get("incident"), dict) else {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") != "pending":
            continue
        old_key = str(entry.get("idempotency_key") or "")
        if len(old_key) <= 50:
            continue
        new_key = event_key(
            pipeline,
            str(entry.get("event") or "message"),
            {"legacy_key": old_key, "message": entry.get("message")},
        )
        entry["idempotency_key"] = new_key
        entry["migrated_from_key"] = old_key
        if incident.get("last_alert_key") == old_key:
            incident["last_alert_key"] = new_key
        migrated += 1
    if incident:
        state["incident"] = incident
    return migrated


def flush_outbox(
    *,
    outbox: dict[str, Any],
    state: dict[str, Any],
    audit_path: Path,
    lark_cli: str,
    chat_id: str,
    now: datetime,
    no_send: bool,
    send_attempts: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    sent_keys = state.setdefault("sent_event_keys", [])
    if not isinstance(sent_keys, list):
        sent_keys = []
        state["sent_event_keys"] = sent_keys
    entries = outbox.get("entries") if isinstance(outbox.get("entries"), list) else []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") in {"sent", "cancelled", "dead_letter"}:
            continue
        due_at = parse_datetime(entry.get("next_attempt_at"))
        if due_at is not None and due_at.astimezone(now.tzinfo) > now:
            continue
        if entry.get("idempotency_key") in sent_keys:
            entry["status"] = "sent"
            entry["sent_at"] = now.isoformat()
            continue
        if no_send:
            results.append({"event": entry.get("event"), "status": "planned"})
            continue
        ok, message_id, error = send_lark_message(
            lark_cli=lark_cli,
            chat_id=chat_id,
            message=str(entry.get("message") or ""),
            idempotency_key=str(entry.get("idempotency_key") or ""),
            attempts=send_attempts,
        )
        entry["attempt_count"] = to_int(entry.get("attempt_count")) + 1
        audit = {
            "attempted_at": now.isoformat(),
            "event": entry.get("event"),
            "idempotency_key": entry.get("idempotency_key"),
            "status": "success" if ok else "failed",
            "message_id": message_id or None,
            "error": error or None,
        }
        append_jsonl(audit_path, audit)
        if ok:
            entry.update({"status": "sent", "sent_at": audit["attempted_at"], "message_id": message_id or None, "last_error": None})
            sent_keys.append(str(entry.get("idempotency_key") or ""))
            state["sent_event_keys"] = sent_keys[-200:]
        else:
            if entry["attempt_count"] > len(OUTBOX_RETRY_MINUTES):
                entry.update(
                    {
                        "status": "dead_letter",
                        "last_error": error,
                        "next_attempt_at": None,
                        "dead_lettered_at": now.isoformat(),
                    }
                )
            else:
                delay = OUTBOX_RETRY_MINUTES[entry["attempt_count"] - 1]
                entry.update(
                    {
                        "status": "pending",
                        "last_error": error,
                        "next_attempt_at": (now + timedelta(minutes=delay)).isoformat(),
                    }
                )
        results.append({"event": entry.get("event"), "status": audit["status"], "message_id": message_id or None})
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Apply low-noise Feishu policy to a ZSXQ download result.")
    parser.add_argument("--result", required=True)
    parser.add_argument("--pipeline", required=True, choices=sorted(PIPELINE_LABELS))
    parser.add_argument("--state", required=True)
    parser.add_argument("--outbox", required=True)
    parser.add_argument("--audit", required=True)
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--lark-cli", default="lark-cli")
    parser.add_argument(
        "--send-attempts",
        type=int,
        default=1,
        help="Inline attempts for one invocation; default 1 so persistent outbox backoff handles retries.",
    )
    parser.add_argument("--no-send", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result_path = Path(args.result)
    state_path = Path(args.state)
    outbox_path = Path(args.outbox)
    audit_path = Path(args.audit)
    result = load_json(result_path, {})
    if not isinstance(result, dict) or not result:
        raise SystemExit(f"invalid result: {result_path}")

    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        state = load_json(state_path, default_state())
        outbox = load_json(outbox_path, default_outbox())
        if not isinstance(state, dict):
            state = default_state()
        if not isinstance(outbox, dict):
            outbox = default_outbox()

        now = now_local()
        normalize_pending_keys(outbox, state, args.pipeline)
        current_status = str(result.get("status") or "").strip().casefold()
        if current_status == "success":
            cancel_undelivered_alerts(outbox, now)
        elif current_status not in {"busy", "waiting", "paused"}:
            cancel_pending_recoveries(outbox, now)
        planned, decision_reason = decide(result, args.pipeline, state, now)
        if planned is not None and planned.event in {"alert", "recovery"}:
            cancel_superseded_pending_transitions(outbox, planned, now)
        enqueued = False
        sent_keys = state.get("sent_event_keys") if isinstance(state.get("sent_event_keys"), list) else []
        if planned is not None and planned.key not in sent_keys:
            enqueued = enqueue(outbox, planned, now)
        update_incident_state(result, state, planned, now)
        update_transient_state(result, state, planned, now)
        deliveries = flush_outbox(
            outbox=outbox,
            state=state,
            audit_path=audit_path,
            lark_cli=args.lark_cli,
            chat_id=args.chat_id,
            now=now,
            no_send=args.no_send,
            send_attempts=max(args.send_attempts, 1),
        )
        state["updated_at"] = now_local().isoformat()
        state["last_decision"] = {
            "pipeline": args.pipeline,
            "reason": decision_reason,
            "event": planned.event if planned else None,
            "idempotency_key": planned.key if planned else None,
            "at": now.isoformat(),
        }
        atomic_write_json(state_path, state)
        atomic_write_json(outbox_path, outbox)

    print(
        json.dumps(
            {
                "decision": "send" if planned else "silent",
                "reason": decision_reason,
                "event": planned.event if planned else None,
                "enqueued": enqueued,
                "deliveries": deliveries,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
