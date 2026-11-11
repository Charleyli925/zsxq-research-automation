from __future__ import annotations

import json
import unicodedata
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

import pytest

from zsxq_pipeline.extract import ExtractionBatchResult, ExtractionItem, sha256_file
from zsxq_pipeline.model import ErrorCategory, Stage, StageState, SummaryIdentity
from zsxq_pipeline.lark import LarkCommandError, LarkTimeoutError
from zsxq_pipeline.publish import PublicationError
from zsxq_pipeline.process import DigestProcessor, ProcessConfig, ProcessRequest
from zsxq_pipeline.providers.codex import CodexExecutionError, CodexTimeoutError
from zsxq_pipeline.state import PipelineState
from zsxq_pipeline.sidecars import SidecarArchiveResult, SidecarError


NOW = datetime(2026, 8, 10, 2, 3, 4, tzinfo=timezone.utc)


class FakeExtractor:
    def __init__(self, *, failed_names: set[str] | None = None, fail_if_called: bool = False) -> None:
        self.failed_names = failed_names or set()
        self.fail_if_called = fail_if_called
        self.calls = 0

    def preflight(self):
        return {"ok": True}

    def extract_batch(self, batch_file, output_dir):
        self.calls += 1
        if self.fail_if_called:
            raise AssertionError("durable extraction should not call the extractor")
        payload = json.loads(Path(batch_file).read_text(encoding="utf-8"))
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        items = []
        for raw in payload["files"]:
            path = str(raw["path"])
            filename = str(raw["filename"])
            if filename in self.failed_names:
                raw.update(
                    {
                        "text_extract_status": "failed",
                        "text_extract_error_type": "content_failure",
                        "text_extract_error_code": "pdf_unreadable",
                        "text_extract_error": "fixture unreadable PDF",
                    }
                )
                items.append(
                    ExtractionItem(
                        path=path,
                        filename=filename,
                        pdf_sha256=str(raw["pdf_sha256"]),
                        extractor_version="extract-fixture-v1",
                        status="failed",
                        error_category=ErrorCategory.CONTENT,
                        error_code="pdf_unreadable",
                        error_detail="fixture unreadable PDF",
                    )
                )
                continue
            text_path = output / f"{Path(path).stem}.md"
            text = f"正文：{filename} 的已提取研究内容。"
            text_path.write_text(text, encoding="utf-8")
            raw.update(
                {
                    "text_extract_status": "success",
                    "text_extract_profile": "extract-fixture-v1",
                    "extracted_text_path": str(text_path),
                    "extracted_text_chars": len(text),
                    "text_source": "fixture",
                    "text_extract_cached": False,
                }
            )
            items.append(
                ExtractionItem(
                    path=path,
                    filename=filename,
                    pdf_sha256=str(raw["pdf_sha256"]),
                    extractor_version="extract-fixture-v1",
                    status="success",
                    text_path=text_path,
                    text_chars=len(text),
                    text_source="fixture",
                )
            )
        return ExtractionBatchResult(manifest=payload, items=tuple(items), stdout="", stderr="")


class FakeProvider:
    def __init__(self, *, fail_if_called: bool = False) -> None:
        self.fail_if_called = fail_if_called
        self.requests = []

    def capability_preflight(self):
        return {"ok": True}

    def summarize(self, request):
        if self.fail_if_called:
            raise AssertionError("cache hit should not call Codex")
        self.requests.append(request)
        item = request.inputs[0]
        return {
            "status": "success",
            "handled_count": 1,
            "handled_paths": [item.path],
            "error": None,
            "summaries": [
                {
                    "path": item.path,
                    "filename": item.filename,
                    "title": f"{item.filename} 摘要",
                    "quality_hint": "fixture",
                    "markdown": f"# {item.filename} 摘要\n\n核心结论：{item.text}",
                }
            ],
        }


class TimeoutProvider(FakeProvider):
    def summarize(self, request):
        self.requests.append(request)
        raise CodexTimeoutError("fixture timeout")


class ExecutionErrorProvider(FakeProvider):
    def __init__(self, diagnostic: str) -> None:
        super().__init__()
        self.diagnostic = diagnostic

    def summarize(self, request):
        self.requests.append(request)
        raise CodexExecutionError(self.diagnostic)


class FakePublisher:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.appended: list[tuple[str, str]] = []
        self.titles: list[tuple[str, str]] = []
        self.permissions: list[tuple[str, str]] = []
        self.documents: dict[str, dict[str, str]] = {}

    def capability_preflight(self):
        return {"ok": True}

    def create_document(self, markdown: str):
        url = f"https://feishu.cn/docx/doxcn{len(self.created) + 12345678}"
        self.created.append(markdown)
        self.documents[url] = {"markdown": markdown, "title": ""}
        return {"url": url}

    def append_document(self, document_url: str, markdown: str):
        self.appended.append((document_url, markdown))
        self.documents.setdefault(document_url, {"markdown": "", "title": "existing"})["markdown"] += markdown
        return {"url": document_url}

    def set_title(self, document_url: str, title: str):
        self.titles.append((document_url, title))
        self.documents[document_url]["title"] = title

    def verify_title(self, document_url: str, expected_title: str):
        assert self.documents[document_url]["title"] == expected_title

    def verify_body(self, document_url: str, expected_markdown: str):
        assert expected_markdown in self.documents[document_url]["markdown"]

    def grant_chat_view(self, document_url: str, chat_id: str):
        self.permissions.append((document_url, chat_id))


class FailingPublisher(FakePublisher):
    def create_document(self, markdown: str):
        raise PublicationError("temporary Lark outage")


class FakeNotifier:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str, str]] = []
        self.compact_modes: list[bool] = []

    def capability_preflight(self):
        return {"ok": True}

    def notify_once(self, chat_id: str, markdown: str, *, idempotency_key: str, compact_document: bool = False):
        self.calls.append((chat_id, markdown, idempotency_key))
        self.compact_modes.append(compact_document)
        if self.error is not None:
            raise self.error
        return object()


class FakeSidecars:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.summary_paths: list[str] = []
        self.archives: list[tuple[tuple[str, ...], str]] = []
        self.published_titles = []

    def persist_summary(self, item, persisted):
        destination = self.root / "library" / f"{Path(item['filename']).stem}.summary.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(persisted.entry.markdown, encoding="utf-8")
        self.summary_paths.append(str(item["path"]))
        return destination

    def archive_published_group(self, *, entries, batch_items, document_url):
        self.archives.append((tuple(entry.path for entry in entries), document_url))
        self.published_titles.extend(entry.title for entry in entries)
        return SidecarArchiveResult(archived_count=len(entries))


class FailingOnceSidecars(FakeSidecars):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.failures_remaining = 1

    def archive_published_group(self, *, entries, batch_items, document_url):
        self.archives.append((tuple(entry.path for entry in entries), document_url))
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise SidecarError("temporary local projection failure")
        return SidecarArchiveResult(archived_count=len(entries))


def _config(runtime: Path) -> ProcessConfig:
    prompt = runtime / "prompts" / "summary.md"
    system = runtime / "prompts" / "summary-system.md"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("只总结正文。", encoding="utf-8")
    system.write_text("不要读 PDF。", encoding="utf-8")
    return ProcessConfig(
        runtime_root=runtime,
        database=runtime / "state" / "pipeline.sqlite3",
        source="zsxq",
        target="daily",
        target_document="",
        extractor_version="extract-fixture-v1",
        codex_command="codex-test",
        codex_model="model-fixture",
        codex_reasoning="medium",
        codex_timeout_seconds=20,
        codex_work_root=runtime / "work" / "codex",
        prompt_path=prompt,
        system_prompt_path=system,
        text_cache_root=runtime / "text_cache",
        summary_cache_root=runtime / "summary_cache",
        work_root=runtime / "work",
        batch_path=runtime / "pending_batch.json",
        result_path=runtime / "last_result.json",
        result_markdown_path=runtime / "last_result.md",
        run_status_path=runtime / "run_status.json",
        usage_path=runtime / "last_usage_summary.json",
        quarantine_path=runtime / "quarantine.json",
        notification_audit_path=runtime / "notification_messages.jsonl",
        research_library_root=None,
        research_library_database=None,
        obsidian_vault_root=None,
        lark_command="lark-cli-test",
        lark_config_dir=None,
        lark_timeout_seconds=20,
        lark_parent_position="my_library",
        target_chat_id="oc_test",
        notifications_enabled=True,
        summary_max_workers=2,
        doc_group_size=10,
        doc_group_threshold=15,
    )


def _batch(path: Path, pdfs: list[Path]) -> Path:
    payload = {
        "generated_at": NOW.isoformat(),
        "files": [
            {"path": str(pdf), "filename": pdf.name, "pdf_sha256": sha256_file(pdf)}
            for pdf in pdfs
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_process_uses_extracted_text_direct_codex_and_lark_boundaries(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"not a real PDF, never given to provider")
    extractor = FakeExtractor()
    provider = FakeProvider()
    publisher = FakePublisher()
    notifier = FakeNotifier()
    sidecars = FakeSidecars(tmp_path)
    processor = DigestProcessor(
        _config(runtime),
        extractor=extractor,
        provider=provider,
        publisher=publisher,
        notifier=notifier,
        sidecars=sidecars,
        clock=lambda: NOW,
    )

    outcome = processor.run(ProcessRequest(batch_file=_batch(tmp_path / "batch.json", [pdf])))

    assert outcome.status == "success"
    assert (outcome.extracted, outcome.summarized, outcome.published) == (1, 1, 1)
    assert provider.requests[0].inputs[0].text.startswith("正文：")
    assert "not a real PDF" not in provider.requests[0].inputs[0].text
    assert len(publisher.created) == 1
    assert publisher.titles and publisher.permissions == [(next(iter(publisher.documents)), "oc_test")]
    assert len(notifier.calls) == 1
    assert notifier.compact_modes == [True]
    assert "本次新增" in notifier.calls[0][1]
    assert "文档累计" in notifier.calls[0][1]
    assert sidecars.summary_paths == [str(pdf)]
    assert sidecars.published_titles == ["report.pdf 摘要"]
    assert sidecars.archives == [((str(pdf),), next(iter(publisher.documents)))]
    assert json.loads((runtime / "last_result.json").read_text(encoding="utf-8"))["status"] == "success"
    assert "openclaw" not in (runtime / "run_status.json").read_text(encoding="utf-8").lower()


def test_process_completes_an_nfd_pdf_path_without_leaking_summary_claims(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    pdf = tmp_path / unicodedata.normalize("NFD", "高盛-BÉIS-260814.pdf")
    pdf.write_bytes(b"fixture PDF")
    provider = FakeProvider()
    processor = DigestProcessor(
        _config(runtime),
        extractor=FakeExtractor(),
        provider=provider,
        publisher=FakePublisher(),
        notifier=FakeNotifier(),
        clock=lambda: NOW,
    )

    outcome = processor.run(ProcessRequest(batch_file=_batch(tmp_path / "unicode-batch.json", [pdf])))

    assert outcome.status == "success"
    assert provider.requests[0].expected_paths == (str(pdf),)
    with PipelineState.open(runtime / "state" / "pipeline.sqlite3") as state:
        attempts = state._connection.execute(
            "SELECT state, lease_token, lease_expires_at_epoch FROM stage_attempts WHERE stage=?",
            (Stage.SUMMARY.value,),
        ).fetchall()
    assert [(row["state"], row["lease_token"], row["lease_expires_at_epoch"]) for row in attempts] == [
        (StageState.SUCCEEDED.value, None, None)
    ]


def test_process_summary_cache_hit_skips_provider_after_a_previous_run(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"fixture PDF")
    batch = _batch(tmp_path / "batch.json", [pdf])
    first_provider = FakeProvider()
    first = DigestProcessor(
        _config(runtime),
        extractor=FakeExtractor(),
        provider=first_provider,
        publisher=FakePublisher(),
        notifier=FakeNotifier(),
        clock=lambda: NOW,
    )
    assert first.run(ProcessRequest(batch_file=batch, summary_only=True)).status == "success"
    assert len(first_provider.requests) == 1

    cached_provider = FakeProvider(fail_if_called=True)
    second = DigestProcessor(
        _config(runtime),
        extractor=FakeExtractor(),
        provider=cached_provider,
        publisher=FakePublisher(),
        notifier=FakeNotifier(),
        clock=lambda: NOW,
    )
    outcome = second.run(ProcessRequest(batch_file=batch, summary_only=True))
    assert outcome.status == "success"
    assert outcome.cache_hits == 1
    assert cached_provider.requests == []


def test_publish_retry_rehydrates_completed_extract_and_summary_without_external_work(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"fixture PDF")
    initial_batch = _batch(tmp_path / "batch.json", [pdf])
    first = DigestProcessor(
        _config(runtime),
        extractor=FakeExtractor(),
        provider=FakeProvider(),
        publisher=FakePublisher(),
        notifier=FakeNotifier(),
        clock=lambda: NOW,
    )
    assert first.run(ProcessRequest(batch_file=initial_batch, summary_only=True)).status == "success"

    extractor = FakeExtractor(fail_if_called=True)
    provider = FakeProvider(fail_if_called=True)
    publisher = FakePublisher()
    retry = DigestProcessor(
        _config(runtime),
        extractor=extractor,
        provider=provider,
        publisher=publisher,
        notifier=FakeNotifier(),
        clock=lambda: NOW,
    )

    # A scanner re-emits a fresh manifest after a publication failure.  The
    # durable extraction artifact, rather than the old compatibility JSON, is
    # what makes this retry safe.
    outcome = retry.run(ProcessRequest(batch_file=_batch(tmp_path / "fresh-batch.json", [pdf])))

    assert outcome.status == "success"
    assert outcome.cache_hits == 1
    assert outcome.published == 1
    assert extractor.calls == 0
    assert provider.requests == []
    assert len(publisher.created) == 1



@pytest.mark.parametrize(("error", "expected_state", "expected_category"), [
    (
        LarkCommandError("docs create", 4, json.dumps({
            "ok": False, "error": {
                "type": "network",
                "message": 'Post "https://accounts.feishu.cn/oauth/v3/token": EOF',
            },
        })),
        StageState.RETRY_WAIT, ErrorCategory.TRANSIENT,
    ),
    (LarkTimeoutError("docs create timed out"), StageState.BLOCKED_RELEASE, ErrorCategory.INVARIANT),
    (
        LarkCommandError("docs create", 3, json.dumps({
            "ok": False, "error": {
                "type": "auth", "message": "User identity is missing; refresh token expired",
            },
        })),
        StageState.RETRY_WAIT, ErrorCategory.AUTH,
    ),
    (LarkCommandError("docs create", 3, "HTTP 401"), StageState.RETRY_WAIT, ErrorCategory.AUTH),
    (LarkCommandError("docs create", 3, "HTTP 403"), StageState.BLOCKED_AUTH, ErrorCategory.AUTH),
])
def test_publication_cli_failure_preserves_summary_and_recovery_policy(
    tmp_path, error, expected_state, expected_category
):
    class BrokenPublisher(FakePublisher):
        def create_document(self, markdown):
            raise error

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"fixture PDF")
    provider = FakeProvider()
    processor = DigestProcessor(
        _config(runtime), extractor=FakeExtractor(), provider=provider,
        publisher=BrokenPublisher(), notifier=FakeNotifier(), clock=lambda: NOW,
    )
    outcome = processor.run(ProcessRequest(batch_file=_batch(tmp_path / "batch.json", [pdf]), no_notify=True))
    assert outcome.status == "partial"
    assert outcome.summarized == 1
    assert outcome.published == 0
    with PipelineState.open(runtime / "state" / "pipeline.sqlite3") as state:
        attempt = state._connection.execute(
            "SELECT state, error_category, available_at_epoch FROM stage_attempts WHERE stage=?",
            (Stage.PUBLISH.value,),
        ).fetchone()
        summary = state._connection.execute(
            "SELECT state FROM stage_attempts WHERE stage=?", (Stage.SUMMARY.value,),
        ).fetchone()
    assert attempt["state"] == expected_state.value
    assert attempt["error_category"] == expected_category.value
    assert (attempt["available_at_epoch"] is not None) is (expected_state is StageState.RETRY_WAIT)
    assert summary["state"] == StageState.SUCCEEDED.value
    assert len(provider.requests) == 1


def test_partial_publish_stage_recovery_completes_only_the_unacknowledged_pdf(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    first_pdf = tmp_path / "first.pdf"
    second_pdf = tmp_path / "second.pdf"
    first_pdf.write_bytes(b"first fixture PDF")
    second_pdf.write_bytes(b"second fixture PDF")
    batch = _batch(tmp_path / "batch.json", [first_pdf, second_pdf])
    publisher = FakePublisher()
    initial = DigestProcessor(
        _config(runtime),
        extractor=FakeExtractor(),
        provider=FakeProvider(),
        publisher=publisher,
        notifier=FakeNotifier(),
        clock=lambda: NOW,
    )
    assert initial.run(ProcessRequest(batch_file=batch)).status == "success"

    with PipelineState.open(runtime / "state" / "pipeline.sqlite3") as state:
        state.migrate()
        document = state.upsert_document(
            "zsxq",
            f"pdf:{sha256_file(second_pdf)}",
            filename=second_pdf.name,
            source_path=str(second_pdf),
        )
        assert state.requeue_succeeded_stage(
            document.id,
            Stage.PUBLISH,
            "publish:daily:new",
            reason="fixture crash after sibling publish acknowledgement",
            now=NOW,
        )

    retry_publisher = FakePublisher()
    retry = DigestProcessor(
        _config(runtime),
        extractor=FakeExtractor(fail_if_called=True),
        provider=FakeProvider(fail_if_called=True),
        publisher=retry_publisher,
        notifier=FakeNotifier(),
        clock=lambda: NOW,
    )
    outcome = retry.run(ProcessRequest(batch_file=_batch(tmp_path / "fresh-batch.json", [first_pdf, second_pdf])))

    assert outcome.status == "success"
    assert retry_publisher.created == []
    assert retry_publisher.appended == []
    with PipelineState.open(runtime / "state" / "pipeline.sqlite3") as state:
        document = state.upsert_document(
            "zsxq",
            f"pdf:{sha256_file(second_pdf)}",
            filename=second_pdf.name,
            source_path=str(second_pdf),
        )
        attempt = state.get_stage_attempt(document.id, Stage.PUBLISH, "publish:daily:new")
        assert attempt is not None
        assert attempt["state"] == StageState.SUCCEEDED.value


def test_successful_lark_publication_retries_only_the_failed_local_projection(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"fixture PDF")
    sidecars = FailingOnceSidecars(tmp_path)
    publisher = FakePublisher()
    first = DigestProcessor(
        _config(runtime),
        extractor=FakeExtractor(),
        provider=FakeProvider(),
        publisher=publisher,
        notifier=FakeNotifier(),
        sidecars=sidecars,
        clock=lambda: NOW,
    )

    first_outcome = first.run(ProcessRequest(batch_file=_batch(tmp_path / "first.json", [pdf])))

    assert first_outcome.status == "partial"
    assert first_outcome.published == 1
    assert len(publisher.created) == 1
    with PipelineState.open(runtime / "state" / "pipeline.sqlite3") as state:
        attempt = state._connection.execute(
            "SELECT state, error_code FROM stage_attempts WHERE stage=?", (Stage.PUBLISH.value,)
        ).fetchone()
        assert attempt is not None
        assert attempt["state"] == StageState.RETRY_WAIT.value
        assert attempt["error_code"] == "local_projection_failed"

    retry_publisher = FakePublisher()
    retry = DigestProcessor(
        _config(runtime),
        extractor=FakeExtractor(fail_if_called=True),
        provider=FakeProvider(fail_if_called=True),
        publisher=retry_publisher,
        notifier=FakeNotifier(),
        sidecars=sidecars,
        clock=lambda: NOW + timedelta(minutes=5),
    )
    retry_outcome = retry.run(ProcessRequest(batch_file=_batch(tmp_path / "retry.json", [pdf])))

    assert retry_outcome.status == "success"
    assert retry_outcome.published == 1
    assert retry_publisher.created == []
    assert retry_publisher.appended == []
    assert len(sidecars.archives) == 2
    with PipelineState.open(runtime / "state" / "pipeline.sqlite3") as state:
        attempt = state._connection.execute(
            "SELECT state, error_code FROM stage_attempts WHERE stage=?", (Stage.PUBLISH.value,)
        ).fetchone()
        assert attempt is not None
        assert attempt["state"] == StageState.SUCCEEDED.value
        assert attempt["error_code"] == ""


def test_extractor_profile_mismatch_blocks_the_release_contract(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"fixture PDF")
    config = replace(_config(runtime), extractor_version="other-profile")
    processor = DigestProcessor(
        config,
        extractor=FakeExtractor(),
        provider=FakeProvider(fail_if_called=True),
        publisher=FakePublisher(),
        notifier=FakeNotifier(),
        clock=lambda: NOW,
    )

    outcome = processor.run(ProcessRequest(batch_file=_batch(tmp_path / "batch.json", [pdf]), summary_only=True))

    assert outcome.status == "failed"
    assert outcome.extracted == 0
    assert any("extractor profile" in failure for failure in outcome.failures)


@pytest.mark.parametrize(
    ("diagnostic", "expected_state", "expected_category"),
    (
        (
            'Codex exited with status 1: {"code":"model_not_found","message":'
            '"The gpt-6.1-sol model is not supported when using Codex with a ChatGPT account."}',
            StageState.BLOCKED_RELEASE,
            ErrorCategory.RELEASE_CONTRACT,
        ),
        (
            "Codex exited with status 1: HTTP 404: model_not_found; input_tokens=1200",
            StageState.BLOCKED_RELEASE,
            ErrorCategory.RELEASE_CONTRACT,
        ),
        (
            "Codex exited with status 1: stream disconnected before completion; tokens used 1200",
            StageState.RETRY_WAIT,
            ErrorCategory.TRANSIENT,
        ),
        (
            "Codex exited with status 1: HTTP 502; input_tokens=3000 output_tokens=200; "
            "report discussed authorization and permission controls",
            StageState.RETRY_WAIT,
            ErrorCategory.TRANSIENT,
        ),
        (
            "Codex exited with status 1: HTTP 401 Unauthorized",
            StageState.RETRY_WAIT,
            ErrorCategory.AUTH,
        ),
        (
            "Codex exited with status 1: HTTP/1.1 403 Forbidden",
            StageState.BLOCKED_AUTH,
            ErrorCategory.AUTH,
        ),
        (
            "Codex exited with status 1: OAuth token refresh failed: refresh_token_reused",
            StageState.RETRY_WAIT,
            ErrorCategory.AUTH,
        ),
    ),
)
def test_summary_execution_failures_preserve_the_correct_recovery_policy(
    tmp_path, diagnostic, expected_state, expected_category
):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"fixture PDF")
    provider = ExecutionErrorProvider(diagnostic)
    processor = DigestProcessor(
        _config(runtime),
        extractor=FakeExtractor(),
        provider=provider,
        publisher=FakePublisher(),
        notifier=FakeNotifier(),
        clock=lambda: NOW,
    )

    outcome = processor.run(ProcessRequest(batch_file=_batch(tmp_path / "batch.json", [pdf]), summary_only=True))

    assert outcome.status == "failed"
    assert len(provider.requests) == 1
    with PipelineState.open(runtime / "state" / "pipeline.sqlite3") as state:
        attempt = state._connection.execute(
            "SELECT state, attempt_count, error_category, error_code, available_at_epoch "
            "FROM stage_attempts WHERE stage=?",
            (Stage.SUMMARY.value,),
        ).fetchone()
    assert attempt is not None
    assert attempt["state"] == expected_state.value
    assert attempt["error_category"] == expected_category.value
    assert attempt["error_code"] == "codex_summary_failed"
    assert attempt["attempt_count"] == 1
    assert (attempt["available_at_epoch"] is not None) is (expected_state is StageState.RETRY_WAIT)


@pytest.mark.parametrize("diagnostic", [
    "stream disconnected before completion",
    "exceeded retry limit, last status: 429 Too Many Requests",
])
def test_temporary_model_outage_survives_restarts_and_recovers_after_capped_cooldowns(tmp_path, diagnostic):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"fixture PDF")
    batch = _batch(tmp_path / "batch.json", [pdf])
    broken = ExecutionErrorProvider(diagnostic)
    minute = 0
    for expected_delay in (5, 10, 20, 40, 60, 60):
        now = NOW + timedelta(minutes=minute)
        processor = DigestProcessor(
            _config(runtime), extractor=FakeExtractor(fail_if_called=minute > 0),
            provider=broken, publisher=FakePublisher(), notifier=FakeNotifier(), clock=lambda: now,
        )
        assert processor.run(ProcessRequest(batch_file=batch, summary_only=True)).status == "failed"
        with PipelineState.open(runtime / "state/pipeline.sqlite3") as state:
            attempt = state._connection.execute(
                "SELECT state,error_category,available_at_epoch FROM stage_attempts WHERE stage='summary'"
            ).fetchone()
            assert attempt["state"] == StageState.RETRY_WAIT.value
            assert attempt["error_category"] == ErrorCategory.TRANSIENT.value
            assert attempt["available_at_epoch"] == int((now + timedelta(minutes=expected_delay)).timestamp())
        calls = len(broken.requests)
        # A fresh worker after a restart must honor the persisted cooldown.
        early = DigestProcessor(
            _config(runtime), extractor=FakeExtractor(fail_if_called=True), provider=broken,
            publisher=FakePublisher(), notifier=FakeNotifier(),
            clock=lambda: now + timedelta(minutes=expected_delay) - timedelta(seconds=1),
        )
        early.run(ProcessRequest(batch_file=batch, summary_only=True))
        assert len(broken.requests) == calls
        minute += expected_delay
    recovered = DigestProcessor(
        _config(runtime), extractor=FakeExtractor(fail_if_called=True), provider=FakeProvider(),
        publisher=FakePublisher(), notifier=FakeNotifier(), clock=lambda: NOW + timedelta(minutes=minute),
    ).run(ProcessRequest(batch_file=batch, summary_only=True))
    assert recovered.status == "success"
    assert recovered.summarized == 1


def test_completed_summary_survives_power_loss_while_another_report_is_running(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    first_pdf = tmp_path / "a.pdf"
    second_pdf = tmp_path / "b.pdf"
    first_pdf.write_bytes(b"first fixture PDF")
    second_pdf.write_bytes(b"second fixture PDF")
    batch = _batch(tmp_path / "batch.json", [first_pdf, second_pdf])
    first_cached = Event()
    observed_cache = []

    class InterruptedProvider(FakeProvider):
        def summarize(self, request):
            if request.inputs[0].path == str(first_pdf):
                return super().summarize(request)
            self.requests.append(request)
            assert first_cached.wait(timeout=3), "the finished report must be cached before sibling jobs finish"
            cached = first.summary_store.load(first_identity, expected_path=str(first_pdf))
            assert cached is not None
            observed_cache.append(cached.entry.markdown)
            raise SystemExit("simulated power loss during the second model request")

    interrupted = InterruptedProvider()
    first = DigestProcessor(
        _config(runtime), extractor=FakeExtractor(), provider=interrupted,
        publisher=FakePublisher(), notifier=FakeNotifier(), clock=lambda: NOW,
    )
    first_identity = SummaryIdentity(
        pdf_sha256=sha256_file(first_pdf), extractor_version=first.config.extractor_version,
        prompt_version=first.prompt_version, model=first.config.codex_model, reasoning=first.config.codex_reasoning,
    )
    persist = first.summary_store.persist

    def observe_cache_commit(identity, entry, **kwargs):
        saved = persist(identity, entry, **kwargs)
        if entry.path == str(first_pdf):
            first_cached.set()
        return saved

    monkeypatch.setattr(first.summary_store, "persist", observe_cache_commit)
    with pytest.raises(SystemExit, match="simulated power loss"):
        first.run(ProcessRequest(batch_file=batch, summary_only=True))

    assert observed_cache
    assert {request.inputs[0].filename for request in interrupted.requests} == {"a.pdf", "b.pdf"}
    output_json, output_markdown = first._summary_output_paths(first_identity)
    assert output_json.is_file() and output_markdown.is_file()
    with PipelineState.open(first.config.database) as state:
        # Neither main-thread stage acknowledgment ran before the power loss.
        attempts = state._connection.execute("SELECT state FROM stage_attempts WHERE stage='summary'").fetchall()
        assert len(attempts) == 2
        assert all(row["state"] == StageState.RUNNING.value for row in attempts)
        assert state._connection.execute("SELECT COUNT(*) FROM artifacts WHERE kind='summary'").fetchone()[0] == 0

    recovered_provider = FakeProvider()
    recovered_extractor = FakeExtractor(fail_if_called=True)
    resumed = DigestProcessor(
        _config(runtime), extractor=recovered_extractor, provider=recovered_provider,
        publisher=FakePublisher(), notifier=FakeNotifier(), clock=lambda: NOW + timedelta(minutes=11),
    ).run(ProcessRequest(batch_file=batch, summary_only=True))

    assert resumed.status == "success"
    assert resumed.summarized == 2
    assert resumed.cache_hits == 1
    assert recovered_extractor.calls == 0
    assert [request.inputs[0].filename for request in recovered_provider.requests] == ["b.pdf"]
    with PipelineState.open(first.config.database) as state:
        attempts = state._connection.execute("SELECT state FROM stage_attempts WHERE stage='summary'").fetchall()
        assert all(row["state"] == StageState.SUCCEEDED.value for row in attempts)


def test_crashed_summary_claim_does_not_exhaust_network_recovery(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"fixture PDF")
    batch = _batch(tmp_path / "batch.json", [pdf])
    processor = DigestProcessor(
        _config(runtime), extractor=FakeExtractor(), provider=TimeoutProvider(),
        publisher=FakePublisher(), notifier=FakeNotifier(), clock=lambda: NOW,
    )
    processor.run(ProcessRequest(batch_file=batch, summary_only=True))
    with PipelineState.open(runtime / "state/pipeline.sqlite3") as state:
        row = state._connection.execute("SELECT workflow_version FROM stage_attempts WHERE stage='summary'").fetchone()
        abandoned = state.claim_due_stage(Stage.SUMMARY, row["workflow_version"], now=NOW + timedelta(minutes=5))
        assert abandoned is not None and abandoned.attempt_count == 2
    # Simulate power loss: the old claim is never completed. After its lease
    # expires, a new worker can fail transiently and still retain recovery.
    now = NOW + timedelta(minutes=11)
    processor = DigestProcessor(
        _config(runtime), extractor=FakeExtractor(fail_if_called=True), provider=TimeoutProvider(),
        publisher=FakePublisher(), notifier=FakeNotifier(), clock=lambda: now,
    )
    processor.run(ProcessRequest(batch_file=batch, summary_only=True))
    with PipelineState.open(runtime / "state/pipeline.sqlite3") as state:
        row = state.get_stage_attempt(abandoned.document_id, Stage.SUMMARY, abandoned.workflow_version)
        assert row["state"] == StageState.RETRY_WAIT.value
        assert row["attempt_count"] == 3
    processor = DigestProcessor(
        _config(runtime), extractor=FakeExtractor(fail_if_called=True), provider=FakeProvider(),
        publisher=FakePublisher(), notifier=FakeNotifier(), clock=lambda: now + timedelta(minutes=20),
    )
    assert processor.run(ProcessRequest(batch_file=batch, summary_only=True)).summarized == 1


def test_reauthorized_lark_publishes_existing_summary_after_persisted_wait(tmp_path):
    class ExpiredPublisher(FakePublisher):
        def create_document(self, markdown):
            raise LarkCommandError("docs create", 3, "User identity is missing; refresh token expired")

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"fixture PDF")
    batch = _batch(tmp_path / "batch.json", [pdf])
    first = DigestProcessor(
        _config(runtime), extractor=FakeExtractor(), provider=FakeProvider(),
        publisher=ExpiredPublisher(), notifier=FakeNotifier(), clock=lambda: NOW,
    ).run(ProcessRequest(batch_file=batch, no_notify=True))
    assert first.status == "partial" and first.summarized == 1
    publisher = FakePublisher()
    fresh = DigestProcessor(
        _config(runtime), extractor=FakeExtractor(fail_if_called=True), provider=FakeProvider(fail_if_called=True),
        publisher=publisher, notifier=FakeNotifier(), clock=lambda: NOW + timedelta(minutes=4),
    )
    fresh.run(ProcessRequest(batch_file=batch, no_notify=True))
    assert publisher.created == []
    after_login = DigestProcessor(
        _config(runtime), extractor=FakeExtractor(fail_if_called=True), provider=FakeProvider(fail_if_called=True),
        publisher=publisher, notifier=FakeNotifier(), clock=lambda: NOW + timedelta(minutes=5),
    ).run(ProcessRequest(batch_file=batch, no_notify=True))
    assert after_login.status == "success" and after_login.published == 1
    assert len(publisher.created) == 1


def test_content_failure_is_quarantined_without_blocking_a_good_pdf(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    good = tmp_path / "good.pdf"
    bad = tmp_path / "bad.pdf"
    good.write_bytes(b"good")
    bad.write_bytes(b"bad")
    processor = DigestProcessor(
        _config(runtime),
        extractor=FakeExtractor(failed_names={bad.name}),
        provider=FakeProvider(),
        publisher=FakePublisher(),
        notifier=FakeNotifier(),
        clock=lambda: NOW,
    )
    outcome = processor.run(ProcessRequest(batch_file=_batch(tmp_path / "batch.json", [good, bad])))
    assert outcome.status == "partial"
    assert outcome.quarantined == 1
    assert outcome.published == 1
    quarantine = json.loads((runtime / "quarantine.json").read_text(encoding="utf-8"))
    assert quarantine["entries"][0]["filename"] == bad.name


def test_later_empty_batch_drains_notification_outbox_without_new_pipeline_work(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"fixture PDF")
    first_notifier = FakeNotifier(error=RuntimeError("temporary notification outage"))
    first = DigestProcessor(
        _config(runtime),
        extractor=FakeExtractor(),
        provider=FakeProvider(),
        publisher=FakePublisher(),
        notifier=first_notifier,
        clock=lambda: NOW,
    )

    first_outcome = first.run(ProcessRequest(batch_file=_batch(tmp_path / "initial.json", [pdf])))

    assert first_outcome.status == "success"
    assert [delivery.status for delivery in first_outcome.notifications] == ["retry_wait"]
    assert len(first_notifier.calls) == 1

    later = NOW + timedelta(minutes=6)
    idle_extractor = FakeExtractor()
    idle_provider = FakeProvider(fail_if_called=True)
    idle_publisher = FakePublisher()
    retry_notifier = FakeNotifier()
    second = DigestProcessor(
        _config(runtime),
        extractor=idle_extractor,
        provider=idle_provider,
        publisher=idle_publisher,
        notifier=retry_notifier,
        clock=lambda: later,
    )

    second_outcome = second.run(ProcessRequest(batch_file=_batch(tmp_path / "empty.json", [])))

    assert second_outcome.status == "success"
    assert [delivery.status for delivery in second_outcome.notifications] == ["sent"]
    assert len(retry_notifier.calls) == 1
    assert idle_extractor.calls == 0
    assert idle_provider.requests == []
    assert idle_publisher.created == []
    assert idle_publisher.appended == []
