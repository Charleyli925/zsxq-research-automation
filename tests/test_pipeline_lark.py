from __future__ import annotations

import json

import pytest

from zsxq_pipeline import lark as lark_module
from zsxq_pipeline.lark import (
    LarkCliConfig,
    LarkCommandError,
    LarkCommandResult,
    LarkNotifier,
    LarkPublisher,
)


DOC_URL = "https://feishu.cn/docx/doxcn12345678"
MARKDOWN = """# 每日研究摘要

## 核心结论
- 收入增长来自高端产品放量，毛利率改善具有持续性。
- 管理层预计下半年现金流继续改善，估值存在修复空间。

## 核心问题与回答

### 1. 增长的主要驱动是什么？
报告认为高端产品和渠道效率是增长的主要驱动。
"""


class _FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, argv, *, input_text, environment, timeout_seconds):
        command = tuple(argv)
        self.calls.append(
            {
                "argv": command,
                "input": input_text,
                "environment": dict(environment),
                "timeout": timeout_seconds,
            }
        )
        if command[1:3] == ("docs", "+create"):
            payload: object = {"data": {"doc_url": DOC_URL}}
        elif command[1:3] == ("docs", "+update"):
            payload = {"data": {"url": DOC_URL}}
        elif command[1:3] == ("drive", "files"):
            payload = {"data": {"title": "每日研究摘要"}}
        elif command[1:3] == ("drive", "+inspect"):
            payload = {"data": {"title": "每日研究摘要"}}
        elif command[1:3] == ("docs", "+fetch"):
            payload = {"data": {"document": {"content": MARKDOWN}}}
        elif command[1:3] == ("im", "+messages-send"):
            payload = {"data": {"message_id": "om_123"}}
        else:
            payload = {"code": 0}
        return LarkCommandResult(argv=command, returncode=0, stdout=json.dumps(payload, ensure_ascii=False))


def _config(tmp_path) -> LarkCliConfig:
    return LarkCliConfig(command="lark-cli-test", config_dir=tmp_path / "lark-profile", timeout_seconds=12)


def test_document_write_is_user_markdown_then_higher_layer_can_verify_and_grant(tmp_path):
    executor = _FakeExecutor()
    publisher = LarkPublisher(_config(tmp_path), executor=executor)

    written = publisher.create_document(MARKDOWN, parent_position="root-folder")
    # This return point is intentionally the remote_written transaction boundary.
    remote_record = {"state": "remote_written", "remote_reference": written.url}
    assert remote_record == {"state": "remote_written", "remote_reference": DOC_URL}
    assert len(executor.calls) == 1

    publisher.set_title(written.url, "每日研究摘要")
    publisher.verify_title(written.url, "每日研究摘要")
    fetched = publisher.verify_body(written.url, MARKDOWN)
    publisher.grant_chat_view(written.url, "oc_chat_123")

    assert fetched.url == DOC_URL
    create = executor.calls[0]
    create_argv = create["argv"]
    assert create_argv[:7] == (
        "lark-cli-test",
        "docs",
        "+create",
        "--api-version",
        "v2",
        "--as",
        "user",
    )
    assert "--content" in create_argv and create_argv[create_argv.index("--content") + 1] == "-"
    assert "--doc-format" in create_argv and create_argv[create_argv.index("--doc-format") + 1] == "markdown"
    assert create["input"] == MARKDOWN
    assert create["environment"]["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] == "1"
    assert create["environment"]["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] == "1"
    assert create["environment"]["LARKSUITE_CLI_CONFIG_DIR"] == str(tmp_path / "lark-profile")

    permission_argv = executor.calls[-1]["argv"]
    assert permission_argv[:6] == (
        "lark-cli-test",
        "drive",
        "permission.members",
        "create",
        "--as",
        "user",
    )
    params = json.loads(permission_argv[permission_argv.index("--params") + 1])
    data = json.loads(permission_argv[permission_argv.index("--data") + 1])
    assert params == {"token": "doxcn12345678", "type": "docx", "need_notification": False}
    assert data == {"member_type": "chat", "member_id": "oc_chat_123", "perm": "view"}


def test_append_uses_user_identity_markdown_stdin_and_append_command(tmp_path):
    executor = _FakeExecutor()
    publisher = LarkPublisher(_config(tmp_path), executor=executor)

    document = publisher.append_document(DOC_URL, MARKDOWN)

    call = executor.calls[0]
    argv = call["argv"]
    assert document.url == DOC_URL
    assert document.mode == "append"
    assert argv[:7] == (
        "lark-cli-test",
        "docs",
        "+update",
        "--api-version",
        "v2",
        "--as",
        "user",
    )
    assert argv[argv.index("--command") + 1] == "append"
    assert argv[argv.index("--content") + 1] == "-"
    assert call["input"] == MARKDOWN


def test_bot_notification_requires_and_reuses_a_remote_idempotency_key(tmp_path):
    executor = _FakeExecutor()
    notifier = LarkNotifier(_config(tmp_path), executor=executor)

    first = notifier.notify_once("oc_chat_123", "[摘要](https://feishu.cn/docx/x)", idempotency_key="publication:abc")
    second = notifier.notify_once("oc_chat_123", "[摘要](https://feishu.cn/docx/x)", idempotency_key="publication:abc")

    assert first.message_id == "om_123"
    assert second.idempotency_key == "publication:abc"
    for call in executor.calls:
        argv = call["argv"]
        assert argv[:3] == ("lark-cli-test", "im", "+messages-send")
        assert argv[argv.index("--as") + 1] == "bot"
        assert argv[argv.index("--idempotency-key") + 1] == "publication:abc"


def test_lark_error_diagnostics_redact_tokens_and_capability_checks_only_use_help(tmp_path):
    def failing_executor(argv, *, input_text, environment, timeout_seconds):
        return LarkCommandResult(
            argv=tuple(argv),
            returncode=1,
            stderr="tenant_access_token=super-secret Authorization: Bearer another-secret",
        )

    publisher = LarkPublisher(_config(tmp_path), executor=failing_executor)
    with pytest.raises(LarkCommandError) as exc_info:
        publisher.create_document(MARKDOWN)
    rendered = str(exc_info.value)
    assert "super-secret" not in rendered
    assert "another-secret" not in rendered

    executor = _FakeExecutor()
    publisher = LarkPublisher(_config(tmp_path), executor=executor)
    notifier = LarkNotifier(_config(tmp_path), executor=executor)
    publisher.capability_preflight()
    notifier.capability_preflight()
    assert executor.calls
    for call in executor.calls:
        argv = call["argv"]
        assert argv[-1] == "--help"
        assert call["input"] is None


def test_default_subprocess_executor_uses_an_argv_array_and_never_a_shell(monkeypatch):
    calls: list[tuple[object, dict[str, object]]] = []

    class _Completed:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return _Completed()

    monkeypatch.setattr(lark_module.subprocess, "run", fake_run)
    result = lark_module._run_subprocess(
        ("lark-cli", "docs", "+fetch", "--help"),
        input_text=None,
        environment={"SAFE": "1"},
        timeout_seconds=1,
    )

    assert result.returncode == 0
    assert calls[0][0] == ["lark-cli", "docs", "+fetch", "--help"]
    assert calls[0][1]["shell"] is False
