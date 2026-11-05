from __future__ import annotations

import json

import pytest

from zsxq_pipeline.lark import LarkCommandError, LarkTimeoutError
from zsxq_pipeline.model import PublicationState
from zsxq_pipeline.publish import (
    PublicationError,
    PublicationReconciliationRequired,
    SummaryForPublish,
    build_publication_groups,
    publish_group,
    resolve_same_day_capacity_target,
)
from zsxq_pipeline.state import InvariantViolation, PipelineState


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


@pytest.mark.parametrize("append", [False, True])
@pytest.mark.parametrize("failure", ["exit", "timeout", "document_network"])
def test_uncertain_body_write_survives_restart_without_a_second_remote_write(tmp_path, append, failure):
    class InterruptedPublisher(FakePublisher):
        def interrupt(self):
            if failure == "exit":
                raise SystemExit("power loss after remote accepted body")
            if failure == "timeout":
                raise LarkTimeoutError("document response timed out after remote accepted body")
            raise LarkCommandError("docs write", 4, json.dumps({
                "error": {"type": "network", "message": 'Post "https://open.feishu.cn/open-apis/docx/v1/documents": EOF'},
            }))

        def create_document(self, markdown):
            super().create_document(markdown)
            self.interrupt()

        def append_document(self, document_url, markdown):
            super().append_document(document_url, markdown)
            self.interrupt()

    database = tmp_path / "pipeline.sqlite3"
    group = build_publication_groups(
        _entries()[:1], target="daily",
        target_document="https://example.test/docx/existing" if append else "",
    )[0]
    failed = InterruptedPublisher()
    with PipelineState.open(database) as state:
        state.migrate()
        expected = SystemExit if failure == "exit" else PublicationReconciliationRequired
        with pytest.raises(expected):
            publish_group(state, failed, group)
        saved = state.get_publication(
            group.summary_sha256, group.target, group.partition_key,
            target_document=group.target_document,
        )
        assert saved.state is PublicationState.INTENT
        assert saved.details["write_in_flight"] is True
        assert saved.remote_reference is None
    assert len(failed.appended if append else failed.created) == 1

    with PipelineState.open(database) as state:
        recovered = FakePublisher()
        recovered.documents.update(failed.documents)
        with pytest.raises(PublicationReconciliationRequired, match="unfinished body write"):
            publish_group(state, recovered, group)
        assert recovered.created == []
        assert recovered.appended == []


@pytest.mark.parametrize("append", [False, True])
def test_crash_before_recording_the_remote_receipt_is_not_replayed(tmp_path, monkeypatch, append):
    database = tmp_path / "pipeline.sqlite3"
    group = build_publication_groups(
        _entries()[:1], target="daily",
        target_document="https://example.test/docx/existing" if append else "",
    )[0]
    publisher = FakePublisher()
    with PipelineState.open(database) as state:
        state.migrate()

        def lose_power(*args, **kwargs):
            raise SystemExit("remote receipt was not committed")

        monkeypatch.setattr(state, "record_remote_write", lose_power)
        with pytest.raises(SystemExit):
            publish_group(state, publisher, group)
    assert len(publisher.appended if append else publisher.created) == 1
    with PipelineState.open(database) as state:
        with pytest.raises(PublicationReconciliationRequired):
            publish_group(state, publisher, group)
    assert len(publisher.appended if append else publisher.created) == 1


@pytest.mark.parametrize("append", [False, True])
@pytest.mark.parametrize("diagnostic", [
    json.dumps({"error": {"type": "auth", "message": "User identity is missing; refresh token expired"}}),
    "HTTP 401 Unauthorized",
    "HTTP 403 Forbidden",
    "invalid access token",
    "refresh_token_reused",
    json.dumps({"error": {"type": "network", "message": 'Post "https://accounts.feishu.cn/oauth/v3/token": EOF'}}),
])
def test_definite_prewrite_rejection_can_resume_after_repair(tmp_path, append, diagnostic):
    class RejectedPublisher(FakePublisher):
        def create_document(self, markdown):
            raise LarkCommandError("docs create", 4, diagnostic)

        def append_document(self, document_url, markdown):
            raise LarkCommandError("docs append", 4, diagnostic)

    database = tmp_path / "pipeline.sqlite3"
    group = build_publication_groups(
        _entries()[:1], target="daily",
        target_document="https://example.test/docx/existing" if append else "",
    )[0]
    with PipelineState.open(database) as state:
        state.migrate()
        with pytest.raises(LarkCommandError):
            publish_group(state, RejectedPublisher(), group)
        saved = state.get_publication(
            group.summary_sha256, group.target, group.partition_key,
            target_document=group.target_document,
        )
        assert saved.state is PublicationState.INTENT
        assert saved.details["write_in_flight"] is False

    with PipelineState.open(database) as state:
        recovered = FakePublisher()
        result = publish_group(state, recovered, group)
        assert result.publication_state is PublicationState.SUCCESS
        assert len(recovered.appended if append else recovered.created) == 1


def test_legacy_intent_without_write_marker_can_make_its_first_write(tmp_path):
    database = tmp_path / "pipeline.sqlite3"
    group = build_publication_groups(_entries()[:1], target="daily")[0]
    with PipelineState.open(database) as state:
        state.migrate()
        state.record_publication_intent(group.summary_sha256, group.target, group.partition_key)
        publisher = FakePublisher()
        assert publish_group(state, publisher, group).publication_state is PublicationState.SUCCESS
        assert len(publisher.created) == 1


@pytest.mark.parametrize("complete", [False, True])
def test_write_marker_api_cannot_reset_a_durable_remote_receipt(tmp_path, complete):
    database = tmp_path / "pipeline.sqlite3"
    group = build_publication_groups(_entries()[:1], target="daily")[0]
    with PipelineState.open(database) as state:
        state.migrate()
        remote = state.record_remote_write(
            group.summary_sha256, group.target, group.partition_key,
            remote_reference="https://example.test/docx/committed",
        )
        if complete:
            remote = state.complete_publication(group.summary_sha256, group.target, group.partition_key)
        for in_flight in (False, True):
            with pytest.raises(InvariantViolation, match="durable remote receipt"):
                state.set_publication_write_in_flight(remote.id, in_flight=in_flight)
        saved = state.get_publication(group.summary_sha256, group.target, group.partition_key)
        assert saved == remote


def test_write_marker_claim_is_exclusive_and_preserves_intent_metadata(tmp_path):
    database = tmp_path / "pipeline.sqlite3"
    with PipelineState.open(database) as state:
        state.migrate()
        intent = state.record_publication_intent("a" * 64, "daily", "partition", details={"title": "kept"})
        claimed = state.set_publication_write_in_flight(intent.id, in_flight=True)
        assert claimed.details == {"title": "kept", "write_in_flight": True}
        with pytest.raises(InvariantViolation, match="already in flight"):
            state.set_publication_write_in_flight(intent.id, in_flight=True)


@pytest.mark.parametrize("append", [False, True])
def test_regroup_after_an_uncertain_write_does_not_publish_the_same_report_again(tmp_path, append):
    class InterruptedPublisher(FakePublisher):
        def create_document(self, markdown):
            super().create_document(markdown)
            raise SystemExit("power loss after remote write")

        def append_document(self, document_url, markdown):
            super().append_document(document_url, markdown)
            raise SystemExit("power loss after remote write")

    database = tmp_path / "pipeline.sqlite3"
    target_document = "https://example.test/docx/existing" if append else ""
    first_group = build_publication_groups(_entries()[:1], target="daily", target_document=target_document)[0]
    regrouped = build_publication_groups(_entries(), target="daily", target_document=target_document)[0]
    assert first_group.summary_sha256 != regrouped.summary_sha256
    interrupted = InterruptedPublisher()
    with PipelineState.open(database) as state:
        state.migrate()
        with pytest.raises(SystemExit):
            publish_group(state, interrupted, first_group)
        original = state.get_publication(
            first_group.summary_sha256, first_group.target, first_group.partition_key,
            target_document=target_document,
        )
        assert original.details["entries"][0]["path"] == _entries()[0].path
        assert original.details["entries"][0]["summary_sha256"] == _entries()[0].summary_sha256

    # A larger recovery batch has a different digest and partition. Report
    # membership still links it to the first uncertain remote operation.
    with PipelineState.open(database) as state:
        recovered = FakePublisher()
        recovered.documents.update(interrupted.documents)
        with pytest.raises(PublicationReconciliationRequired, match="unfinished body write"):
            publish_group(state, recovered, regrouped)
        assert recovered.created == []
        assert recovered.appended == []
        saved = state.get_publication(
            first_group.summary_sha256, first_group.target, first_group.partition_key,
            target_document=target_document,
        )
        assert saved.details["write_in_flight"] is True
    assert len(interrupted.appended if append else interrupted.created) == 1


def test_an_unrelated_report_can_publish_while_known_members_await_reconciliation(tmp_path):
    database = tmp_path / "pipeline.sqlite3"
    first_group = build_publication_groups(_entries()[:1], target="daily")[0]
    unrelated = build_publication_groups(_entries()[1:], target="daily")[0]
    with PipelineState.open(database) as state:
        state.migrate()
        intent = state.record_publication_intent(
            first_group.summary_sha256, first_group.target, first_group.partition_key,
            details={"entries": [{"source": "zsxq", "path": _entries()[0].path,
                                  "summary_sha256": _entries()[0].summary_sha256}]},
        )
        state.set_publication_write_in_flight(intent.id, in_flight=True)
        publisher = FakePublisher()
        assert publish_group(state, publisher, unrelated).publication_state is PublicationState.SUCCESS
        assert len(publisher.created) == 1


def test_inflight_legacy_intent_without_member_metadata_blocks_new_groups(tmp_path):
    database = tmp_path / "pipeline.sqlite3"
    group = build_publication_groups(_entries(), target="daily")[0]
    with PipelineState.open(database) as state:
        state.migrate()
        intent = state.record_publication_intent("f" * 64, "daily", "legacy-partition")
        state.set_publication_write_in_flight(intent.id, in_flight=True)
        publisher = FakePublisher()
        with pytest.raises(PublicationReconciliationRequired):
            publish_group(state, publisher, group)
        assert publisher.created == []
        assert publisher.appended == []


def test_unknown_network_error_mentioning_credentials_keeps_the_write_marker(tmp_path):
    class UncertainPublisher(FakePublisher):
        def create_document(self, markdown):
            raise LarkCommandError("docs create", 4, json.dumps({
                "error": {"type": "network", "message": "invalid access token; upstream connection lost"},
            }))

    database = tmp_path / "pipeline.sqlite3"
    group = build_publication_groups(_entries()[:1], target="daily")[0]
    with PipelineState.open(database) as state:
        state.migrate()
        with pytest.raises(PublicationReconciliationRequired):
            publish_group(state, UncertainPublisher(), group)
        saved = state.get_publication(group.summary_sha256, group.target, group.partition_key)
        assert saved.details["write_in_flight"] is True


@pytest.mark.parametrize("append", [False, True])
@pytest.mark.parametrize("remote_written_only", [False, True])
def test_regroup_does_not_repeat_members_with_a_durable_remote_receipt(tmp_path, append, remote_written_only):
    database = tmp_path / "pipeline.sqlite3"
    target_document = "https://example.test/docx/existing" if append else ""
    first_group = build_publication_groups(_entries()[:1], target="daily", target_document=target_document)[0]
    regrouped = build_publication_groups(_entries(), target="daily", target_document=target_document)[0]
    assert first_group.summary_sha256 != regrouped.summary_sha256
    publisher = FakePublisher(fail_fetch=remote_written_only)
    with PipelineState.open(database) as state:
        state.migrate()
        if remote_written_only:
            with pytest.raises(PublicationError, match="summary anchor"):
                publish_group(state, publisher, first_group)
        else:
            assert publish_group(state, publisher, first_group).publication_state is PublicationState.SUCCESS
    assert len(publisher.appended if append else publisher.created) == 1

    with PipelineState.open(database) as state:
        with pytest.raises(PublicationReconciliationRequired, match="recorded body write"):
            publish_group(state, publisher, regrouped)
        assert len(publisher.appended if append else publisher.created) == 1
        # The original exact group still resumes or returns its receipt.
        publisher.fail_fetch = False
        assert publish_group(state, publisher, first_group).publication_state is PublicationState.SUCCESS
        assert len(publisher.appended if append else publisher.created) == 1
