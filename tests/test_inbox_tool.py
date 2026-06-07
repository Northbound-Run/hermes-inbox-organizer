"""The inbox_create_draft tool handler (account->writer resolver seam)."""

from __future__ import annotations

import json

from hermes_inbox_organizer.inbox_tool import (
    LoggingDraftWriter,
    UnconfiguredWriter,
    make_inbox_create_draft_handler,
)


class FakeWriter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create_draft(self, *, account_id: str, thread_id: str, body: str) -> str:
        self.calls.append(
            {"account_id": account_id, "thread_id": thread_id, "body": body}
        )
        return "draft-123"


def test_handler_creates_draft_and_returns_json() -> None:
    writer = FakeWriter()
    handler = make_inbox_create_draft_handler(lambda aid: writer)
    out = json.loads(
        handler({"account_id": "a1", "thread_id": "t1", "body": "Sounds good."})
    )
    assert out == {"ok": True, "draft_id": "draft-123", "thread_id": "t1"}
    assert writer.calls == [
        {"account_id": "a1", "thread_id": "t1", "body": "Sounds good."}
    ]


def test_handler_validates_required_args() -> None:
    handler = make_inbox_create_draft_handler(lambda aid: FakeWriter())
    assert "error" in json.loads(handler({"thread_id": "t1"}))


def test_handler_reports_not_connected() -> None:
    handler = make_inbox_create_draft_handler(lambda aid: None)
    out = json.loads(handler({"account_id": "a1", "thread_id": "t1", "body": "hi"}))
    assert "not connected" in out["error"]


def test_handler_never_raises_on_writer_failure() -> None:
    handler = make_inbox_create_draft_handler(lambda aid: UnconfiguredWriter())
    out = json.loads(handler({"account_id": "a1", "thread_id": "t1", "body": "hi"}))
    assert "error" in out  # returned as JSON, not raised


def test_logging_writer_succeeds_for_probe() -> None:
    handler = make_inbox_create_draft_handler(lambda aid: LoggingDraftWriter())
    out = json.loads(handler({"account_id": "a1", "thread_id": "t1", "body": "hello"}))
    assert out["ok"] is True
    assert out["draft_id"] == "probe-draft"


class FakeWriterWithUpdate:
    """Writer that supports both create and update, recording each call."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.updated: list[dict] = []

    def create_draft(self, *, account_id: str, thread_id: str, body: str) -> str:
        self.created.append({"account_id": account_id, "thread_id": thread_id, "body": body})
        return "new-draft"

    def update_draft(self, *, account_id: str, thread_id: str, body: str, draft_id: str) -> str:
        self.updated.append(
            {"account_id": account_id, "thread_id": thread_id, "body": body, "draft_id": draft_id}
        )
        return draft_id


def test_handler_records_draft_id_on_success() -> None:
    # AC1: a successful create flows the new draft id to the record_draft seam.
    writer = FakeWriter()
    recorded: list[tuple] = []
    handler = make_inbox_create_draft_handler(
        lambda aid: writer, record_draft=lambda a, t, d: recorded.append((a, t, d))
    )
    out = json.loads(handler({"account_id": "a1", "thread_id": "t1", "body": "hi"}))
    assert out["ok"] is True and out["draft_id"] == "draft-123"
    assert recorded == [("a1", "t1", "draft-123")]


def test_handler_updates_existing_draft_instead_of_creating() -> None:
    # AC6: when the ledger already has a draft id, re-draft UPDATES (no duplicate).
    writer = FakeWriterWithUpdate()
    recorded: list[tuple] = []
    handler = make_inbox_create_draft_handler(
        lambda aid: writer,
        lookup_draft=lambda a, t: "existing-9",
        record_draft=lambda a, t, d: recorded.append((a, t, d)),
    )
    out = json.loads(handler({"account_id": "a1", "thread_id": "t1", "body": "v2"}))
    assert out["ok"] is True and out["draft_id"] == "existing-9"
    assert writer.updated and writer.updated[0]["draft_id"] == "existing-9"
    assert writer.created == []
    assert recorded == [("a1", "t1", "existing-9")]


def test_handler_creates_when_no_existing_draft() -> None:
    writer = FakeWriterWithUpdate()
    handler = make_inbox_create_draft_handler(lambda aid: writer, lookup_draft=lambda a, t: None)
    json.loads(handler({"account_id": "a1", "thread_id": "t1", "body": "v1"}))
    assert writer.created and not writer.updated  # no existing id -> create path


def test_handler_recorder_failure_does_not_break_tool() -> None:
    def boom(a, t, d):
        raise RuntimeError("db down")

    handler = make_inbox_create_draft_handler(lambda aid: FakeWriter(), record_draft=boom)
    out = json.loads(handler({"account_id": "a1", "thread_id": "t1", "body": "hi"}))
    assert out["ok"] is True  # recorder failure is logged, never raised out of the tool


# ── record_outcome seam (draft-feedback-loop body capture) ──────────────────────

def test_record_outcome_receives_account_thread_body_draft_id() -> None:
    # The seam is called with (account_id, thread_id, body, draft_id) on success.
    writer = FakeWriter()
    captured: list[tuple] = []
    handler = make_inbox_create_draft_handler(
        lambda aid: writer,
        record_outcome=lambda a, t, b, d: captured.append((a, t, b, d)),
    )
    out = json.loads(handler({"account_id": "a1", "thread_id": "t1", "body": "Hello there."}))
    assert out["ok"] is True
    assert captured == [("a1", "t1", "Hello there.", "draft-123")]


def test_record_outcome_failure_does_not_break_tool() -> None:
    # A record_outcome that raises must be swallowed; the tool still returns ok.
    def boom(a, t, b, d):
        raise RuntimeError("outcome db down")

    handler = make_inbox_create_draft_handler(
        lambda aid: FakeWriter(), record_outcome=boom
    )
    out = json.loads(handler({"account_id": "a1", "thread_id": "t1", "body": "hi"}))
    assert out["ok"] is True


def test_record_outcome_none_behaves_as_before() -> None:
    # When record_outcome is not supplied (default None) the handler is unchanged.
    writer = FakeWriter()
    handler = make_inbox_create_draft_handler(lambda aid: writer)
    out = json.loads(handler({"account_id": "a1", "thread_id": "t1", "body": "hi"}))
    assert out == {"ok": True, "draft_id": "draft-123", "thread_id": "t1"}
    assert writer.calls == [{"account_id": "a1", "thread_id": "t1", "body": "hi"}]


def test_record_outcome_called_with_updated_draft_id() -> None:
    # On a re-draft (update path), record_outcome receives the existing draft id.
    writer = FakeWriterWithUpdate()
    captured: list[tuple] = []
    handler = make_inbox_create_draft_handler(
        lambda aid: writer,
        lookup_draft=lambda a, t: "existing-9",
        record_outcome=lambda a, t, b, d: captured.append((a, t, b, d)),
    )
    out = json.loads(handler({"account_id": "a1", "thread_id": "t1", "body": "v2 body"}))
    assert out["ok"] is True
    assert captured == [("a1", "t1", "v2 body", "existing-9")]
