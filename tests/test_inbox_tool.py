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
