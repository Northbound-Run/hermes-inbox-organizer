"""Draft trigger — induce a *context-rich* agent turn to compose a reply.

The background daemon detects a "To Respond" message but does NOT write the
draft itself. The whole point of the plugin form is that **Hermes** drafts,
using its vault / memory / chat-transcript context — so the daemon asks the
agent to do it, then the agent calls ``inbox_create_draft`` (see ``inbox_tool``).

GROUNDED against the real gateway source (``gateway/run.py`` in
``NousResearch/hermes-agent`` @sha256:b6e41c15…):

- ``GatewayRunner._handle_message(event: MessageEvent)`` (run.py:6755) is the
  inbound entrypoint. It loads the session (transcript) and runs an agent turn.
- It only fires ``pre_gateway_dispatch`` and user-auth when the event is NOT
  internal: ``is_internal = bool(getattr(event, "internal", False))``. Hermes
  itself injects synthetic turns as ``MessageEvent(text=…, source=…,
  internal=True)`` then ``await self._handle_message(event)`` (run.py:4587).
- So **`internal=True` is the recursion guard**: our injected turn will not
  re-enter our own ``pre_gateway_dispatch`` hook. The ``source`` targets the
  user's live session, which is what carries the transcript.

Two dispatchers implement the same ``Dispatcher`` seam so the daemon's routing
is unit-testable without a live agent:

1. ``GatewayInjectionDispatcher`` (DEFAULT) — synthetic ``MessageEvent`` into
   the captured ``GatewayRunner``. Most native; carries the transcript.
2. ``HttpWakeDispatcher`` (FALLBACK) — POST to the OpenAI-compatible
   ``/v1/chat/completions`` (as ``hermes-paperclip-bridge`` does). Spins a
   server-side agent that has vault+memory but NOT the live transcript.

The default was chosen empirically against a real Hermes.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Awaitable, Callable, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Instruction + proactive-nudge helpers
# ---------------------------------------------------------------------------

def build_draft_instruction(
    *, account_id: str, thread_id: str, sender: str, subject: str
) -> str:
    """Compose the wake message handed to the agent."""
    return (
        "A new email needs a reply.\n"
        f"- account: {account_id}\n"
        f"- thread_id: {thread_id}\n"
        f"- from: {sender}\n"
        f"- subject: {subject}\n\n"
        "Read the thread with the inbox tools, draft a reply in my voice using "
        "everything you know about this person and our prior conversations, then "
        "call inbox_create_draft(account_id, thread_id, body). Do not send."
    )


def pending_drafts_context(thread_ids: list[str]) -> dict | None:
    """``pre_llm_call`` hook payload: proactively surface awaiting drafts.

    Returns ``{"context": ...}`` (appended to the turn's user message per the
    real hook contract) or ``None`` when nothing is pending. Only used if
    Confirmed against a real Hermes that ``pre_llm_call`` actually fires.
    """
    if not thread_ids:
        return None
    joined = ", ".join(thread_ids)
    return {
        "context": (
            f"You have reply drafts awaiting on threads: {joined}. "
            "Use inbox_create_draft to complete them."
        )
    }


# ---------------------------------------------------------------------------
# Dispatcher seam
# ---------------------------------------------------------------------------

class Dispatcher(Protocol):
    def dispatch(self, instruction: str) -> None: ...


class FakeDispatcher:
    """Records dispatches instead of touching a live agent (tests/unconfigured)."""

    def __init__(self) -> None:
        self.dispatched: list[str] = []

    def dispatch(self, instruction: str) -> None:
        self.dispatched.append(instruction)


# ---------------------------------------------------------------------------
# DEFAULT: synthetic MessageEvent injection into the live gateway
# ---------------------------------------------------------------------------

# A representative source object for the user's session (a gateway SessionSource).
# Captured lazily from the live gateway; opaque to this module.
Source = Any


def _default_make_event(instruction: str, source: Source) -> Any:
    """Build a real internal ``MessageEvent``. Needs the Hermes runtime present."""
    from gateway.platforms.base import MessageEvent  # lazy: live-only import

    # internal=True => _handle_message skips pre_gateway_dispatch (recursion
    # guard) and user-auth, while still loading the session transcript.
    return MessageEvent(text=instruction, source=source, internal=True)


def _default_run_coro(coro: Awaitable[Any]) -> None:
    """Run the gateway coroutine to completion from our daemon thread.

    Bridges sync→async like hermes-chat-recorder's background loop. Replaced in
    tests; the real wiring schedules onto the gateway's running event loop.
    """
    from ._background_loop import get_background_loop  # type: ignore[attr-defined]

    get_background_loop().run_coro_sync(coro)


class GatewayInjectionDispatcher:
    """Inject a synthetic, transcript-aware agent turn (DEFAULT trigger).

    ``get_gateway``/``get_source`` are lazy getters populated when the plugin
    captures the live ``GatewayRunner`` via its ``pre_gateway_dispatch`` hook
    (chat-recorder pattern). ``make_event``/``run_coro`` are injectable seams so
    the routing is unit-testable without the Hermes runtime.
    """

    def __init__(
        self,
        *,
        get_gateway: Callable[[], Any | None],
        get_source: Callable[[], Source | None],
        make_event: Callable[[str, Source], Any] = _default_make_event,
        run_coro: Callable[[Awaitable[Any]], None] = _default_run_coro,
    ) -> None:
        self._get_gateway = get_gateway
        self._get_source = get_source
        self._make_event = make_event
        self._run_coro = run_coro

    def dispatch(self, instruction: str) -> None:
        gateway = self._get_gateway()
        source = self._get_source()
        if gateway is None or source is None:
            raise RuntimeError(
                "gateway not captured yet (no pre_gateway_dispatch seen); "
                "cannot inject draft turn"
            )
        event = self._make_event(instruction, source)
        # GatewayRunner._handle_message is async; run it to completion.
        self._run_coro(gateway._handle_message(event))


# ---------------------------------------------------------------------------
# FALLBACK: /v1/chat/completions wake (server-side agent; no live transcript)
# ---------------------------------------------------------------------------

class MessagePoster(Protocol):
    def post(self, content: str) -> dict: ...


class NoopPoster:
    """Records requests instead of hitting the agent (unconfigured / tests)."""

    def __init__(self) -> None:
        self.posted: list[str] = []

    def post(self, content: str) -> dict:
        self.posted.append(content)
        return {"status": 0, "noop": True}


class HttpMessagePoster:
    """POST to the local Hermes OpenAI-compatible endpoint (e.g. localhost:8642).

    Needs a running agent + ``API_SERVER_KEY`` — exercised only in live
    validation. Uses stdlib ``urllib`` so this path has no runtime dependency
    for this path.
    """

    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model

    def post(self, content: str) -> dict:
        import json
        import urllib.request

        payload = json.dumps(
            {"model": self._model, "messages": [{"role": "user", "content": content}]}
        ).encode()
        req = urllib.request.Request(
            f"{self._base_url}/v1/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        # A full draft turn (read thread + compose + drafts.create) can take well
        # over a minute; keep the timeout generous so we don't abandon a turn the
        # agent is still completing server-side.
        with urllib.request.urlopen(req, timeout=300) as resp:  # noqa: S310 localhost
            return {"status": resp.status, "body": resp.read().decode()}


class HttpWakeDispatcher:
    """Fallback dispatcher wrapping a MessagePoster (the /v1/chat/completions path)."""

    def __init__(self, poster: MessagePoster) -> None:
        self._poster = poster

    def dispatch(self, instruction: str) -> None:
        self._poster.post(instruction)


class DraftTrigger:
    """Routes a To-Respond message to a Dispatcher with a composed instruction."""

    def __init__(self, dispatcher: Dispatcher) -> None:
        self._dispatcher = dispatcher

    def request_draft(
        self, *, account_id: str, thread_id: str, sender: str, subject: str
    ) -> None:
        self._dispatcher.dispatch(
            build_draft_instruction(
                account_id=account_id,
                thread_id=thread_id,
                sender=sender,
                subject=subject,
            )
        )


def wake_draft(
    *,
    account_id: str,
    thread_id: str,
    sender: str = "",
    subject: str = "",
    instruction: "str | None" = None,
    poster: "MessagePoster | None" = None,
) -> bool:
    """Autonomous draft trigger: POST a drafting task to the local Hermes api_server.

    The PROVEN path — every live draft test drove the agent this way, and it's
    silent (no Matrix-chat pollution). Reads HERMES_API_URL / API_SERVER_KEY /
    HERMES_MODEL from env; ``poster`` is injectable for tests.

    ``instruction`` is the prebuilt wake message (the runtime passes the context-rich
    brief from ``brief.build_draft_brief``). When None, falls back to the minimal
    ``build_draft_instruction`` so direct/unenriched callers still work.
    """
    if instruction is None:
        instruction = build_draft_instruction(
            account_id=account_id, thread_id=thread_id, sender=sender, subject=subject
        )
    if poster is not None:
        # injected (tests): run synchronously for determinism
        try:
            poster.post(instruction)
            return True
        except Exception:
            logger.exception("inbox: wake_draft failed for thread %s", thread_id)
            return False

    # production: fire-and-forget so the drain doesn't block on the multi-minute
    # agent turn. The api_server runs the turn + the agent calls inbox_create_draft.
    real_poster = HttpMessagePoster(
        os.environ.get("HERMES_API_URL", "http://localhost:8642"),
        os.environ.get("API_SERVER_KEY", ""),
        os.environ.get("INBOX_WAKE_MODEL", "hermes-agent"),
    )

    def _post() -> None:
        try:
            real_poster.post(instruction)
            logger.info("inbox: wake_draft completed for thread %s", thread_id)
        except Exception:
            logger.exception("inbox: wake_draft post failed for thread %s", thread_id)

    threading.Thread(target=_post, name="inbox-wake", daemon=True).start()
    logger.info("inbox: wake_draft dispatched (async) for thread %s", thread_id)
    return True
