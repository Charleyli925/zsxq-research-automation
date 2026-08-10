from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zsxq_pipeline.extract import ExtractionBatchResult, ExtractionItem, sha256_file
from zsxq_pipeline.model import ErrorCategory, Stage, StageState
from zsxq_pipeline.publish import PublicationError
from zsxq_pipeline.process import DigestProcessor, ProcessConfig, ProcessRequest
from zsxq_pipeline.providers.codex import CodexTimeoutError
from zsxq_pipeline.state import PipelineState
from zsxq_pipeline.sidecars import SidecarArchiveResult


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

    def capability_preflight(self):
        return {"ok": True}

    def notify_once(self, chat_id: str, markdown: str, *, idempotency_key: str):
        self.calls.append((chat_id, markdown, idempotency_key))
        if self.error is not None:
            raise self.error
        return object()


class FakeSidecars:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.summary_paths: list[str] = []
        self.archives: list[tuple[tuple[str, ...], str]] = []

    def persist_summary(self, item, persisted):
        destination = self.root / "library" / f"{Path(item['filename']).stem}.summary.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(persisted.entry.markdown, encoding="utf-8")
        self.summary_paths.append(str(item["path"]))
        return destination

    def archive_published_group(self, *, entries, batch_items, document_url):
        self.archives.append((tuple(entry.path for entry in entries), document_url))
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
        watch_state_path=runtime / "watch_state.json",
        result_path=runtime / "last_result.json",
        result_markdown_path=runtime / "last_result.md",
        run_status_path=runtime / "run_status.json",
        usage_path=runtime / "last_usage_summary.json",
        quarantine_path=runtime / "quarantine.json",
        notification_audit_path=runtime / "notification_messages.jsonl",
        research_library_root=None,
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
    assert sidecars.summary_paths == [str(pdf)]
    assert sidecars.archives == [((str(pdf),), next(iter(publisher.documents)))]
    assert json.loads((runtime / "last_result.json").read_text(encoding="utf-8"))["status"] == "success"
    assert "openclaw" not in (runtime / "run_status.json").read_text(encoding="utf-8").lower()


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


def test_transient_summary_failure_waits_once_then_blocks_without_repeated_model_calls(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"fixture PDF")
    provider = TimeoutProvider()

    first = DigestProcessor(
        _config(runtime),
        extractor=FakeExtractor(),
        provider=provider,
        publisher=FakePublisher(),
        notifier=FakeNotifier(),
        clock=lambda: NOW,
    )
    assert first.run(ProcessRequest(batch_file=_batch(tmp_path / "first.json", [pdf]), summary_only=True)).status == "failed"
    assert len(provider.requests) == 1
    with PipelineState.open(runtime / "state" / "pipeline.sqlite3") as state:
        attempt = state._connection.execute(
            "SELECT state, attempt_count FROM stage_attempts WHERE stage=?", (Stage.SUMMARY.value,)
        ).fetchone()
        assert attempt is not None
        assert attempt["state"] == StageState.RETRY_WAIT.value
        assert attempt["attempt_count"] == 1

    cooldown = DigestProcessor(
        _config(runtime),
        extractor=FakeExtractor(fail_if_called=True),
        provider=provider,
        publisher=FakePublisher(),
        notifier=FakeNotifier(),
        clock=lambda: NOW + timedelta(minutes=4),
    )
    cooldown.run(ProcessRequest(batch_file=_batch(tmp_path / "cooldown.json", [pdf]), summary_only=True))
    assert len(provider.requests) == 1

    retry = DigestProcessor(
        _config(runtime),
        extractor=FakeExtractor(fail_if_called=True),
        provider=provider,
        publisher=FakePublisher(),
        notifier=FakeNotifier(),
        clock=lambda: NOW + timedelta(minutes=5),
    )
    assert retry.run(ProcessRequest(batch_file=_batch(tmp_path / "retry.json", [pdf]), summary_only=True)).status == "failed"
    assert len(provider.requests) == 2
    with PipelineState.open(runtime / "state" / "pipeline.sqlite3") as state:
        attempt = state._connection.execute(
            "SELECT state, attempt_count, error_code FROM stage_attempts WHERE stage=?", (Stage.SUMMARY.value,)
        ).fetchone()
        assert attempt is not None
        assert attempt["state"] == StageState.BLOCKED_RELEASE.value
        assert attempt["attempt_count"] == 2
        assert str(attempt["error_code"]).endswith("retry_exhausted")

    later = DigestProcessor(
        _config(runtime),
        extractor=FakeExtractor(fail_if_called=True),
        provider=provider,
        publisher=FakePublisher(),
        notifier=FakeNotifier(),
        clock=lambda: NOW + timedelta(minutes=20),
    )
    later.run(ProcessRequest(batch_file=_batch(tmp_path / "later.json", [pdf]), summary_only=True))
    assert len(provider.requests) == 2


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


def test_scanner_batch_acknowledges_only_when_each_pdf_is_published_or_quarantined(tmp_path, monkeypatch):
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
    scanner_calls: list[bool] = []
    acknowledgements: list[str] = []

    def fake_scan(*, include_existing: bool) -> None:
        scanner_calls.append(include_existing)
        _batch(processor.config.batch_path, [good, bad])

    def fake_ack() -> None:
        acknowledgements.append("ack")

    monkeypatch.setattr(processor, "_scan_batch", fake_scan)
    monkeypatch.setattr(processor, "_ack_scanner_batch", fake_ack)

    outcome = processor.run(ProcessRequest())

    assert outcome.status == "partial"
    assert outcome.quarantined == 1
    assert outcome.published == 1
    assert scanner_calls == [False]
    assert acknowledgements == ["ack"]


def test_scanner_batch_does_not_ack_a_transient_publish_failure(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"fixture PDF")
    processor = DigestProcessor(
        _config(runtime),
        extractor=FakeExtractor(),
        provider=FakeProvider(),
        publisher=FailingPublisher(),
        notifier=FakeNotifier(),
        clock=lambda: NOW,
    )
    acknowledgements: list[str] = []

    def fake_scan(*, include_existing: bool) -> None:
        _batch(processor.config.batch_path, [pdf])

    monkeypatch.setattr(processor, "_scan_batch", fake_scan)
    monkeypatch.setattr(processor, "_ack_scanner_batch", lambda: acknowledgements.append("ack"))

    outcome = processor.run(ProcessRequest())

    assert outcome.status == "partial"
    assert outcome.published == 0
    assert acknowledgements == []


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
