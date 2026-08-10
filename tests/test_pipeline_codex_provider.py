from __future__ import annotations

import json
import signal
import subprocess
from pathlib import Path

import pytest

from zsxq_pipeline.providers import codex as codex_module
from zsxq_pipeline.providers.codex import (
    CodexCommandResult,
    CodexProviderConfig,
    CodexSummaryInput,
    CodexSummaryProvider,
    CodexSummaryRequest,
    CodexTimeoutError,
    SummaryOutputValidationError,
    summary_schema_path,
    validate_summary_payload,
)


def _request() -> CodexSummaryRequest:
    return CodexSummaryRequest(
        job_id="batch-20260810",
        manifest={"files": [{"path": "/runtime/clean/report-a.md", "filename": "report-a.pdf"}]},
        inputs=(
            CodexSummaryInput(
                path="/runtime/clean/report-a.md",
                filename="report-a.pdf",
                text="这是已经提取好的正文。收入增长，估值有上行空间。",
            ),
        ),
        system_prompt="只依据正文总结。",
        prompt="生成中文摘要。",
    )


def _success_payload() -> dict[str, object]:
    return {
        "status": "success",
        "handled_count": 1,
        "handled_paths": ["/runtime/clean/report-a.md"],
        "summaries": [
            {
                "path": "/runtime/clean/report-a.md",
                "filename": "report-a.pdf",
                "title": "报告 A",
                "quality_hint": "",
                "markdown": "# 报告 A\n\n## 核心结论\n- 收入增长。",
            }
        ],
    }


class _FakeProcess:
    def __init__(self, output_path: Path, payload: object, *, stdout: str = "", timeout_plan: list[bool] | None = None) -> None:
        self.output_path = output_path
        self.payload = payload
        self.stdout = stdout
        self.timeout_plan = list(timeout_plan or [])
        self.pid = 48271
        self.returncode: int | None = 0
        self.communicate_calls: list[tuple[str | None, float | None]] = []

    def communicate(self, input: str | None = None, timeout: float | None = None) -> tuple[str, str]:
        self.communicate_calls.append((input, timeout))
        if self.timeout_plan and self.timeout_plan.pop(0):
            raise subprocess.TimeoutExpired("codex", timeout or 0)
        if not self.output_path.exists():
            self.output_path.write_text(json.dumps(self.payload, ensure_ascii=False), encoding="utf-8")
        return self.stdout, ""

    def terminate(self) -> None:  # pragma: no cover - POSIX process groups are exercised instead
        pass

    def kill(self) -> None:  # pragma: no cover - POSIX process groups are exercised instead
        pass


class _RecordingPopen:
    def __init__(self, payload: object, *, stdout: str = "", timeout_plan: list[bool] | None = None) -> None:
        self.payload = payload
        self.stdout = stdout
        self.timeout_plan = timeout_plan
        self.argv: tuple[str, ...] = ()
        self.kwargs: dict[str, object] = {}
        self.process: _FakeProcess | None = None
        self.workdir: Path | None = None
        self.stdin_text = ""

    def __call__(self, argv, **kwargs):
        self.argv = tuple(argv)
        self.kwargs = kwargs
        self.workdir = Path(str(kwargs["cwd"]))
        output_path = Path(self.argv[self.argv.index("--output-last-message") + 1])
        self.process = _FakeProcess(output_path, self.payload, stdout=self.stdout, timeout_plan=self.timeout_plan)
        return self.process


def test_direct_codex_call_uses_isolated_argv_and_only_last_message_output(tmp_path):
    popen = _RecordingPopen(_success_payload(), stdout='{"event":"completed","usage":{"input_tokens":123}}\n')
    provider = CodexSummaryProvider(
        CodexProviderConfig(model="gpt-test", work_root=tmp_path, retain_workdir=True),
        popen_factory=popen,
    )

    result = provider.summarize(_request())

    assert result.output["status"] == "success"
    assert result.usage == {"input_tokens": 123}
    assert result.workdir is not None
    assert popen.argv[:8] == (
        "codex",
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--sandbox",
        "read-only",
        "--model",
    )
    assert popen.argv[popen.argv.index("--model") + 1] == "gpt-test"
    assert popen.argv[popen.argv.index("--config") + 1] == 'model_reasoning_effort="medium"'
    assert Path(popen.argv[popen.argv.index("--output-schema") + 1]) == summary_schema_path()
    assert popen.argv[-1] == "-"
    assert popen.kwargs["shell"] is False
    assert popen.kwargs["start_new_session"] is True
    assert popen.workdir is not None
    assert popen.workdir.parent == tmp_path
    assert result.workdir == popen.workdir
    assert {path.name for path in popen.workdir.iterdir()} == {
        "batch-manifest.json",
        "inputs",
        "prompt.md",
        "summary-system.md",
        "result.json",
    }
    manifest = json.loads((popen.workdir / "batch-manifest.json").read_text(encoding="utf-8"))
    assert manifest["inputs"] == [
        {
            "path": "/runtime/clean/report-a.md",
            "filename": "report-a.pdf",
            "text_path": "inputs/0001.md",
        }
    ]
    assert (popen.workdir / "inputs" / "0001.md").read_text(encoding="utf-8").startswith("这是已经提取好的正文")
    assert popen.process is not None
    stdin_text = popen.process.communicate_calls[0][0]
    assert stdin_text is not None
    assert "exactly one JSON object" in stdin_text
    assert "report-a.md" in stdin_text
    # The only source path in the process directory is a logical manifest value;
    # text is copied into an isolated input file, not read from its source path.
    assert not (tmp_path / "runtime").exists()


def test_summary_schema_asset_forbids_free_form_root_and_entry_fields():
    schema = json.loads(summary_schema_path().read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert schema["properties"]["summaries"]["items"]["additionalProperties"] is False
    assert set(schema["properties"]) == {"status", "handled_count", "handled_paths", "summaries", "error"}


def test_schema_validation_rejects_extra_fields_and_manifest_mismatches():
    payload = _success_payload()
    payload["surprise"] = "free-form output"
    with pytest.raises(SummaryOutputValidationError, match="unsupported"):
        validate_summary_payload(payload, expected_paths=("/runtime/clean/report-a.md",))

    payload = _success_payload()
    payload["handled_paths"] = ["/somewhere/else.md"]
    with pytest.raises(SummaryOutputValidationError, match="manifest"):
        validate_summary_payload(payload, expected_paths=("/runtime/clean/report-a.md",))


def test_invalid_last_message_is_rejected_before_any_artifact_can_be_accepted(tmp_path):
    invalid = _success_payload()
    invalid["summaries"] = [{"path": "/runtime/clean/report-a.md"}]
    provider = CodexSummaryProvider(
        CodexProviderConfig(model="gpt-test", work_root=tmp_path),
        popen_factory=_RecordingPopen(invalid),
    )

    with pytest.raises(SummaryOutputValidationError, match="missing required"):
        provider.summarize(_request())


def test_hard_timeout_terminates_then_kills_the_isolated_process_group(tmp_path, monkeypatch):
    popen = _RecordingPopen(_success_payload(), timeout_plan=[True, True, False])
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(codex_module.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    provider = CodexSummaryProvider(
        CodexProviderConfig(model="gpt-test", work_root=tmp_path, timeout_seconds=1, terminate_grace_seconds=0),
        popen_factory=popen,
    )

    with pytest.raises(CodexTimeoutError, match="hard timeout"):
        provider.summarize(_request())

    assert signals == [(48271, signal.SIGTERM), (48271, signal.SIGKILL)]


def test_capability_preflight_is_injectable_and_only_asks_for_help(tmp_path):
    calls: list[tuple[str, ...]] = []

    def help_executor(argv: tuple[str, ...]) -> CodexCommandResult:
        calls.append(argv)
        return CodexCommandResult(argv=argv, returncode=0, stdout="usage")

    provider = CodexSummaryProvider(
        CodexProviderConfig(model="gpt-test", work_root=tmp_path),
        help_executor=help_executor,
    )

    assert provider.capability_preflight().stdout == "usage"
    assert calls == [("codex", "exec", "--help")]
