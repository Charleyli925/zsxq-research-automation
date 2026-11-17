"""Local projections are independent durable consumers of accepted summaries."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import time
from pathlib import Path

from .model import SummaryIdentity
from .publish import SummaryForPublish
from .sidecars import ArtifactSidecars
from .summary import SummaryStore
from .tasks import TaskQueue, TaskResult, diagnostic, retry_seconds
from .state import PipelineState


def accepted_summary(state, document_id):
    row = state._connection.execute(
        """SELECT a.* FROM stage_attempts s JOIN artifacts a ON a.id=s.output_artifact_id
        WHERE s.document_id=? AND s.stage='summary' AND s.state='succeeded' AND a.kind='summary'
        ORDER BY s.id DESC LIMIT 1""",
        (document_id,),
    ).fetchone()
    return dict(row) if row else None


def load_summary(config, artifact, *, path=None, filename=None):
    identity = SummaryIdentity(
        *(
            artifact[key]
            for key in (
                "pdf_sha256",
                "extractor_version",
                "prompt_version",
                "model",
                "reasoning",
            )
        )
    )
    cached = SummaryStore(config.runtime.root / "summary_cache").load(identity)
    if cached is None or cached.markdown_sha256 != artifact["content_sha256"]:
        raise ValueError("accepted summary cache unavailable or changed")
    if path is not None:
        cached = replace(
            cached,
            entry=replace(
                cached.entry, path=str(path), filename=filename or cached.entry.filename
            ),
        )
    return cached


def enqueue_projection(
    state, document_id, summary_artifact_id, *, source, publication_id=0
):
    return TaskQueue(state).enqueue(
        f"projection:{document_id}:{summary_artifact_id}:{publication_id}",
        "projection",
        {
            "document_id": document_id,
            "summary_artifact_id": summary_artifact_id,
            "publication_id": publication_id,
        },
        source=source,
    )


def run_projection(config, task):
    if config.pipeline.research_library_root is None:
        return TaskResult()
    with PipelineState.open(config.runtime.database) as state:
        payload = task["payload"]
        document = state.get_document(payload["document_id"])
        artifact = dict(
            state._connection.execute(
                "SELECT * FROM artifacts WHERE id=?", (payload["summary_artifact_id"],)
            ).fetchone()
        )
        pdf = dict(
            state._connection.execute(
                "SELECT * FROM artifacts WHERE id=?", (document.artifact_id,)
            ).fetchone()
        )
        reference = ""
        publication_id = payload.get("publication_id")
        if publication_id:
            publication = state._connection.execute(
                "SELECT * FROM publications WHERE id=? AND state='success'",
                (publication_id,),
            ).fetchone()
            if publication is None:
                return TaskResult("needs_reconciliation", "publication_not_verified")
            reference = publication["remote_reference"]
        else:
            # Never let a late summary-only projection overwrite publication
            # metadata. A pending verified-publication projection owns it.
            newer = state._connection.execute(
                """SELECT 1 FROM tasks WHERE kind='projection'
                AND json_extract(payload_json,'$.document_id')=?
                AND json_extract(payload_json,'$.summary_artifact_id')=?
                AND json_extract(payload_json,'$.publication_id')>0 LIMIT 1""",
                (document.id, artifact["id"]),
            ).fetchone()
            if newer:
                return TaskResult()
    try:
        path = Path(pdf["canonical_path"])
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest != artifact["pdf_sha256"]:
            return TaskResult("needs_action", "projection_pdf_changed")
        cached = load_summary(config, artifact, path=path, filename=document.filename)
        item = {
            "path": str(path),
            "filename": document.filename,
            "pdf_sha256": digest,
            "batch_id": "unified-tick",
            "title": cached.entry.title,
            "source": document.source,
            "source_file_id": document.source_file_id,
        }
        sidecars = ArtifactSidecars(
            library_root=config.pipeline.research_library_root,
            library_database=config.pipeline.research_library_database,
            vault_root=config.pipeline.obsidian_vault_root,
            work_root=config.runtime.root / "work/tasks" / str(task["id"]),
        )
        item["summary_md_path"] = str(sidecars.persist_summary(item, cached))
        if reference:
            entry = SummaryForPublish(
                cached.markdown_sha256,
                cached.entry.markdown,
                item["batch_id"],
                str(path),
                document.filename,
                title=cached.entry.title,
            )
            sidecars.archive_published_group(
                entries=(entry,), batch_items={str(path): item}, document_url=reference
            )
            with PipelineState.open(config.runtime.database) as state:
                with state._write_transaction():
                    state._connection.execute(
                        """UPDATE tasks SET state='succeeded',error_code='projection_superseded',
                        error_detail='Verified publication projection includes the same accepted summary',finished_at_epoch=?
                        WHERE kind='projection' AND state NOT IN ('running','succeeded')
                          AND json_extract(payload_json,'$.document_id')=?
                          AND json_extract(payload_json,'$.summary_artifact_id')=?
                          AND json_extract(payload_json,'$.publication_id')=0""",
                        (int(time.time()), document.id, artifact["id"]),
                    )
        return TaskResult()
    except (ValueError, KeyError) as exc:
        return TaskResult(
            "needs_action", "projection_identity_invalid", diagnostic(exc)
        )
    except Exception as exc:
        return TaskResult(
            "retry_wait",
            "projection_failed",
            diagnostic(exc),
            retry_seconds(task.get("failure_count", 0) + 1),
        )
