#!/usr/bin/env python3
"""Offline production-copy migration and recovery rehearsal. Never starts workers.

SQLite backups use read-only source connections. All repaired files and both
writable databases live under a new destination. Browser, model and Lark
commands are disabled. Original PDFs/notes are copied before any repair.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from zsxq_pipeline.config import load_pipeline_config  # noqa: E402
from zsxq_pipeline.discovery import import_saved_discovery, schedule_discovery, coverage  # noqa: E402
from zsxq_pipeline.recovery import inventory  # noqa: E402
from zsxq_pipeline.service import _enqueue_due_documents  # noqa: E402
from zsxq_pipeline.state import PipelineState  # noqa: E402
from zsxq_pipeline.tasks import TaskQueue, fingerprint  # noqa: E402
import repair_summary_projections as repair  # noqa: E402
from datetime import datetime, timezone  # noqa: E402


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def backup(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src,
        sqlite3.connect(destination) as dst,
    ):
        src.backup(dst)


def clone_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return
    if sys.platform == "darwin":
        result = subprocess.run(
            ["/bin/cp", "-c", str(source), str(destination)], capture_output=True
        )
        if result.returncode == 0:
            return
    shutil.copy2(source, destination)


def copy_config(source, dest):
    def local_file(path):
        if not path:
            return None
        target = (
            dest / "inputs" / hashlib.sha256(str(path).encode()).hexdigest() / path.name
        )
        if path.is_file():
            clone_file(path, target)
        return target

    sources = {
        name: replace(
            item,
            state_path=local_file(item.state_path),
            job_config_path=local_file(item.job_config_path),
            keyword_path=local_file(item.keyword_path),
            cdp_endpoint="http://127.0.0.1:1",
            cft_executable_path=None,
            cft_user_data_dir=None,
        )
        for name, item in source.sources.items()
    }
    return replace(
        source,
        runtime=replace(
            source.runtime, root=dest, database=dest / "state/pipeline.sqlite3"
        ),
        sources=sources,
        codex=replace(
            source.codex,
            command="/usr/bin/false",
            work_root=dest / "work/codex",
            prompt_path=local_file(source.codex.prompt_path),
            system_prompt_path=local_file(source.codex.system_prompt_path),
            output_schema_path=local_file(source.codex.output_schema_path),
            routing_config_path=None,
        ),
        lark=replace(
            source.lark,
            command="/usr/bin/false",
            config_dir=dest / "disabled-lark",
            notifications_enabled=False,
            target_chat_id="",
        ),
        pipeline=replace(
            source.pipeline,
            research_library_root=dest / "library",
            research_library_database=dest / "state/catalog.sqlite",
            obsidian_vault_root=dest / "vault",
        ),
        legacy=replace(source.legacy, root=None),
        daily_digest=replace(source.daily_digest, enabled=False),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--destination",
        required=True,
        help="New empty directory outside production and repository",
    )
    parser.add_argument(
        "--repair-projections",
        action="store_true",
        help="Exercise every verified projection candidate on the copy",
    )
    args = parser.parse_args()
    source = load_pipeline_config(args.config)
    dest = Path(args.destination).expanduser().resolve()
    if (
        dest.exists()
        or dest.is_relative_to(source.runtime.root)
        or dest.is_relative_to(ROOT)
    ):
        raise ValueError("destination must be new and outside production/repository")
    dest.mkdir(parents=True)
    start = time.monotonic()
    config = copy_config(source, dest)
    backup(source.runtime.database, config.runtime.database)
    backup(
        source.pipeline.research_library_database,
        config.pipeline.research_library_database,
    )
    for name in ("plans", "summary_cache"):
        shutil.copytree(source.runtime.root / name, dest / name)
    # Relocate only cache storage addresses; accepted identity and Markdown
    # hashes are unchanged. Never rewrite original cache files.
    relocated = 0
    for path in (dest / "summary_cache/json").glob("*.json"):
        cached = json.loads(path.read_text())
        cached["markdown_path"] = str(
            dest / "summary_cache/markdown" / (path.stem + ".md")
        )
        write(path, cached)
        relocated += 1
    # Read-only inventory before migration, tied to this snapshot's exact rows.
    plan = inventory(config)
    write(dest / "historical-plan.json", plan)
    with PipelineState.open(config.runtime.database) as state:
        before = {
            name: state._connection.execute("SELECT COUNT(*) FROM " + name).fetchone()[
                0
            ]
            for name in (
                "documents",
                "stage_attempts",
                "publications",
                "notification_outbox",
            )
        }
        public_before = fingerprint(
            [
                dict(r)
                for r in state._connection.execute(
                    "SELECT * FROM publications ORDER BY id"
                )
            ]
        )
        state.migrate()
        imported = import_saved_discovery(state, config)
        repeated = import_saved_discovery(state, config)
        assert repeated == imported
        requeued = 0
        for retry in plan["retry_plans"]:
            requeued += state.apply_stage_retry_plan(
                retry["candidates"],
                plan_hash=retry["plan_hash"],
                expected_count=retry["expected_count"],
            )
        schedule_discovery(state, config, datetime.now(timezone.utc))
        _enqueue_due_documents(state, config)
        q = TaskQueue(state)
        due = q.due()
        after = {
            name: state._connection.execute("SELECT COUNT(*) FROM " + name).fetchone()[
                0
            ]
            for name in before
        }
        assert before == after, (before, after)
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
        report = {
            "schema_version": state.schema_version,
            "before": before,
            "after": after,
            "import": imported,
            "terminal_classification": plan["classification_counts"],
            "exact_download_requeues": requeued,
            "due_task_resources": dict(Counter(t["resource"] for t in due)),
            "due_task_sources": dict(Counter(t["source"] for t in due)),
            "coverage": {
                name: coverage(state, name).isoformat()
                if coverage(state, name)
                else None
                for name in config.sources
            },
            "relocated_cache_addresses": relocated,
            "publication_receipts_unchanged": True,
            "network_calls": 0,
            "model_calls": 0,
            "lark_calls": 0,
        }
    write(dest / "report.json", report)
    print(json.dumps({"phase": "migration_and_retry", "result": report}), flush=True)
    # Build against the snapshot catalog and copied cache, with source files
    # still read-only. Copy all verified evidence then remap only those rows.
    projection = repair.build_plan(config)
    write(dest / "projection-source-plan.json", projection)
    with sqlite3.connect(config.pipeline.research_library_database) as db:
        for candidate in projection["candidates"]:
            report_id = candidate["item"]["report_id"]
            old = db.execute(
                "SELECT pdf_path,summary_md_path,obsidian_note_path FROM reports WHERE report_id=?",
                (report_id,),
            ).fetchone()
            paths = []
            for index, value in enumerate(old):
                if not value:
                    paths.append("")
                    continue
                target = dest / "evidence" / report_id / str(index) / Path(value).name
                clone_file(Path(value), target)
                paths.append(str(target))
            db.execute(
                "UPDATE reports SET pdf_path=?,summary_md_path=?,obsidian_note_path=? WHERE report_id=?",
                (*paths, report_id),
            )
    projection = repair.build_plan(config)
    write(dest / "projection-copy-plan.json", projection)
    assert all(
        Path(c["item"]["path"]).is_relative_to(dest)
        and (not c["note_path"] or Path(c["note_path"]).is_relative_to(dest))
        for c in projection["candidates"]
    )
    report["projection_candidates"] = projection["expected_count"]
    report["projection_excluded"] = projection["excluded"]
    write(dest / "report.json", report)
    print(
        json.dumps(
            {
                "phase": "projection_evidence_copied",
                "candidates": projection["expected_count"],
                "excluded": projection["excluded"],
            }
        ),
        flush=True,
    )
    if args.repair_projections:
        result = repair.apply_plan(config, projection, projection["expected_count"])
        report["projection_repair"] = result
        report["projection_remaining"] = repair.build_plan(config)["expected_count"]
        assert report["projection_remaining"] == 0
    with sqlite3.connect(config.runtime.database) as db:
        db.row_factory = sqlite3.Row
        assert (
            fingerprint(
                [dict(r) for r in db.execute("SELECT * FROM publications ORDER BY id")]
            )
            == public_before
        )
    report["elapsed_seconds"] = round(time.monotonic() - start, 2)
    write(dest / "report.json", report)
    print(json.dumps({"phase": "complete", "report": report}), flush=True)


if __name__ == "__main__":
    main()
