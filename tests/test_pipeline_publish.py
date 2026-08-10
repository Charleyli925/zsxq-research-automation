from __future__ import annotations

import pytest

from zsxq_pipeline.model import PublicationState
from zsxq_pipeline.publish import (
    PublicationError,
    SummaryForPublish,
    build_publication_groups,
    publish_group,
    resolve_same_day_capacity_target,
)
from zsxq_pipeline.state import PipelineState


class FakePublisher:
    def __init__(self, *, fail_fetch: bool = False) -> None:
        self.fail_fetch = fail_fetch
        self.created: list[str] = []
        self.appended: list[tuple[str, str]] = []
        self.titles: list[tuple[str, str]] = []
        self.permissions: list[tuple[str, str]] = []
        self.documents: dict[str, dict[str, str]] = {}

    def create_document(self, markdown: str):
        url = f"https://example.test/docx/{len(self.created) + 1}"
        self.created.append(markdown)
        self.documents[url] = {"title": "", "markdown": markdown}
        return {"url": url}

    def append_document(self, document_url: str, markdown: str):
        self.appended.append((document_url, markdown))
        current = self.documents.setdefault(document_url, {"title": "existing", "markdown": ""})
        current["markdown"] += markdown
        return {"url": document_url}

    def set_title(self, document_url: str, title: str):
        self.titles.append((document_url, title))
        self.documents[document_url]["title"] = title

    def verify_title(self, document_url: str, expected_title: str):
        if self.documents[document_url]["title"] != expected_title:
            raise RuntimeError("title mismatch")

    def verify_body(self, document_url: str, expected_markdown: str):
        if self.fail_fetch:
            raise RuntimeError("missing local summary anchor")
        if expected_markdown not in self.documents[document_url]["markdown"]:
            raise RuntimeError("missing local summary anchor")

    def grant_chat_view(self, document_url: str, chat_id: str):
        self.permissions.append((document_url, chat_id))


def _entries() -> list[SummaryForPublish]:
    # Completion order is deliberately reversed below; publication order must
    # be based on the durable source/date/file identity instead.
    return [
        SummaryForPublish(
            summary_sha256="a" * 64,
            markdown="# Alpha\n\n正文 A",
            source="zsxq",
            path="/library/alpha.pdf",
            filename="Alpha.pdf",
            source_date="2026-08-09T11:00:00+08:00",
        ),
        SummaryForPublish(
            summary_sha256="b" * 64,
            markdown="# Beta\n\n正文 B",
            source="zsxq",
            path="/library/beta.pdf",
            filename="Beta.pdf",
            source_date="2026-08-10T11:00:00+08:00",
        ),
    ]


def test_groups_are_deterministic_and_keep_existing_capacity_policy():
    groups = build_publication_groups(
        list(reversed(_entries())), target="daily", doc_group_size=1, doc_group_threshold=1
    )
    assert [entry.filename for group in groups for entry in group.entries] == ["Alpha.pdf", "Beta.pdf"]
    assert [group.title for group in groups] == [
        "知识星球研报总结（2026-08-09） 1 篇 1/2",
        "知识星球研报总结（2026-08-10） 1 篇 2/2",
    ]
    assert groups[0].partition_key != groups[1].partition_key


def test_remote_written_recovery_never_creates_a_second_document(tmp_path):
    database = tmp_path / "pipeline.sqlite3"
    group = build_publication_groups(_entries()[:1], target="daily")[0]
    with PipelineState.open(database) as state:
        state.migrate()
        failed = FakePublisher(fail_fetch=True)
        with pytest.raises(PublicationError, match="missing local summary anchor"):
            publish_group(state, failed, group, chat_id="oc_test")
        candidates = state.find_publications(group.summary_sha256, group.target, group.partition_key)
        saved = next(item for item in candidates if item.state is PublicationState.REMOTE_WRITTEN)
        assert saved is not None
        assert saved.state is PublicationState.REMOTE_WRITTEN
        assert len(failed.created) == 1

        recovered = FakePublisher()
        # The remote projection is retained by the fake service in a real
        # retry; provide the same durable URL/content to model that fact.
        remote_url = saved.remote_reference
        assert remote_url
        recovered.documents[remote_url] = {"title": group.title, "markdown": group.markdown}
        result = publish_group(state, recovered, group, chat_id="oc_test")
        assert result.publication_state is PublicationState.SUCCESS
        assert recovered.created == []
        assert recovered.appended == []
        assert recovered.permissions == [(remote_url, "oc_test")]


def test_explicit_target_is_append_only_and_never_mutates_its_title_or_permissions(tmp_path):
    database = tmp_path / "pipeline.sqlite3"
    target = "https://example.test/docx/existing123456"
    group = build_publication_groups(_entries()[:1], target="daily", target_document=target)[0]
    with PipelineState.open(database) as state:
        state.migrate()
        publisher = FakePublisher()
        result = publish_group(state, publisher, group, chat_id="oc_test")

    assert result.publication_state is PublicationState.SUCCESS
    assert publisher.created == []
    assert publisher.appended == [(target, group.markdown)]
    assert publisher.titles == []
    assert publisher.permissions == []


def test_same_day_capacity_reuses_only_a_verified_pipeline_created_document(tmp_path):
    database = tmp_path / "pipeline.sqlite3"
    first_entry = SummaryForPublish(
        summary_sha256="c" * 64,
        markdown="# First\n\n正文 first",
        source="zsxq",
        path="/library/first.pdf",
        filename="First.pdf",
        source_date="2026-08-10T09:00:00+08:00",
    )
    next_entry = SummaryForPublish(
        summary_sha256="d" * 64,
        markdown="# Next\n\n正文 next",
        source="zsxq",
        path="/library/next.pdf",
        filename="Next.pdf",
        source_date="2026-08-10T10:00:00+08:00",
    )
    first_group = build_publication_groups([first_entry], target="daily")[0]
    next_group = build_publication_groups([next_entry], target="daily")[0]
    publisher = FakePublisher()

    with PipelineState.open(database) as state:
        state.migrate()
        initial = publish_group(state, publisher, first_group, chat_id="oc_test")
        continued = resolve_same_day_capacity_target(state, next_group, max_files_per_document=2)
        assert continued.target_document == initial.remote_reference

        result = publish_group(state, publisher, continued, chat_id="oc_test")
        assert result.publication_state is PublicationState.SUCCESS
        assert publisher.appended == [(initial.remote_reference, next_group.markdown)]

        no_room = resolve_same_day_capacity_target(state, next_group, max_files_per_document=1)
        # The group itself has now been published, so recovery must win rather
        # than attempting a capacity-driven second append.
        assert no_room.target_document == ""


def test_same_day_append_recovery_never_retitles_or_regrants_the_existing_document(tmp_path):
    database = tmp_path / "pipeline.sqlite3"
    first, second = _entries()
    second = SummaryForPublish(
        summary_sha256=second.summary_sha256,
        markdown=second.markdown,
        source=second.source,
        path=second.path,
        filename=second.filename,
        source_date=first.source_date,
    )
    initial_group = build_publication_groups([first], target="daily")[0]
    append_group = build_publication_groups([second], target="daily")[0]
    with PipelineState.open(database) as state:
        state.migrate()
        initial_publisher = FakePublisher()
        initial = publish_group(state, initial_publisher, initial_group, chat_id="oc_test")
        continued = resolve_same_day_capacity_target(state, append_group, max_files_per_document=2)
        assert continued.target_document == initial.remote_reference

        failed = FakePublisher(fail_fetch=True)
        failed.documents[initial.remote_reference] = {
            "title": initial_group.title,
            "markdown": initial_group.markdown,
        }
        with pytest.raises(PublicationError):
            publish_group(state, failed, continued, chat_id="oc_test")

        recovered = FakePublisher()
        recovered.documents[initial.remote_reference] = {
            "title": initial_group.title,
            "markdown": initial_group.markdown + append_group.markdown,
        }
        result = publish_group(state, recovered, append_group, chat_id="oc_test")

        assert result.publication_state is PublicationState.SUCCESS
        assert recovered.created == []
        assert recovered.appended == []
        assert recovered.titles == []
        assert recovered.permissions == []
