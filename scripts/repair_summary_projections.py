#!/usr/bin/env python3
"""Plan/apply an exact local projection repair using verified summary caches.

Never runs a model, publishes a document, or sends a notification. The plan
requires unchanged accepted summary bytes, a matching PDF, and a valid cache.
Uncertain source changes remain excluded for separate inspection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zsxq_pipeline.config import load_pipeline_config  # noqa: E402
from zsxq_pipeline.lock import runtime_lock  # noqa: E402
from zsxq_pipeline.model import SummaryIdentity  # noqa: E402
from zsxq_pipeline.publish import SummaryForPublish  # noqa: E402
from zsxq_pipeline.sidecars import ArtifactSidecars  # noqa: E402
from zsxq_pipeline.summary import SummaryStore  # noqa: E402
try:
    from zsxq_download_receipt import write as atomic_json
except ModuleNotFoundError:
    from scripts.zsxq_download_receipt import write as atomic_json


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_hash(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def pending_path(config):
    return config.runtime.root / "state" / "projection-repair-pending.json"


def pending_ids(config):
    path = pending_path(config)
    return set(json.loads(path.read_text())["report_ids"]) if path.is_file() else set()


@contextmanager
def read_only(path):
    conn = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def build_plan(config):
    database = config.pipeline.research_library_database
    store = SummaryStore(config.runtime.root / "summary_cache")
    pending = sorted(pending_ids(config)) or [""]
    with read_only(database) as catalog, read_only(config.runtime.database) as pipeline:
        rows = catalog.execute("""SELECT r.*, e.content_sha256 AS accepted_sha256,
            e.identity_sha256 AS accepted_identity FROM reports r
            JOIN report_evidence_state e USING(report_id)
            WHERE (e.status='source_changed' OR r.report_id IN (""" + ",".join("?" * len(pending)) + "))"
            + " AND e.source_kind='summary' ORDER BY r.report_id", pending).fetchall()
        artifacts = {}
        for artifact in pipeline.execute("SELECT * FROM artifacts WHERE kind='summary' ORDER BY id DESC"):
            artifacts.setdefault(artifact["pdf_sha256"], []).append(dict(artifact))
        candidates = []
        excluded = Counter()
        for raw in rows:
            row = dict(raw)
            try:
                summary = Path(row["summary_md_path"])
                if file_hash(summary) != row["accepted_sha256"]:
                    raise ValueError("accepted_summary_bytes_changed")
                if file_hash(row["pdf_path"]) != row["pdf_sha256"]:
                    raise ValueError("pdf_identity_mismatch")
                found = None
                for artifact in artifacts.get(row["pdf_sha256"], []):
                    identity = SummaryIdentity(*(artifact[key] for key in (
                        "pdf_sha256", "extractor_version", "prompt_version", "model", "reasoning")))
                    cached = store.load(identity)
                    if cached is not None and cached.entry.markdown.strip() == summary.read_text().strip():
                        found = cached
                        break
                if found is None:
                    raise ValueError("matching_committed_summary_missing")
                item = {
                    "report_id": row["report_id"], "path": row["pdf_path"],
                    "filename": Path(row["pdf_path"]).name, "pdf_sha256": row["pdf_sha256"],
                    "batch_id": row["batch_id"], "title": found.entry.title,
                    "feishu_doc_url": row["feishu_doc_url"],
                }
                candidates.append({
                    "item": item, "identity": json.loads(found.identity.canonical_json),
                    "catalog_fingerprint": fingerprint(row), "accepted_sha256": row["accepted_sha256"],
                    "note_path": row["obsidian_note_path"],
                    "note_sha256": file_hash(row["obsidian_note_path"]) if row["obsidian_note_path"] else "",
                })
            except (OSError, ValueError) as exc:
                excluded[str(exc) if isinstance(exc, ValueError) else type(exc).__name__] += 1
    plan = {"schema_version": 1, "runtime_root": str(config.runtime.root), "database": str(database),
            "candidates": candidates, "expected_count": len(candidates), "excluded": dict(excluded)}
    plan["plan_hash"] = fingerprint(plan)
    return plan


def apply_plan(config, plan, expected_count):
    unsigned = {key: value for key, value in plan.items() if key != "plan_hash"}
    if fingerprint(unsigned) != plan.get("plan_hash") or expected_count != plan.get("expected_count"):
        raise ValueError("plan hash or expected count mismatch")
    with runtime_lock(config.runtime.root) as acquired:
        if not acquired:
            raise ValueError("runtime is busy")
        if build_plan(config) != plan:
            raise ValueError("repair evidence changed; create and review a fresh plan")
        backup = config.runtime.root / "backups" / ("projection-repair-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        backup.mkdir(parents=True)
        with read_only(config.pipeline.research_library_database) as source, sqlite3.connect(backup / "catalog.sqlite") as target:
            source.backup(target)
        (backup / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2))
        sidecars = ArtifactSidecars(
            library_root=config.pipeline.research_library_root,
            library_database=config.pipeline.research_library_database,
            vault_root=config.pipeline.obsidian_vault_root, work_root=config.runtime.root / "work",
        )
        store = SummaryStore(config.runtime.root / "summary_cache")
        pending = pending_ids(config) | {candidate["item"]["report_id"] for candidate in plan["candidates"]}
        atomic_json(pending_path(config), {"report_ids": sorted(pending)})
        completed = []
        for candidate in plan["candidates"]:
            item = dict(candidate["item"])
            if candidate["note_path"]:
                shutil.copy2(candidate["note_path"], backup / (item["report_id"] + ".md"))
            cached = store.load(SummaryIdentity(**candidate["identity"]))
            if cached is None:
                raise ValueError("summary cache disappeared")
            item["summary_md_path"] = str(sidecars.persist_summary(item, cached))
            entry = SummaryForPublish(cached.markdown_sha256, cached.entry.markdown,
                                      item["batch_id"], item["path"], item["filename"], title=cached.entry.title)
            sidecars.archive_published_group(entries=(entry,), batch_items={item["path"]: item},
                                            document_url=item["feishu_doc_url"])
            with read_only(config.pipeline.research_library_database) as conn:
                status = conn.execute("SELECT status FROM report_evidence_state WHERE report_id=?", (item["report_id"],)).fetchone()
                if status is None or status[0] != "ready":
                    raise ValueError("projection did not restore verified index state")
            completed.append(item["report_id"])
            (backup / "completed.json").write_text(json.dumps(completed))
            pending.remove(item["report_id"])
            atomic_json(pending_path(config), {"report_ids": sorted(pending)})
    return {"repaired": len(completed), "backup": str(backup)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "apply"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    config = load_pipeline_config(args.config)
    if args.command == "plan":
        plan = build_plan(config)
        Path(args.plan).write_text(json.dumps(plan, ensure_ascii=False, indent=2))
        print(json.dumps({key: value for key, value in plan.items() if key != "candidates"}))
    else:
        if not args.apply:
            parser.error("apply requires --apply and a reviewed --expected-count")
        print(json.dumps(apply_plan(config, json.loads(Path(args.plan).read_text()), args.expected_count)))


if __name__ == "__main__":
    main()
