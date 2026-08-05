from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.zsxq_notification_policy import (
    PlannedEvent,
    cancel_undelivered_alerts,
    cancel_pending_recoveries,
    cancel_superseded_pending_transitions,
    decide,
    default_outbox,
    default_state,
    enqueue,
    failure_guidance,
    flush_outbox,
    normalize_pending_keys,
    parse_args,
    update_incident_state,
    update_transient_state,
)


class ZsxqNotificationPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime.fromisoformat("2026-07-14T12:30:00+08:00")

    def test_foreign_success_is_silent_for_digest_to_aggregate(self) -> None:
        event, reason = decide(
            {"status": "success", "downloaded_count": 4},
            "foreign_download",
            default_state(),
            self.now,
        )
        self.assertIsNone(event)
        self.assertEqual(reason, "routine_success_silent")

    def test_cli_defaults_to_one_inline_send_attempt(self) -> None:
        with patch(
            "sys.argv",
            [
                "zsxq_notification_policy.py",
                "--result",
                "result.json",
                "--pipeline",
                "foreign_download",
                "--state",
                "state.json",
                "--outbox",
                "outbox.json",
                "--audit",
                "audit.jsonl",
                "--chat-id",
                "oc_test",
            ],
        ):
            args = parse_args()

        self.assertEqual(args.send_attempts, 1)

    def test_content_protection_guidance_does_not_recommend_bypass(self) -> None:
        diagnosis, cause, action = failure_guidance(
            "source_content_protected",
            "",
        )
        self.assertIn("内容保护", diagnosis)
        self.assertIn("源站权限策略", cause)
        self.assertIn("不会绕过", action)

    def test_domestic_empty_and_busy_are_silent(self) -> None:
        empty, _ = decide(
            {"status": "success", "downloaded_count": 0},
            "domestic_cicc",
            default_state(),
            self.now,
        )
        busy, _ = decide({"status": "busy"}, "domestic_cicc", default_state(), self.now)
        self.assertIsNone(empty)
        self.assertIsNone(busy)

    def test_second_consecutive_busy_state_escalates_and_can_recover(self) -> None:
        state = default_state()
        busy_result = {
            "status": "busy",
            "exit_code": 23,
            "core_reason_code": "busy_locked",
            "core_reason_text": "已有同类任务仍在执行，本次触发被跳过",
            "window_start": "2026-07-14T08:00:00+08:00",
            "window_end": "2026-07-14T12:00:00+08:00",
        }

        first, first_reason = decide(busy_result, "foreign_download", state, self.now)
        self.assertIsNone(first)
        self.assertEqual(first_reason, "routine_transient_state")
        update_incident_state(busy_result, state, first, self.now)
        update_transient_state(busy_result, state, first, self.now)

        second_at = datetime.fromisoformat("2026-07-14T16:30:00+08:00")
        second, second_reason = decide(busy_result, "foreign_download", state, second_at)
        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(second_reason, "persistent_transient_state")
        self.assertIn("持续阻塞", second.message)
        self.assertIn("连续出现 2 次", second.message)
        update_incident_state(busy_result, state, second, second_at)
        update_transient_state(busy_result, state, second, second_at)
        state["sent_event_keys"] = [second.key]

        recovered, reason = decide(
            {"status": "success", "downloaded_count": 0},
            "foreign_download",
            state,
            datetime.fromisoformat("2026-07-14T16:35:00+08:00"),
        )
        self.assertIsNotNone(recovered)
        self.assertEqual(reason, "incident_recovered")

    def test_domestic_download_gets_one_compact_completion(self) -> None:
        event, reason = decide(
            {
                "status": "success",
                "downloaded_count": 1,
                "downloaded_files": ["中金公司_测试.pdf"],
                "window_start": "2026-07-14T08:20:00+08:00",
                "window_end": "2026-07-14T12:20:00+08:00",
            },
            "domestic_cicc",
            default_state(),
            self.now,
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.event, "completion")
        self.assertEqual(reason, "new_domestic_reports")
        self.assertIn("本轮新增 1 篇", event.message)
        self.assertNotIn("日志", event.message)

    def test_failure_alert_is_suppressed_for_same_incident_within_4h(self) -> None:
        result = {
            "status": "failed",
            "exit_code": 21,
            "core_reason_code": "blocked_browser_missing",
            "core_reason_text": "浏览器不可用",
        }
        first, _ = decide(result, "foreign_download", default_state(), self.now)
        self.assertIsNotNone(first)
        signature = __import__("scripts.zsxq_notification_policy", fromlist=["incident_signature"]).incident_signature(result)
        state = default_state()
        state["incident"] = {
            "active": True,
            "signature": signature,
            "last_alert_at": "2026-07-14T10:00:00+08:00",
        }
        repeated, reason = decide(result, "foreign_download", state, self.now)
        self.assertIsNone(repeated)
        self.assertEqual(reason, "incident_already_alerted")

    def test_same_incident_gets_a_new_diagnostic_reminder_after_4h(self) -> None:
        result = {
            "status": "blocked_browser",
            "exit_code": 22,
            "core_reason_code": "blocked_browser_cdp_unresponsive",
            "core_reason_text": "Playwright 接管浏览器超时",
        }
        signature = __import__("scripts.zsxq_notification_policy", fromlist=["incident_signature"]).incident_signature(result)
        state = default_state()
        state["incident"] = {
            "active": True,
            "signature": signature,
            "last_alert_at": "2026-07-14T08:00:00+08:00",
        }

        repeated, reason = decide(result, "foreign_download", state, self.now)

        self.assertIsNotNone(repeated)
        self.assertEqual(reason, "actionable_failure")

    def test_same_failure_after_recovery_uses_a_new_idempotency_key(self) -> None:
        first_result = {
            "status": "failed",
            "core_reason_code": "task_failed",
            "execute_time": "2026-07-14T12:00:00+08:00",
        }
        state = default_state()
        first, _ = decide(first_result, "foreign_download", state, self.now)
        self.assertIsNotNone(first)
        assert first is not None
        update_incident_state(first_result, state, first, self.now)
        state["sent_event_keys"] = [first.key]

        success = {"status": "success", "downloaded_count": 0}
        update_incident_state(success, state, None, self.now)

        later_result = {
            **first_result,
            "execute_time": "2026-07-15T12:00:00+08:00",
        }
        later, _ = decide(
            later_result,
            "foreign_download",
            state,
            datetime.fromisoformat("2026-07-15T12:00:00+08:00"),
        )

        self.assertIsNotNone(later)
        assert later is not None
        self.assertNotEqual(first.key, later.key)

    def test_success_after_incident_gets_recovery(self) -> None:
        state = default_state()
        state["incident"] = {
            "active": True,
            "signature": "abc",
            "last_alert_at": "2026-07-14T10:00:00+08:00",
            "last_alert_key": "alert-key",
        }
        state["sent_event_keys"] = ["alert-key"]
        event, reason = decide(
            {"status": "success", "downloaded_count": 0},
            "foreign_download",
            state,
            self.now,
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.event, "recovery")
        self.assertEqual(reason, "incident_recovered")

    def test_success_does_not_announce_recovery_for_undelivered_alert(self) -> None:
        state = default_state()
        state["incident"] = {
            "active": True,
            "signature": "abc",
            "last_alert_at": "2026-07-14T10:00:00+08:00",
            "last_alert_key": "alert-key",
        }
        event, reason = decide(
            {"status": "success", "downloaded_count": 0},
            "foreign_download",
            state,
            self.now,
        )
        self.assertIsNone(event)
        self.assertEqual(reason, "routine_success_silent")

    def test_success_cancels_pending_stale_alert(self) -> None:
        outbox = default_outbox()
        enqueue(outbox, PlannedEvent("alert", "key-3", "warning", "warning"), self.now)
        self.assertEqual(cancel_undelivered_alerts(outbox, self.now), 1)
        self.assertEqual(outbox["entries"][0]["status"], "cancelled")

    def test_new_failure_cancels_pending_stale_recovery(self) -> None:
        outbox = default_outbox()
        enqueue(outbox, PlannedEvent("recovery", "key-r", "recovered", "recovery"), self.now)
        self.assertEqual(cancel_pending_recoveries(outbox, self.now), 1)
        self.assertEqual(outbox["entries"][0]["status"], "cancelled")
        self.assertEqual(outbox["entries"][0]["cancel_reason"], "new_failure_before_delivery")

    def test_new_alert_supersedes_older_pending_alert_before_flush(self) -> None:
        outbox = default_outbox()
        enqueue(outbox, PlannedEvent("alert", "old-alert", "old", "warning"), self.now)
        replacement = PlannedEvent("alert", "new-alert", "new", "warning")
        self.assertEqual(cancel_superseded_pending_transitions(outbox, replacement, self.now), 1)
        enqueue(outbox, replacement, self.now)
        self.assertEqual(outbox["entries"][0]["status"], "cancelled")
        self.assertEqual(outbox["entries"][0]["superseded_by"], "new-alert")
        self.assertEqual(outbox["entries"][1]["status"], "pending")

    def test_zero_of_n_partial_is_rendered_as_not_completed(self) -> None:
        event, _ = decide(
            {
                "status": "partial",
                "download_candidate_count": 4,
                "download_success_count": 0,
                "core_reason_code": "download_manifest_invariant_failed",
                "core_reason_text": "运行账本校验未通过",
            },
            "foreign_download",
            default_state(),
            self.now,
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertIn("**状态**：未完成", event.message)
        self.assertNotIn("**状态**：部分完成", event.message)

    def test_manual_action_failure_does_not_claim_automatic_recovery(self) -> None:
        event, _ = decide(
            {
                "status": "blocked_login",
                "core_reason_code": "need_reauth",
                "core_reason_text": "登录态失效",
            },
            "foreign_download",
            default_state(),
            self.now,
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertIn("请重新登录知识星球", event.message)
        self.assertNotIn("无需手工重启", event.message)

    def test_browser_cdp_failure_message_explains_half_alive_session(self) -> None:
        event, _ = decide(
            {
                "status": "blocked_browser",
                "core_reason_code": "blocked_browser_cdp_unresponsive",
                "core_reason_text": "Playwright 接管浏览器超时",
            },
            "foreign_download",
            default_state(),
            self.now,
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertIn("**定位结果**", event.message)
        self.assertIn("9223", event.message)
        self.assertIn("假在线", event.message)
        self.assertIn("重启专用 Chrome for Testing", event.message)

    def test_login_failure_message_says_browser_is_healthy_but_login_expired(self) -> None:
        event, _ = decide(
            {
                "status": "blocked_login",
                "core_reason_code": "need_reauth",
                "core_reason_text": "登录态失效",
            },
            "foreign_download",
            default_state(),
            self.now,
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertIn("浏览器可以使用", event.message)
        self.assertIn("登录态已过期", event.message)
        self.assertIn("使用专用 Chrome for Testing", event.message)

    def test_zsxq_page_failure_message_separates_site_or_network_from_browser(self) -> None:
        event, _ = decide(
            {
                "status": "blocked_site",
                "core_reason_code": "zsxq_page_unavailable",
                "core_reason_text": "知识星球页面加载失败",
            },
            "domestic_cicc",
            default_state(),
            self.now,
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertIn("浏览器可以连接", event.message)
        self.assertIn("网络、DNS/代理", event.message)
        self.assertIn("知识星球", event.message)

    def test_idempotency_keys_fit_feishu_limit(self) -> None:
        event, _ = decide(
            {"status": "failed", "core_reason_code": "task_failed"},
            "foreign_download",
            default_state(),
            self.now,
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertLessEqual(len(event.key), 50)

    def test_legacy_pending_key_is_migrated_with_incident_pointer(self) -> None:
        old_key = "x" * 52
        outbox = default_outbox()
        outbox["entries"] = [
            {
                "event": "alert",
                "idempotency_key": old_key,
                "message": "warning",
                "status": "pending",
            }
        ]
        state = default_state()
        state["incident"] = {"active": True, "last_alert_key": old_key}
        self.assertEqual(normalize_pending_keys(outbox, state, "foreign_download"), 1)
        new_key = outbox["entries"][0]["idempotency_key"]
        self.assertLessEqual(len(new_key), 50)
        self.assertEqual(state["incident"]["last_alert_key"], new_key)

    def test_outbox_delivery_is_independent_and_records_message_id(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            audit_path = Path(raw_tmp) / "audit.jsonl"
            state = default_state()
            outbox = default_outbox()
            event = PlannedEvent("alert", "key-1", "## warning", "warning")
            self.assertTrue(enqueue(outbox, event, self.now))

            with patch(
                "scripts.zsxq_notification_policy.send_lark_message",
                return_value=(True, "om_test", ""),
            ):
                deliveries = flush_outbox(
                    outbox=outbox,
                    state=state,
                    audit_path=audit_path,
                    lark_cli="lark-cli",
                    chat_id="test",
                    now=self.now,
                    no_send=False,
                    send_attempts=1,
                )

            self.assertEqual(deliveries, [{"event": "alert", "status": "success", "message_id": "om_test"}])
            self.assertEqual(outbox["entries"][0]["status"], "sent")
            self.assertIn("key-1", state["sent_event_keys"])
            self.assertIn("om_test", audit_path.read_text(encoding="utf-8"))

    def test_outbox_failure_waits_five_minutes_without_touching_job_result(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            audit_path = Path(raw_tmp) / "audit.jsonl"
            state = default_state()
            outbox = default_outbox()
            enqueue(outbox, PlannedEvent("alert", "key-2", "warning", "warning"), self.now)

            with patch(
                "scripts.zsxq_notification_policy.send_lark_message",
                return_value=(False, "", "network down"),
            ):
                deliveries = flush_outbox(
                    outbox=outbox,
                    state=state,
                    audit_path=audit_path,
                    lark_cli="lark-cli",
                    chat_id="test",
                    now=self.now,
                    no_send=False,
                    send_attempts=1,
                )

            entry = outbox["entries"][0]
            self.assertEqual(deliveries[0]["status"], "failed")
            self.assertEqual(entry["status"], "pending")
            self.assertEqual(entry["next_attempt_at"], "2026-07-14T12:35:00+08:00")
            self.assertNotIn("key-2", state["sent_event_keys"])

    def test_outbox_moves_to_dead_letter_after_three_scheduled_retries(self) -> None:
        with TemporaryDirectory() as raw_tmp:
            audit_path = Path(raw_tmp) / "audit.jsonl"
            state = default_state()
            outbox = default_outbox()
            enqueue(outbox, PlannedEvent("alert", "key-dead", "warning", "warning"), self.now)
            attempt_times = [
                self.now,
                datetime.fromisoformat("2026-07-14T12:35:00+08:00"),
                datetime.fromisoformat("2026-07-14T12:45:00+08:00"),
                datetime.fromisoformat("2026-07-14T13:05:00+08:00"),
            ]
            with patch(
                "scripts.zsxq_notification_policy.send_lark_message",
                return_value=(False, "", "network down"),
            ):
                for attempt_at in attempt_times:
                    flush_outbox(
                        outbox=outbox,
                        state=state,
                        audit_path=audit_path,
                        lark_cli="lark-cli",
                        chat_id="test",
                        now=attempt_at,
                        no_send=False,
                        send_attempts=1,
                    )

            entry = outbox["entries"][0]
            self.assertEqual(entry["attempt_count"], 4)
            self.assertEqual(entry["status"], "dead_letter")
            self.assertIsNone(entry["next_attempt_at"])


if __name__ == "__main__":
    unittest.main()
