from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import json
import sqlite3
import subprocess

import pytest

from zsxq_pipeline.config import ConfigError, load_pipeline_config
from zsxq_pipeline.daily_digest import (
    CodexDigestModel, DailyDigest, DailyDigestError, PROPOSAL_SCHEMA,
    atomic_json, digest_hash, inventory, read_json, validate_proposal, verify_publication,
)
from zsxq_pipeline.worker import PipelineWorker


NOW = datetime.fromisoformat("2026-10-17T21:00:00+08:00")  # Saturday is a normal digest day.


def setup(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    config_path = tmp_path / "pipeline.toml"
    config_path.write_text(f'''[runtime]
root = "{root}"
database = "state/pipeline.sqlite3"
[codex]
model = "gpt-6.1-sol"
reasoning = "high"
prompt_version = "test"
[pipeline]
research_library_root = "library"
research_library_database = "state/research.sqlite"
[daily_digest]
enabled = true
''')
    config = load_pipeline_config(config_path)
    database = config.pipeline.research_library_database
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE reports (report_id TEXT,title TEXT,pdf_path TEXT,pdf_sha256 TEXT,"
                           "summary_md_path TEXT,downloaded_at TEXT)")
    return config, config_path


def add_report(config, name="r1", timestamp="2026-10-17T10:00:00+08:00", content=b"PDF fixture"):
    path = config.runtime.root / f"{name}.pdf"
    path.write_bytes(content)
    summary = config.runtime.root / f"{name}.md"
    summary.write_text("Industry evidence summary", encoding="utf-8")
    with sqlite3.connect(config.pipeline.research_library_database) as connection:
        connection.execute("INSERT INTO reports VALUES (?,?,?,?,?,?)",
                           (name, name, str(path), digest_hash(path), str(summary), timestamp))
    return path


def proposal():
    return {"read_report_ids": ["r1"], "excluded": [], "pending": [], "items": [{
        "title": "行业供需变化", "body": "机构预计明年需求同比增长 3%。", "notes": "2027 年预测。",
        "sources": [{"report_id": "r1", "institution": "Research", "report_title": "Industry",
                     "report_date": "2026-10-16", "pages": [2]}],
        "charts": [{"report_id": "r1", "page": 2, "bbox": [0.1, 0.2, 0.9, 0.8], "caption": "需求预测"}],
    }]}


def render(pdf, page, output, bbox=None):
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(f"{pdf.name}/{page}/{bbox}".encode())
    return output


class Model:
    def __init__(self):
        self.calls = []

    def __call__(self, work, name, prompt, schema, images=()):
        self.calls.append((name, images))
        if name == "research":
            assert (work / "manifest.json").is_file()
            return proposal()
        assert len(images) == 3  # cover, evidence page, chart crop
        return {"accepted": True, "reason": "All claims and complete chart verified."}


class Publisher:
    def __init__(self):
        self.calls = []
        self.body = ""
        self.fail_after_write = False

    def write(self, work, op, document):
        self.calls.append(op)
        if op["kind"] == "create":
            self.body = f"<title>{op['title']}</title>"
            result = {"document_id": "doc123", "url": "https://example.feishu.cn/docx/doc123"}
        elif op["kind"] == "image":
            assert (work / op["file"]).is_file()
            self.body += '<img src="image123"/>'
            result = {"file_token": "image123"}
        else:
            self.body += op["xml"]
            result = {"ok": True}
        if self.fail_after_write:
            raise TimeoutError("response lost after remote write")
        return result

    def fetch(self, work, document):
        return self.body


def advance(digest, status, now=NOW):
    for _ in range(20):
        result = digest.step(now)
        if result["status"] == status:
            return result
    raise AssertionError(f"did not reach {status}: {result}")


def test_inventory_timezone_inclusive_boundary_duplicate_hash_and_read_only(tmp_path):
    config, _ = setup(tmp_path)
    add_report(config, timestamp="2026-10-16T16:00:00Z")
    add_report(config, "duplicate", timestamp="2026-10-17T01:00:00+08:00")
    add_report(config, "older", timestamp="2026-10-16T15:59:59Z", content=b"old")
    add_report(config, "future", timestamp="2026-10-18T00:00:00+08:00", content=b"future")
    before = digest_hash(config.pipeline.research_library_database)
    rows = inventory(config, "2026-10-17T00:00:00+08:00", NOW.isoformat(), set())
    assert [r["report_id"] for r in rows] == ["r1"]
    assert inventory(config, "2026-10-17T00:00:00+08:00", NOW.isoformat(), {rows[0]["pdf_sha256"]}) == []
    assert digest_hash(config.pipeline.research_library_database) == before


def test_weekend_resumable_publish_and_same_day_replay_do_not_duplicate(tmp_path):
    config, _ = setup(tmp_path)
    add_report(config)
    model, publisher = Model(), Publisher()
    digest = DailyDigest(config, model=model, publisher=publisher, renderer=render)
    assert digest.step(NOW - timedelta(minutes=1))["status"] == "idle"
    assert digest.step(NOW)["status"] == "research"
    # A new process resumes the saved snapshot, rather than rerunning its model.
    digest = DailyDigest(config, model=model, publisher=publisher, renderer=render)
    result = advance(digest, "complete")
    assert result["document"] == "doc123"
    assert [name for name, _ in model.calls] == ["research", "verify-0"]
    assert [op["kind"] for op in publisher.calls] == ["create", "text", "image", "text"]
    assert digest.step(NOW)["status"] == "idle"
    assert digest.step(NOW + timedelta(days=1))["status"] == "empty"
    state = read_json(digest.root / "state.json")
    assert len(state["seen"]) == 1
    assert "为什么值得读" not in publisher.body
    assert "阅读记录与来源" not in publisher.body


@pytest.mark.parametrize("mutation", [
    lambda p: p["read_report_ids"].clear(),
    lambda p: p["excluded"].append({"report_id": "r1", "reason": "contradiction"}),
    lambda p: p["items"][0]["charts"][0].update(report_id="invented"),
    lambda p: p["items"][0]["charts"][0].update(page=0),
    lambda p: p["items"][0]["charts"][0].update(bbox=[0, 0, 1.1, 1]),
    lambda p: p["items"][0]["charts"][0].update(bbox=[0, 0, float("nan"), 1]),
    lambda p: p["items"][0].update(body="为什么值得读：..."),
    lambda p: p["items"][0].update(charts=[]),
])
def test_contract_rejects_unread_or_invented_sources_and_invalid_crops(mutation):
    value = proposal()
    mutation(value)
    with pytest.raises(DailyDigestError):
        validate_proposal(value, [{"report_id": "r1"}])


def test_unknown_create_outcome_stops_instead_of_creating_another_document(tmp_path):
    config, _ = setup(tmp_path)
    add_report(config)
    publisher = Publisher()
    digest = DailyDigest(config, model=Model(), publisher=publisher, renderer=render)
    advance(digest, "publish")
    publisher.fail_after_write = True
    with pytest.raises(TimeoutError):
        digest.step(NOW)
    assert digest.step(NOW + timedelta(hours=1))["status"] == "needs_reconciliation"
    assert len(publisher.calls) == 1
    assert read_json(digest.root / "state.json")["seen"] == []
    publisher.fail_after_write = False
    receipt = {"document_id": "doc123"}
    assert not digest.reconcile(day="2026-10-17", index=0, receipt=receipt,
                                confirmed_not_applied=False, reason="inspected existing title", apply=False)["applied"]
    assert digest.step(NOW)["status"] == "needs_reconciliation"
    digest.reconcile(day="2026-10-17", index=0, receipt=receipt,
                     confirmed_not_applied=False, reason="inspected existing title", apply=True)
    advance(digest, "complete")
    assert len([op for op in publisher.calls if op["kind"] == "create"]) == 1


def test_reconciliation_rejects_a_receipt_for_the_wrong_document_body(tmp_path):
    config, _ = setup(tmp_path)
    add_report(config)
    publisher = Publisher()
    digest = DailyDigest(config, model=Model(), publisher=publisher, renderer=render)
    advance(digest, "publish")
    publisher.fail_after_write = True
    with pytest.raises(TimeoutError):
        digest.step(NOW)
    publisher.body = "<title>Unrelated document</title>"
    with pytest.raises(DailyDigestError):
        digest.reconcile(day="2026-10-17", index=0, receipt={"document_id": "other"},
                         confirmed_not_applied=False, reason="wrong document", apply=True)
    assert digest.step(NOW)["status"] == "needs_reconciliation"


def test_failed_model_call_does_not_switch_model_or_advance_processed_state(tmp_path):
    config, _ = setup(tmp_path)
    add_report(config)
    attempts = []

    def unavailable(*args):
        attempts.append(config.daily_digest.model)
        raise DailyDigestError("model not supported with these credentials")

    digest = DailyDigest(config, model=unavailable, publisher=Publisher(), renderer=render)
    assert digest.step(NOW)["status"] == "research"
    with pytest.raises(DailyDigestError):
        digest.step(NOW)
    assert digest.step(NOW)["status"] == "retry_wait"
    assert attempts == ["gpt-6.1-sol"]
    assert read_json(digest.root / "state.json")["seen"] == []


def test_missing_pdf_stays_pending_and_is_retried_next_day(tmp_path):
    config, _ = setup(tmp_path)
    pdf = add_report(config)
    original = pdf.read_bytes()
    pdf.unlink()
    digest = DailyDigest(config, model=Model(), publisher=Publisher(), renderer=render)
    result = advance(digest, "complete")
    assert result["pending"] == 1
    assert read_json(digest.root / "state.json")["seen"] == []
    pdf.write_bytes(original)
    assert digest.step(NOW + timedelta(days=1))["status"] == "research"
    assert read_json(digest.root / "2026-10-18" / "manifest.json")[0]["report_id"] == "r1"


def test_visual_rejection_never_publishes_and_returns_to_research(tmp_path):
    config, _ = setup(tmp_path)
    add_report(config)
    publisher = Publisher()

    def reject(work, name, prompt, schema, images=()):
        return proposal() if name == "research" else {"accepted": False, "reason": "legend clipped"}

    digest = DailyDigest(config, model=reject, publisher=publisher, renderer=render)
    advance(digest, "verify")
    with pytest.raises(DailyDigestError, match="visual evidence"):
        digest.step(NOW)
    run = read_json(digest.root / "2026-10-17" / "run.json")
    assert run["phase"] == "research"
    assert run["verification_failure"]["reason"] == "legend clipped"
    assert not publisher.calls


def test_verified_image_tampering_blocks_publication(tmp_path):
    config, _ = setup(tmp_path)
    add_report(config)
    publisher = Publisher()
    digest = DailyDigest(config, model=Model(), publisher=publisher, renderer=render)
    advance(digest, "publish")
    digest.step(NOW)  # create
    digest.step(NOW)  # text
    (digest.root / "2026-10-17/charts/0-0.png").write_bytes(b"modified")
    with pytest.raises(DailyDigestError, match="changed after visual"):
        digest.step(NOW)
    assert len(publisher.calls) == 2


def test_publication_checks_image_location_and_complete_text():
    ops = [{"kind": "create", "title": "Digest"}, {"kind": "text", "xml": "<p>Evidence</p>"},
           {"kind": "image"}, {"kind": "text", "xml": "<p>Source</p>"}]
    receipts = [{}, {}, {"file_token": "image123"}, {}]
    verify_publication('<title>Digest</title><p>Evidence</p><img src="image123"/><p>Source</p>', ops, receipts)
    with pytest.raises(DailyDigestError):
        verify_publication('<title>Digest</title><img src="image123"/><p>Evidence</p><p>Source</p>', ops, receipts)


def test_codex_call_pins_model_and_effort_and_passes_images_without_shell(tmp_path, monkeypatch):
    config, _ = setup(tmp_path)
    work = tmp_path / "run"
    work.mkdir()
    image = work / "page.png"
    image.write_bytes(b"image")
    captured = {}

    class Process:
        returncode = 0

        def __init__(self, command, **kwargs):
            captured.update(command=command, **kwargs)
            self.result = Path(command[command.index("--output-last-message") + 1])

        def communicate(self, prompt, timeout):
            self.result.write_text(json.dumps(proposal()))

    monkeypatch.setattr(subprocess, "Popen", Process)
    result = CodexDigestModel(config)(work, "check", "prompt", PROPOSAL_SCHEMA, (image,))
    assert result == proposal()
    command = captured["command"]
    assert command[command.index("--model") + 1] == "gpt-6.1-sol"
    assert 'model_reasoning_effort="high"' in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--image") + 1] == str(image)
    assert "shell" not in captured
    assert captured["cwd"] == work / "model" / "check"


def test_seed_skips_existing_first_edition(tmp_path):
    config, _ = setup(tmp_path)
    pdf = add_report(config)
    seed = config.runtime.root / "seed.json"
    atomic_json(seed, {"last_successful_downloaded_at": "2026-10-17T10:00:00+08:00",
                       "screened_pdf_sha256": [digest_hash(pdf)], "pending_report_ids": [],
                       "runs": [{"date": "2026-10-17", "document_url": "https://example.feishu.cn/docx/existing"}]})
    config = replace(config, daily_digest=replace(config.daily_digest, seed_state=seed))
    digest = DailyDigest(config)
    assert digest.step(NOW)["status"] == "idle"
    assert digest.step(NOW + timedelta(days=1))["status"] == "empty"


def test_config_rejects_invalid_time_and_escape(tmp_path):
    config, path = setup(tmp_path)
    original = path.read_text()
    for setting in ('at = "25:00"', 'rules_path = "../outside.json"', 'enabled = "true"'):
        path.write_text(original.replace("enabled = true", setting))
        with pytest.raises(ConfigError):
            load_pipeline_config(path)
    assert config.daily_digest.model == "gpt-6.1-sol"


def test_existing_worker_advances_digest_and_exposes_attention(tmp_path):
    config, _ = setup(tmp_path)
    states = iter([{"status": "research"}, {"status": "needs_reconciliation", "operation": 0}])
    worker = PipelineWorker(config, clock=lambda: NOW, daily_digest_runner=lambda now: next(states))
    result = worker.run_stage("daily-digest")
    assert result.status == "partial"
    assert result.daily_digest["status"] == "needs_reconciliation"
    assert "daily-digest:needs_reconciliation" in result.failures


def test_new_defaults_do_not_enable_daily_publication(tmp_path):
    _, path = setup(tmp_path)
    path.write_text(path.read_text().split("[daily_digest]")[0])
    config = load_pipeline_config(path)
    assert not config.daily_digest.enabled
    assert DailyDigest(config).step(NOW) == {"status": "disabled"}


def test_additional_unaccounted_input_is_rejected():
    with pytest.raises(DailyDigestError, match="every input"):
        validate_proposal(deepcopy(proposal()), [{"report_id": "r1"}, {"report_id": "unread"}])
