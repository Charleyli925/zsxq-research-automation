"""Deterministic, recoverable publication of local summary artifacts.

The publisher deliberately treats the local Markdown artifact as the source of
truth.  A remote Feishu document is only a projection of that artifact: after
the remote write it is recorded as ``remote_written`` before any verification
or permission change is attempted.  This makes a crash between create and
fetch recoverable without creating a second document.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Protocol, Sequence

from .model import PublicationState
from .state import PipelineState


class PublicationError(RuntimeError):
    """A remote publication was not safely verified."""


@dataclass(frozen=True, slots=True)
class SummaryForPublish:
    """The minimum immutable local artifact needed to publish one report."""

    summary_sha256: str
    markdown: str
    source: str
    path: str
    filename: str
    source_date: str = ""


@dataclass(frozen=True, slots=True)
class PublicationGroup:
    target: str
    target_document: str
    partition_key: str
    title: str
    markdown: str
    summary_sha256: str
    entries: tuple[SummaryForPublish, ...]


@dataclass(frozen=True, slots=True)
class PublishedDocument:
    remote_reference: str
    publication_state: PublicationState
    created: bool


class LarkPublicationClient(Protocol):
    """Minimal direct Lark document contract used by publication recovery.

    ``create_document`` and ``append_document`` deliberately stop immediately
    after their respective remote body writes.  The caller records
    ``remote_written`` before it performs a title operation, fetch validation,
    or a permission grant.
    """

    def create_document(self, markdown: str) -> Any: ...

    def append_document(self, document_url: str, markdown: str) -> Any: ...

    def set_title(self, document_url: str, title: str) -> Any: ...

    def verify_title(self, document_url: str, expected_title: str) -> Any: ...

    def verify_body(self, document_url: str, expected_markdown: str) -> Any: ...

    def grant_chat_view(self, document_url: str, chat_id: str) -> None: ...


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_text(value: str) -> str:
    return unicodedata.normalize("NFC", str(value or "").strip())


def _source_date(entry: SummaryForPublish) -> str:
    raw = _canonical_text(entry.source_date)
    if not raw:
        return "undated"
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return raw[:10] or "undated"


def _entry_sort_key(entry: SummaryForPublish) -> tuple[str, str, str, str, str]:
    return (
        _source_date(entry),
        _canonical_text(entry.source),
        _canonical_text(entry.filename).casefold(),
        _canonical_text(entry.path),
        entry.summary_sha256,
    )


def _group_digest(entries: Sequence[SummaryForPublish]) -> str:
    payload = [
        {"summary_sha256": item.summary_sha256, "path": _canonical_text(item.path)}
        for item in entries
    ]
    return _sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _document_title(date: str, count: int, index: int, total: int) -> str:
    title = f"知识星球研报总结（{date}） {count} 篇"
    if total > 1:
        title = f"{title} {index}/{total}"
    return title


def build_publication_groups(
    entries: Sequence[SummaryForPublish],
    *,
    target: str,
    target_document: str = "",
    doc_group_size: int = 10,
    doc_group_threshold: int = 15,
) -> list[PublicationGroup]:
    """Build date-stable groups independent of summary completion order.

    ``doc_group_size`` only becomes active once the full batch crosses the
    existing threshold.  This preserves the legacy capacity semantics while
    making the input order explicit and testable.
    """

    if not str(target).strip():
        raise ValueError("target is required")
    if int(doc_group_size) < 1:
        raise ValueError("doc_group_size must be positive")
    if int(doc_group_threshold) < 0:
        raise ValueError("doc_group_threshold must be non-negative")
    ordered = sorted(entries, key=_entry_sort_key)
    if not ordered:
        return []
    group_size = int(doc_group_size) if len(ordered) > int(doc_group_threshold) else len(ordered)
    chunks = [ordered[start : start + group_size] for start in range(0, len(ordered), group_size)]
    total = len(chunks)
    groups: list[PublicationGroup] = []
    for index, chunk in enumerate(chunks, start=1):
        date = _source_date(chunk[0])
        digest = _group_digest(chunk)
        # The digest is part of the partition, so changes to a local summary
        # form a new immutable publication intent instead of overwriting a
        # document from a previous artifact identity.
        partition = f"{date}:{index:03d}:{digest[:20]}"
        markdown = "\n\n".join(item.markdown.strip() for item in chunk if item.markdown.strip()).strip()
        if not markdown:
            raise ValueError("publication group has no Markdown")
        groups.append(
            PublicationGroup(
                target=str(target).strip(),
                target_document=str(target_document).strip(),
                partition_key=partition,
                title=_document_title(date, len(chunk), index, total),
                markdown=markdown,
                summary_sha256=digest,
                entries=tuple(chunk),
            )
        )
    return groups


def resolve_same_day_capacity_target(
    state: PipelineState,
    group: PublicationGroup,
    *,
    max_files_per_document: int,
) -> PublicationGroup:
    """Reuse one verified pipeline document when its same-day capacity permits.

    The legacy worker made this decision only for the first new publication
    group of a run.  The caller keeps that boundary; this helper merely finds
    a safe target from durable SQLite records.  It intentionally ignores
    imported/unknown records and user-managed explicit targets: lacking a
    durable ``created_document`` flag or an integer ``entry_count`` must cause
    a new document, never an unbounded append.
    """

    if int(max_files_per_document) < 1:
        raise ValueError("max_files_per_document must be positive")
    if group.target_document:
        return group
    # A retry must always recover its own intent/remote-written transaction
    # before making a new capacity decision.
    if state.find_publications(group.summary_sha256, group.target, group.partition_key):
        return group
    date, separator, _ = group.partition_key.partition(":")
    if not separator or not date:
        return group
    by_document: dict[str, int] = {}
    ordered_documents: list[str] = []
    for record in state.list_publications(
        target=group.target,
        states=(PublicationState.SUCCESS,),
        partition_prefix=f"{date}:",
    ):
        document = str(record.target_document or "").strip()
        if not document or record.details.get("created_document") is not True:
            continue
        raw_count = record.details.get("entry_count")
        if isinstance(raw_count, bool):
            continue
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            continue
        if count < 1:
            continue
        if document not in by_document:
            by_document[document] = 0
            ordered_documents.append(document)
        by_document[document] += count
    incoming = len(group.entries)
    for document in ordered_documents:
        if by_document[document] + incoming <= int(max_files_per_document):
            return replace(group, target_document=document)
    return group


def _remote_reference(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("url", "doc_url", "document", "remote_reference"):
            candidate = str(value.get(key, "")).strip()
            if candidate:
                return candidate
    for name in ("url", "doc_url", "document", "remote_reference"):
        candidate = str(getattr(value, name, "")).strip()
        if candidate:
            return candidate
    return ""


def _state_kwargs(method: Any, *, target_document: str) -> dict[str, str]:
    """Use the v2 target-document identity without breaking v1 import tests.

    The production state migration adds the optional argument.  The small
    fallback keeps this module usable against an imported v1 fixture while
    retaining a deterministic partition identity there.
    """

    try:
        supports = "target_document" in inspect.signature(method).parameters
    except (TypeError, ValueError):  # pragma: no cover - defensive for C callables
        supports = False
    return {"target_document": target_document} if supports else {}


def _publication_details(group: PublicationGroup, *, target_document: str, created_document: bool) -> dict[str, Any]:
    return {
        "title": group.title,
        "entry_count": len(group.entries),
        "target_document": target_document,
        "created_document": created_document,
    }


def _created_document(record: Any, *, default: bool = False) -> bool:
    """Read the durable ownership bit conservatively during recovery."""

    details = getattr(record, "details", {})
    if isinstance(details, Mapping):
        value = details.get("created_document")
        if isinstance(value, bool):
            return value
    return default


def _record_intent(state: PipelineState, group: PublicationGroup) -> Any:
    return state.record_publication_intent(
        group.summary_sha256,
        group.target,
        group.partition_key,
        details=_publication_details(
            group,
            target_document=group.target_document,
            created_document=not bool(group.target_document),
        ),
        **_state_kwargs(state.record_publication_intent, target_document=group.target_document),
    )


def _get_publication(state: PipelineState, group: PublicationGroup) -> Any:
    return state.get_publication(
        group.summary_sha256,
        group.target,
        group.partition_key,
        **_state_kwargs(state.get_publication, target_document=group.target_document),
    )


def _record_remote_write(
    state: PipelineState,
    group: PublicationGroup,
    *,
    reference: str,
    target_document: str,
) -> Any:
    return state.record_remote_write(
        group.summary_sha256,
        group.target,
        group.partition_key,
        remote_reference=reference,
        details=_publication_details(
            group,
            target_document=target_document,
            created_document=not bool(group.target_document),
        ),
        **_state_kwargs(state.record_remote_write, target_document=target_document),
    )


def _complete(state: PipelineState, group: PublicationGroup) -> Any:
    return state.complete_publication(
        group.summary_sha256,
        group.target,
        group.partition_key,
        **_state_kwargs(state.complete_publication, target_document=group.target_document),
    )


def _verify_remote(
    publisher: LarkPublicationClient,
    group: PublicationGroup,
    reference: str,
    *,
    chat_id: str,
    created_document: bool,
) -> None:
    """Finish the post-write transaction without ever writing the body again.

    A date-grouped document owns its title and target-chat permission only
    when this pipeline created it.  An explicitly configured target document
    is append-only: changing its title or permissions for each digest would
    mutate a user-managed document outside the requested publication scope.
    """

    if created_document:
        # This is intentionally after the durable remote_written transition.
        # Repeating a title patch after a crash is idempotent; repeating a body
        # write is not, which is why create/append never appears in this path.
        publisher.set_title(reference, group.title)
        publisher.verify_title(reference, group.title)
    try:
        publisher.verify_body(reference, group.markdown)
    except Exception as exc:
        # Preserve the publication-specific error boundary for callers while
        # retaining the adapter's diagnostic as the chained cause.
        raise PublicationError("remote document body is missing local summary anchor") from exc
    if created_document and chat_id:
        publisher.grant_chat_view(reference, chat_id)


def publish_group(
    state: PipelineState,
    publisher: LarkPublicationClient,
    group: PublicationGroup,
    *,
    chat_id: str = "",
) -> PublishedDocument:
    """Publish or resume one deterministic group.

    A ``remote_written`` row is intentionally verified and granted again on a
    retry.  It is never created or appended again, even if a process crashed
    just after Lark accepted the original write.
    """

    existing = _get_publication(state, group)
    # A newly-created document is not known until after Lark accepts the
    # write.  Once it is known, state v2 binds the pending empty-target intent
    # to that real document.  A retry starts with an empty target again, so
    # look across the logical partition before deciding to create anything.
    if not group.target_document and (existing is None or existing.state is PublicationState.INTENT):
        finder = getattr(state, "find_publications", None)
        if callable(finder):
            variants = tuple(finder(group.summary_sha256, group.target, group.partition_key))
            recovered = next(
                (
                    item
                    for item in variants
                    if item.state in {PublicationState.REMOTE_WRITTEN, PublicationState.SUCCESS}
                    and str(item.remote_reference or "").strip()
                ),
                None,
            )
            if recovered is not None:
                existing = recovered
    if existing is None:
        existing = _record_intent(state, group)
    reference = str(getattr(existing, "remote_reference", "") or "").strip()
    effective_group = (
        replace(group, target_document=str(getattr(existing, "target_document", "") or "").strip())
        if str(getattr(existing, "target_document", "") or "").strip()
        else group
    )
    created = False
    created_document = _created_document(existing)
    if existing.state is PublicationState.SUCCESS:
        return PublishedDocument(remote_reference=reference, publication_state=PublicationState.SUCCESS, created=False)
    if existing.state is PublicationState.REMOTE_WRITTEN:
        if not reference:
            raise PublicationError("remote_written publication has no remote reference")
    else:
        remote = (
            publisher.append_document(group.target_document, group.markdown)
            if group.target_document
            else publisher.create_document(group.markdown)
        )
        reference = _remote_reference(remote)
        if not reference:
            raise PublicationError("Lark write returned no document reference")
        # This is deliberately before title/fetch/permission verification.
        effective_group = replace(group, target_document=group.target_document or reference)
        _record_remote_write(
            state,
            group,
            reference=reference,
            target_document=effective_group.target_document,
        )
        created = not bool(group.target_document)
        created_document = created

    _verify_remote(
        publisher,
        effective_group,
        reference,
        chat_id=chat_id,
        created_document=created_document,
    )
    completed = _complete(state, effective_group)
    return PublishedDocument(
        remote_reference=reference,
        publication_state=completed.state,
        created=created,
    )
