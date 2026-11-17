"""Read-only, exhaustive historical inventory; never blanket-requeue failures."""

from __future__ import annotations

from collections import Counter, defaultdict
import json
import sqlite3

from .download import _script_module
from .state import _stage_retry_fingerprint
from .tasks import fingerprint, diagnostic


def inventory(config):
    scanner = _script_module("scan_zsxq_download_candidates")
    evidence = {}
    invalid = 0
    for source in config.sources.values():
        for path in sorted(
            (config.runtime.root / "plans" / source.name).glob("*.json")
        ):
            try:
                plan = json.loads(path.read_text())
                if plan.get("blocked_reason") or plan.get("api_probe_status") != "ok":
                    continue
                if plan.get("plan_hash") != scanner.canonical_plan_hash(plan):
                    invalid += 1
                    continue
                for candidate in plan.get("download_candidates", []):
                    if (
                        candidate.get("file_id")
                        and candidate.get("topic_url")
                        and candidate.get("filename")
                    ):
                        evidence[(source.name, str(candidate["file_id"]))] = {
                            "plan_hash": plan["plan_hash"],
                            "candidate_sha256": fingerprint(candidate),
                            "plan_path": str(path),
                        }
            except (OSError, ValueError, TypeError):
                invalid += 1
    with sqlite3.connect(config.runtime.database.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("""SELECT s.*,d.source,d.source_file_id,d.artifact_id,
            (SELECT MAX(l.id) FROM stage_attempts l WHERE l.document_id=s.document_id AND l.stage=s.stage) latest_id
            FROM stage_attempts s JOIN documents d ON d.id=s.document_id
            WHERE s.state IN ('blocked_auth','blocked_release','quarantined') ORDER BY s.id""").fetchall()
        items = []
        review = defaultdict(list)
        plans = defaultdict(list)
        for raw in rows:
            row = dict(raw)
            proof = evidence.get((row["source"], row["source_file_id"]))
            if row["id"] != row["latest_id"]:
                classification = "superseded_stage"
            elif row["state"] == "quarantined":
                classification = "protected_or_content_review"
            elif row["stage"] == "download" and row["artifact_id"]:
                classification = "artifact_present_identity_review"
            elif (
                row["stage"] == "download"
                and row["error_code"] == "staging_collision"
                and proof
            ):
                classification = "receipt_isolated_download_retry"
            elif row["stage"] == "download":
                classification = "missing_attachment_evidence"
            elif row["stage"] == "publish":
                classification = "publication_receipt_review"
            elif row["stage"] == "summary":
                classification = "summary_identity_and_provider_review"
            else:
                classification = "extraction_content_or_dependency_review"
            candidate = {
                key: row[key]
                for key in (
                    "document_id",
                    "stage",
                    "workflow_version",
                    "state",
                    "error_code",
                    "attempt_count",
                    "updated_at_epoch",
                )
            }
            candidate.update(
                attempt_id=row["id"],
                error_category=row["error_category"] or "",
                fingerprint=_stage_retry_fingerprint(row),
            )
            items.append(
                {
                    "candidate": candidate,
                    "classification": classification,
                    "evidence": proof,
                    "source": row["source"],
                    "source_file_id": row["source_file_id"],
                    "source_configured": row["source"] in config.sources,
                    "diagnostic": diagnostic(row["error_detail"]),
                    "pdf_artifact_id": row["artifact_id"],
                }
            )
            review[
                (
                    classification,
                    row["stage"],
                    row["workflow_version"],
                    row["error_code"],
                )
            ].append(candidate)
            if classification == "receipt_isolated_download_retry":
                plans[
                    (row["stage"], row["workflow_version"], row["error_code"])
                ].append(candidate)
        publications = []
        for row in db.execute(
            "SELECT * FROM publications WHERE state<>'success' ORDER BY id"
        ):
            value = dict(row)
            details = json.loads(value["details_json"])
            publications.append(
                {
                    "id": value["id"],
                    "state": value["state"],
                    "fingerprint": fingerprint(value),
                    "write_in_flight": bool(details.get("write_in_flight")),
                    "has_remote_reference": bool(value["remote_reference"]),
                    "member_count": len(details.get("entries", [])),
                    "action": "inspect_remote_and_preserve_or_reconcile_receipt",
                }
            )
    retry_plans = []
    for (stage, workflow, code), candidates in plans.items():
        plan = {
            "schema_version": 1,
            "database": str(config.runtime.database),
            "stage": stage,
            "workflow_version": workflow,
            "error_code": code,
            "expected_count": len(candidates),
            "candidates": candidates,
        }
        plan["plan_hash"] = fingerprint(plan)
        retry_plans.append(plan)
    gates = {
        "receipt_isolated_download_retry": [
            "review source attachment identity",
            "deploy receipt-isolated downloader",
            "regenerate exact fingerprints after idle",
        ],
        "publication_receipt_review": [
            "restore exact legacy source/member/summary binding if absent",
            "inspect uncertain remote body and receipt first",
            "reuse accepted summary without model",
            "verify configured source and target before requeue",
        ],
        "summary_identity_and_provider_review": [
            "verify PDF and extraction cache hashes",
            "classify actual provider diagnostic, not legacy retry-exhausted label",
            "prove current model/route with one schema-valid local request",
            "select only failures resolved by that evidence",
        ],
        "extraction_content_or_dependency_review": [
            "verify PDF integrity and extractor dependency",
            "single-file local extraction proof",
            "retry only transient/tooling failures; retain unreadable content quarantine",
        ],
        "protected_or_content_review": [
            "retain quarantine",
            "do not bypass source protection",
            "replace or manually attest content before any new attempt",
        ],
        "artifact_present_identity_review": [
            "match artifact to source file ID and receipt; do not redownload blindly"
        ],
        "missing_attachment_evidence": [
            "rescan exact source window to reconstruct attachment identity"
        ],
        "superseded_stage": ["retain historical row; no retry"],
    }
    review_cohorts = [
        {
            "classification": key[0],
            "stage": key[1],
            "workflow_version": key[2],
            "error_code": key[3],
            "expected_count": len(candidates),
            "candidates": candidates,
            "required_checks": gates[key[0]],
        }
        for key, candidates in review.items()
    ]
    result = {
        "schema_version": 1,
        "database": str(config.runtime.database),
        "terminal_count": len(items),
        "classification_counts": dict(Counter(i["classification"] for i in items)),
        "records": items,
        "review_cohorts": review_cohorts,
        "retry_plans": retry_plans,
        "publication_review": publications,
        "invalid_saved_plans": invalid,
    }
    result["plan_hash"] = fingerprint(result)
    return result
