from __future__ import annotations

import json
import os
import uuid
from argparse import Namespace

import pytest

from scripts import zsxq_download_receipt as receipts
from test_download_zsxq_plan_file import MODULE as downloader
from test_finalize_download_batch import MODULE as finalizer, write_run_fixture


@pytest.mark.parametrize("corrupt_first", [False, True])
def test_receipt_recovers_atomic_download_without_browser_or_run_mtime(tmp_path, corrupt_first):
    staging, archive, config, keywords, state, plan_file = write_run_fixture(tmp_path, ["报告.pdf", "报告.pdf"])
    plan = json.loads(plan_file.read_text())
    for index, item in enumerate(plan["download_candidates"]):
        item["topic_url"] = f"https://example.invalid/topic/{index}"
        receipt, value = receipts.prepare(staging, item)
        payload = receipt.parent / item["filename"]
        payload.write_bytes(b"%PDF-1.7\n" + bytes([65 + index]) * 2048)
        os.utime(payload, (1, 1))
        # Simulate process death after validated rename, before receipt commit.
        result = downloader.download_candidate_on_page(item, page=None, staging_dir=staging, timeout_ms=1)
        assert result["reason_code"] == "download_recovered"
        if corrupt_first and index == 0:
            payload.write_bytes(b"%PDF-1.7\n" + b"corrupted" * 300)
    plan_file.write_text(json.dumps(plan))
    unknown = staging / "报告.pdf"
    unknown.write_bytes(b"user-owned-unrelated-file")
    paths = None
    for _ in range(2):
        args = Namespace(config=str(config), keywords=str(keywords), state=str(state),
                         window_start=plan["window_start"], window_end=plan["window_end"],
                         downloaded_after="2030-01-01T00:00:00+00:00", downloaded_before=None,
                         run_id=str(uuid.uuid4()), scan_plan=str(plan_file), run_manifest=str(tmp_path / f"{uuid.uuid4()}.json"),
                         skip_state_update=True, commit_state=False, wait_seconds=0,
                         wait_interval_seconds=1, dry_run=False)
        summary, code = finalizer.run_finalization(args)
        manifest = json.loads(open(args.run_manifest).read())
        assert code == 0 and summary["state_commit_eligible"] is (not corrupt_first)
        entries = manifest["downloaded_entries"]
        assert len(entries) == 2 - int(corrupt_first)
        actual = {entry["archive_path"] for entry in entries}
        assert len(actual) == 2 - int(corrupt_first)
        if paths is not None:
            assert actual == paths  # Crash after archive, before DB acknowledgement.
        paths = actual
    assert unknown.read_bytes() == b"user-owned-unrelated-file"
    assert len(list(archive.rglob("*.pdf"))) == 2 - int(corrupt_first)


def test_receipt_never_adopts_corruption_or_different_identity(tmp_path):
    item = {"file_id": "one", "filename": "same.pdf", "topic_url": "https://example.invalid/1"}
    receipt, value = receipts.prepare(tmp_path, item)
    payload = receipt.parent / item["filename"]
    payload.write_bytes(b"%PDF-1.7\n" + b"a" * 2048)
    assert receipts.completed_path(receipt, value) == payload
    payload.write_bytes(b"%PDF-1.7\n" + b"b" * 2048)
    with pytest.raises(ValueError, match="content mismatch"):
        receipts.completed_path(receipt, value)
    with pytest.raises(ValueError, match="identity mismatch"):
        receipts.load(receipt, {**item, "file_id": "two"})


def test_archive_crash_after_destination_intent_resumes_without_overwrite(tmp_path):
    item = {"file_id": "one", "filename": "same.pdf", "topic_url": "https://example.invalid/1"}
    receipt, value = receipts.prepare(tmp_path / "staging", item)
    payload = receipt.parent / item["filename"]
    payload.write_bytes(b"%PDF-1.7\n" + b"a" * 2048)
    receipts.completed_path(receipt, value)
    destination = tmp_path / "archive" / "same.pdf"
    value["archive_path"] = str(destination)
    receipts.write(receipt, value)
    assert receipts.archive(receipt, value, payload, destination) == destination
    assert receipts.completed_path(receipt, value) == destination
    destination.write_bytes(b"unrelated")
    with pytest.raises(ValueError, match="content mismatch"):
        receipts.completed_path(receipt, value)
