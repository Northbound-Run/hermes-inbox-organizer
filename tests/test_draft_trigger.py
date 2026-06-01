"""Draft trigger: instruction, dispatcher seam, synthetic-injection, fallback."""

from __future__ import annotations

import pytest

from hermes_inbox_organizer.draft_trigger import (
    DraftTrigger,
    FakeDispatcher,
    GatewayInjectionDispatcher,
    HttpWakeDispatcher,
    NoopPoster,
    build_draft_instruction,
    pending_drafts_context,
)


def test_instruction_carries_thread_context() -> None:
    text = build_draft_instruction(
        account_id="a1", thread_id="t1", sender="alice@x.com", subject="Re: lunch"
    )
    assert "t1" in text
    assert "alice@x.com" in text
    assert "Re: lunch" in text
    assert "inbox_create_draft" in text
    assert "Do not send" in text


def test_trigger_dispatches_once_with_instruction() -> None:
    disp = FakeDispatcher()
    DraftTrigger(disp).request_draft(
        account_id="a1", thread_id="t1", sender="alice@x.com", subject="hi"
    )
    assert len(disp.dispatched) == 1
    assert "t1" in disp.dispatched[0]


def test_http_wake_dispatcher_posts() -> None:
    poster = NoopPoster()
    HttpWakeDispatcher(poster).dispatch("draft this")
    assert poster.posted == ["draft this"]


def test_gateway_injection_builds_internal_event_and_dispatches() -> None:
    # Fake gateway records the event passed to _handle_message.
    seen: dict = {}

    class FakeGateway:
        async def _handle_message(self, event):  # noqa: ANN001
            seen["event"] = event
            return "ok"

    def make_event(instruction: str, source):  # noqa: ANN001
        return {"text": instruction, "source": source, "internal": True}

    def run_coro(coro):  # drive the coroutine to completion synchronously
        import asyncio

        return asyncio.new_event_loop().run_until_complete(coro)

    disp = GatewayInjectionDispatcher(
        get_gateway=lambda: FakeGateway(),
        get_source=lambda: "session-source",
        make_event=make_event,
        run_coro=run_coro,
    )
    disp.dispatch("please draft")

    assert seen["event"]["internal"] is True  # the recursion guard
    assert seen["event"]["text"] == "please draft"
    assert seen["event"]["source"] == "session-source"


def test_gateway_injection_raises_until_gateway_captured() -> None:
    disp = GatewayInjectionDispatcher(
        get_gateway=lambda: None,  # not captured yet
        get_source=lambda: None,
        make_event=lambda i, s: object(),
        run_coro=lambda c: None,
    )
    with pytest.raises(RuntimeError):
        disp.dispatch("x")


def test_pending_context_none_when_empty_and_lists_threads() -> None:
    assert pending_drafts_context([]) is None
    ctx = pending_drafts_context(["t1", "t2"])
    assert ctx is not None
    assert "t1" in ctx["context"] and "t2" in ctx["context"]
    assert "inbox_create_draft" in ctx["context"]
