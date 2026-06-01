"""The native ``inbox_create_draft`` agent tool.

This is the write-back primitive Hermes calls *after* it has composed a reply
with its own context (vault / Honcho memory / chat transcripts). The plugin
owns the Gmail "hands"; the agent owns the words.

Tool handlers follow the real Hermes contract
(``hermes_cli/plugins.py`` -> ``PluginContext.register_tool``): a callable that
takes the LLM-supplied ``args`` dict and returns a JSON **string**, and never
raises out of the handler.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional, Protocol

logger = logging.getLogger(__name__)


class GmailDraftWriter(Protocol):
    """Creates a Gmail draft reply on a thread and returns its draft id.

    The real implementation wraps the Gmail API (``drafts.create`` with a MIME
    reply + an idempotency header) — the agent's "hands." It needs live OAuth to
    exercise, so tests inject a fake.
    """

    def create_draft(self, *, account_id: str, thread_id: str, body: str) -> str: ...


class UnconfiguredWriter:
    """Inert writer used when register() runs without live Gmail config.

    Keeps ``register(ctx)`` safe to call in tests/CI: the tool is wired, but
    invoking it without configuration fails loudly rather than silently.
    """

    def create_draft(self, *, account_id: str, thread_id: str, body: str) -> str:
        raise RuntimeError("Gmail writer not configured")


class LoggingDraftWriter:
    """Diagnostic writer: logs the composed draft instead of touching Gmail.

    Lets us observe whether the agent produces a context-rich reply and actually
    calls ``inbox_create_draft`` — without needing Gmail OAuth/Pub/Sub wired.
    """

    def create_draft(self, *, account_id: str, thread_id: str, body: str) -> str:
        logger.info(
            "inbox_create_draft PROBE account=%s thread=%s body=%r",
            account_id,
            thread_id,
            body,
        )
        return "probe-draft"


INBOX_CREATE_DRAFT_SCHEMA: dict[str, Any] = {
    "name": "inbox_create_draft",
    "description": (
        "Create a Gmail draft reply on a thread. Call this after you have "
        "composed the reply text in the user's voice. Drafts are never sent."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "account_id": {
                "type": "string",
                "description": "Connected Gmail account id.",
            },
            "thread_id": {
                "type": "string",
                "description": "Gmail thread id to reply on.",
            },
            "body": {"type": "string", "description": "The reply body text."},
        },
        "required": ["account_id", "thread_id", "body"],
    },
}


# Seam: account_id -> writer (live GmailDraftWriter, or a LoggingDraftWriter
# fallback when no account is connected). None => not connected.
ResolveWriter = Callable[[str], Optional[GmailDraftWriter]]


def make_inbox_create_draft_handler(resolve_writer: ResolveWriter):
    """Build the tool handler closure over an account->writer resolver."""

    def handler(args: dict, **_kwargs: Any) -> str:
        a = args or {}
        account_id = a.get("account_id", "")
        thread_id = a.get("thread_id", "")
        body = a.get("body", "")
        if not account_id or not thread_id or not body:
            return json.dumps(
                {"error": "account_id, thread_id and body are required"}
            )
        writer = resolve_writer(account_id)
        if writer is None:
            return json.dumps({"error": f"account not connected: {account_id!r}"})
        try:
            draft_id = writer.create_draft(
                account_id=account_id, thread_id=thread_id, body=body
            )
        except Exception as exc:  # contract: never raise out of a tool handler
            return json.dumps({"error": f"draft creation failed: {exc}"})
        return json.dumps({"ok": True, "draft_id": draft_id, "thread_id": thread_id})

    return handler
