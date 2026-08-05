#!/usr/bin/env python3
"""Shared status and reason mapping for ZSXQ autodownload runs."""

from __future__ import annotations

SUCCESS_NO_DOWNLOAD_REASONS = frozenset(
    {
        "no_new_documents",
        "no_new_docs",
        "no_keyword_match",
        "no_window_updates",
        "already_archived_duplicates",
    }
)

SUCCESS_CORE_REASONS = frozenset(
    {
        "",
        "download_completed",
        "window_no_updates",
        "window_no_new_docs",
        "window_has_no_new_documents",
        "window_has_updates_but_no_keyword_match",
        "window_has_no_updates",
        "window_candidates_already_archived",
        "no_download_reason_unknown",
    }
)

CANONICAL_NO_DOWNLOAD_REASON_BY_ALIAS = {
    "no_new_docs": "no_new_documents",
    "no_window_new_docs": "no_new_documents",
}

CANONICAL_REASON_CODE_BY_ALIAS = {
    "download_success": "download_completed",
    "downloads_completed": "download_completed",
    "scan_api_ok_no_download_candidates": "window_has_no_new_documents",
    "all_candidates_source_content_protected": "source_content_protected",
    "source_web_download_protected": "source_content_protected",
    "window_no_updates": "window_has_no_updates",
    "window_no_new_docs": "window_has_no_new_documents",
}

CORE_REASON_BY_NO_DOWNLOAD_REASON = {
    "blocked_browser_unavailable": "blocked_browser_unavailable_or_interrupted",
    "blocked_browser_endpoint_unavailable": "blocked_browser_endpoint_unavailable",
    "blocked_browser_cdp_unresponsive": "blocked_browser_cdp_unresponsive",
    "need_reauth": "need_reauth",
    "zsxq_page_unavailable": "zsxq_page_unavailable",
    "zsxq_page_state_unrecognized": "zsxq_page_state_unrecognized",
    "source_content_protected": "source_content_protected",
    "no_new_documents": "window_has_no_new_documents",
    "no_new_docs": "window_has_no_new_documents",
    "no_keyword_match": "window_has_updates_but_no_keyword_match",
    "no_window_updates": "window_has_no_updates",
    "download_incomplete": "download_candidates_not_completed",
}

NO_DOWNLOAD_REASON_BY_CORE_REASON = {
    "need_reauth": "need_reauth",
    "blocked_browser_missing": "blocked_browser",
    "blocked_browser_endpoint_unavailable": "blocked_browser",
    "blocked_browser_cdp_unresponsive": "blocked_browser",
    "blocked_browser_unavailable_or_interrupted": "blocked_browser",
    "zsxq_page_unavailable": "zsxq_page_unavailable",
    "zsxq_page_state_unrecognized": "zsxq_page_state_unrecognized",
    "source_content_protected": "source_content_protected",
    "window_has_no_new_documents": "no_new_documents",
    "window_has_updates_but_no_keyword_match": "no_keyword_match",
    "window_has_no_updates": "no_window_updates",
}

REASON_TEXT_BY_CODE = {
    "need_reauth": "浏览器可以使用，但知识星球登录态已失效",
    "blocked_browser_missing": "Chrome for Testing 浏览器未找到或不可执行",
    "blocked_browser_endpoint_unavailable": "Chrome for Testing 未能提供可用的 9223 调试端口",
    "blocked_browser_cdp_unresponsive": "浏览器进程和调试端口仍在，但 Playwright 接管浏览器超时",
    "blocked_browser_unavailable_or_interrupted": "Chrome 会话异常中断，下载流程未完成",
    "zsxq_page_unavailable": "浏览器可以连接，但知识星球页面加载失败",
    "zsxq_page_state_unrecognized": "知识星球页面已打开，但没有识别到星球内容或登录提示",
    "source_content_protected": "星主已开启内容保护，知识星球网页端不提供该文件下载",
    "blocked_documents_permission": "当前进程无法读取 ~/Documents 下的 Codex 任务文件，常见于 cron 或后台进程缺少 Full Disk Access",
    "cloud_requirements_timeout": "Codex 云端依赖检查超时，任务还没进入知识星球扫描就提前退出了",
    "codex_exec_timeout": "Codex 执行超过硬超时，进程组已终止，下载游标未推进",
    "api_unavailable_then_playwright_mcp_cdp_timeout": "知识星球接口异常，回退到页面扫描后又遇到浏览器连接超时，本轮检查未完整完成",
    "task_interrupted_sigterm": "任务运行中被终止（SIGTERM），常见原因是调度超时、人工停止，或主进程被误终止",
    "self_terminated_codex_runner": "任务运行中误终止了自己的 Codex 主进程",
    "busy_locked": "已有同类任务仍在执行，本次触发被跳过",
    "task_failed": "任务执行失败，请查看日志定位具体原因",
    "window_no_new_docs": "本时间窗口内没有新文档",
    "window_no_updates": "本时间窗口内没有新更新",
    "window_has_no_new_documents": "本时间窗口内没有新文档",
    "window_has_updates_but_no_keyword_match": "本时间窗口内有更新，但没有命中兴趣关键词",
    "window_has_no_updates": "本时间窗口内没有新更新",
    "download_candidates_not_completed": "检测到候选文档，但下载没有完成",
    "download_manifest_invariant_failed": "下载运行账本校验未通过，未推进扫描游标",
    "no_download_reason_unknown": "原因暂未能结构化判断",
    "download_completed": "下载并归档完成",
    "window_candidates_already_archived": "候选内容已在资料库中，无需重复下载",
}


def normalize_no_download_reason(no_download_reason: str) -> str:
    raw = str(no_download_reason or "").strip()
    return CANONICAL_NO_DOWNLOAD_REASON_BY_ALIAS.get(raw, raw)


def normalize_reason_code(reason_code: str) -> str:
    raw = str(reason_code or "").strip()
    return CANONICAL_REASON_CODE_BY_ALIAS.get(raw, raw)


def core_reason_from_no_download_reason(no_download_reason: str) -> str:
    return CORE_REASON_BY_NO_DOWNLOAD_REASON.get(normalize_no_download_reason(no_download_reason), "")


def no_download_reason_from_core_reason(core_reason: str) -> str:
    return NO_DOWNLOAD_REASON_BY_CORE_REASON.get(normalize_reason_code(core_reason), "")


def classify_report_status(
    *,
    codex_rc: int,
    downloaded_count: int,
    no_download_reason: str,
    core_reason: str,
    scan_alert: str,
) -> str:
    if codex_rc == 23:
        return "busy"
    if codex_rc == 20:
        return "blocked_login"
    if codex_rc in {21, 22}:
        return "blocked_browser"
    if codex_rc != 0:
        return "failed"
    if downloaded_count > 0:
        return "success"
    if str(no_download_reason or "").strip() in SUCCESS_NO_DOWNLOAD_REASONS:
        return "success"
    if str(no_download_reason or "").strip() == "download_incomplete":
        return "partial"
    if str(scan_alert or "").strip():
        return "partial"
    if str(core_reason or "").strip() not in SUCCESS_CORE_REASONS:
        return "partial"
    return "success"
