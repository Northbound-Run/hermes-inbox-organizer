"""Draft trigger: instruction, dispatcher seam, synthetic-injection, fallback."""

from __future__ import annotations

import pytest

from hermes_inbox_organizer.draft_trigger import (
    DRAFT_TURN_SENTINEL,
    DraftTrigger,
    FakeDispatcher,
    GatewayInjectionDispatcher,
    HttpMessagePoster,
    HttpWakeDispatcher,
    NoopPoster,
    build_draft_instruction,
    pending_drafts_context,
    wake_draft,
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
        async def _handle_message(self, event):
            seen["event"] = event
            return "ok"

    def make_event(instruction: str, source):
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


def test_instruction_carries_sentinel_and_guardrail() -> None:
    text = build_draft_instruction(account_id="a", thread_id="t", sender="s@x", subject="j")
    assert DRAFT_TURN_SENTINEL in text   # so even the fallback wake is recognized + restricted (B4)
    assert "untrusted" in text.lower()   # guardrail present on the fallback path too
    assert "inbox_create_draft" in text


def test_http_poster_uses_configured_timeout(monkeypatch) -> None:
    # AC13: the wake POST uses the (config-supplied) timeout, not a hardcoded 300.
    import urllib.request

    captured: dict = {}

    class _Resp:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    HttpMessagePoster("http://localhost:8642", "key", "model", timeout=123).post("hi")
    assert captured["timeout"] == 123


def test_wake_draft_fallback_instruction_carries_sentinel() -> None:
    # When no brief is supplied (instruction=None), wake_draft rebuilds the minimal
    # instruction — which must STILL carry the sentinel so the turn is restricted (B4).
    poster = NoopPoster()
    wake_draft(
        account_id="a", thread_id="t", sender="s@x", subject="j", instruction=None, poster=poster
    )
    assert poster.posted and DRAFT_TURN_SENTINEL in poster.posted[0]
