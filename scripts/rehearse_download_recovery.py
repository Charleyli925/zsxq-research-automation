#!/usr/bin/env python3
"""Exercise exact historical download debt on an existing isolated snapshot.

The network transport is replaced by distinct synthetic receipt payloads. The
real downloader recovery, finalizer, manifest reconciliation and SQLite stage
transitions run twice. This validates recovery, not live source availability.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from zsxq_pipeline.download import DownloadPipeline, DownloadRequest, _script_module  # noqa: E402
from zsxq_pipeline.state import PipelineState  # noqa: E402
from zsxq_pipeline.tasks import fingerprint  # noqa: E402
import zsxq_download_receipt as receipts  # noqa: E402


class NoNetworkSession:
    page = None

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def rehearse(snapshot):
    snapshot = Path(snapshot).resolve()
    report = json.loads((snapshot / "report.json").read_text())
    historical = json.loads((snapshot / "historical-plan.json").read_text())
    database = snapshot / "state/pipeline.sqlite3"
    if (
        Path(historical["database"]).resolve() != database
        or report["schema_version"] != 5
    ):
        raise ValueError("only an isolated schema-5 rehearsal snapshot is accepted")
    work = snapshot / "download-rehearsal"
    work.mkdir(exist_ok=False)
    staging, archive = work / "staging", work / "archive"
    staging.mkdir()
    unknown = staging / "unowned-user-file.pdf"
    unknown.write_bytes(b"Unowned fixture; never adopt or move")
    keywords = work / "keywords.json"
    keywords.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "standalone_keywords": [],
                "region_keywords": [],
                "region_required_keywords": [],
                "exclude_keywords": [],
            }
        )
    )
    scanner = _script_module("scan_zsxq_download_candidates")
    completed = []
    with PipelineState.open(database) as state:
        public_before = fingerprint(
            [
                dict(r)
                for r in state._connection.execute(
                    "SELECT * FROM publications ORDER BY id"
                )
            ]
        )
    for group in historical["retry_plans"]:
        for selected in group["candidates"]:
            with PipelineState.open(database) as state:
                row = state._connection.execute(
                    """SELECT p.*,w.source,w.window_start_iso,w.window_end_iso
                    FROM attachment_plans p JOIN source_windows w ON w.id=p.window_id WHERE p.document_id=?""",
                    (selected["document_id"],),
                ).fetchone()
                if row is None:
                    raise ValueError(
                        "retry candidate has no immutable attachment binding"
                    )
                candidate = json.loads(row["candidate_json"])
                original_job = json.loads(row["job_config_json"])
            job = work / "job.json"
            job.write_text(
                json.dumps(
                    {
                        "group_url": original_job["group_url"],
                        "download_settings": {
                            "staging_dir": str(staging),
                            "archive_root": str(archive),
                            "allowed_extensions": [".pdf"],
                        },
                    }
                )
            )
            receipt, value = receipts.prepare(staging, candidate)
            payload = receipt.parent / candidate["filename"]
            payload.write_bytes(
                b"%PDF-1.7\n% OFFLINE REHEARSAL FIXTURE\n"
                + str(selected["document_id"]).encode() * 1024
            )
            # Crash point: payload installed but completion receipt not yet saved.
            plan = {
                "schema_version": 3,
                "window_start": row["window_start_iso"],
                "window_end": row["window_end_iso"],
                "download_candidates": [candidate],
                "download_candidate_count": 1,
                "api_probe_status": "ok",
                "scan_mode": "offline_fixture",
                "blocked_reason": None,
            }
            plan["plan_hash"] = scanner.canonical_plan_hash(plan)
            request = DownloadRequest(
                source=row["source"],
                runtime_root=snapshot,
                database=database,
                job_config_path=job,
                keyword_path=keywords,
                legacy_state_path=work / "legacy-state.json",
                cdp_endpoint="http://127.0.0.1:1",
                window_start=datetime.fromisoformat(row["window_start_iso"]),
                window_end=datetime.fromisoformat(row["window_end_iso"]),
                workflow_version=row["workflow_version"],
                prepared_plan=plan,
                defer_library_projection=True,
            )
            worker = DownloadPipeline(browser_session_factory=NoNetworkSession)
            first = worker.run(request)
            with PipelineState.open(database) as state:
                stage = state.get_stage_attempt(
                    selected["document_id"], "download", row["workflow_version"]
                )
                assert stage["state"] == "succeeded", (
                    selected["document_id"],
                    stage["error_code"],
                    first.status,
                )
                artifact_id = state.get_document(selected["document_id"]).artifact_id
            worker.run(request)
            with PipelineState.open(database) as state:
                assert (
                    state.get_document(selected["document_id"]).artifact_id
                    == artifact_id
                )
            completed.append(selected["document_id"])
            (work / "completed.json").write_text(json.dumps(completed))
    with PipelineState.open(database) as state:
        assert (
            fingerprint(
                [
                    dict(r)
                    for r in state._connection.execute(
                        "SELECT * FROM publications ORDER BY id"
                    )
                ]
            )
            == public_before
        )
        assert state._connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert unknown.read_bytes() == b"Unowned fixture; never adopt or move"
    assert len(list(archive.rglob("*.pdf"))) == len(completed)
    result = {
        "recovered": len(completed),
        "second_passes": len(completed),
        "distinct_archived_files": len(completed),
        "transport": "synthetic PDF receipts, no browser or network",
        "real_components": [
            "receipt recovery",
            "finalizer",
            "manifest reconciliation",
            "SQLite stages",
        ],
        "publication_receipts_unchanged": True,
        "unowned_file_unchanged": True,
    }
    (work / "report.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    print(json.dumps(rehearse(parser.parse_args().snapshot)))
