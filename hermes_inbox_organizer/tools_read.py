"""Read tools the agent calls to inspect the inbox before drafting.

Each handler resolves a ``GmailReader`` for the account (seam), formats the
result as a JSON string, and never raises — per the Hermes tool contract.
Until OAuth/connect lands, the resolver returns ``None`` and the tools return a
clear "account not connected" message rather than failing.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from .gmail import ResolveReader, parse_message


def _err(message: str) -> str:
    return json.dumps({"error": message})


def _need_connect(account_id: str) -> str:
    return _err(f"account not connected: {account_id!r} — connect it first")


INBOX_GET_THREAD_SCHEMA: dict[str, Any] = {
    "name": "inbox_get_thread",
    "description": "Read a Gmail thread (all messages, headers + plaintext) so you can draft a reply.",
    "parameters": {
        "type": "object",
        "properties": {
            "account_id": {"type": "string", "description": "Connected Gmail account id."},
            "thread_id": {"type": "string", "description": "Gmail thread id."},
        },
        "required": ["account_id", "thread_id"],
    },
}

INBOX_LIST_EMAILS_SCHEMA: dict[str, Any] = {
    "name": "inbox_list_emails",
    "description": (
        "Search Gmail across the connected inboxes. Accepts ANY Gmail search query — "
        "the same syntax as the Gmail search box: from:, to:, cc:, subject:, "
        "\"exact phrase\", after:/before:/newer_than:/older_than:, has:attachment, "
        "filename:, label:, is:unread, is:starred, in:anywhere, and OR / (). "
        "By DEFAULT searches ALL connected accounts; pass account_id to scope to one. "
        "Returns matching messages (from/subject/date/snippet, each tagged with its "
        "account) — use inbox_get_thread or inbox_get_email for the full body."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Gmail search query, e.g. 'from:bob subject:invoice newer_than:30d'.",
                "default": "in:inbox",
            },
            "account_id": {
                "type": "string",
                "description": "Optional connected Gmail account id (email). Omit to search ALL connected accounts.",
            },
            "max_results": {"type": "integer", "description": "Max messages per account (default 20).", "default": 20},
        },
    },
}

INBOX_GET_EMAIL_SCHEMA: dict[str, Any] = {
    "name": "inbox_get_email",
    "description": "Read a single Gmail message (headers + plaintext body).",
    "parameters": {
        "type": "object",
        "properties": {
            "account_id": {"type": "string", "description": "Connected Gmail account id."},
            "message_id": {"type": "string", "description": "Gmail message id."},
        },
        "required": ["account_id", "message_id"],
    },
}


def make_inbox_get_thread_handler(resolve_reader: ResolveReader):
    def handler(args: dict, **_kwargs: Any) -> str:
        a = args or {}
        account_id, thread_id = a.get("account_id"), a.get("thread_id")
        if not account_id or not thread_id:
            return _err("account_id and thread_id are required")
        reader = resolve_reader(account_id)
        if reader is None:
            return _need_connect(account_id)
        try:
            thread = reader.get_thread(thread_id)
        except Exception as exc:
            return _err(f"get_thread failed: {exc}")
        # Deeper read for drafting: 8000 chars/message. parse_message's shared default
        # (4000, used by the classifier + rollup) is left untouched — call-site only.
        messages = [parse_message(m, body_limit=8000) for m in thread.get("messages", []) or []]
        return json.dumps({"thread_id": thread_id, "messages": messages})

    return handler


def make_inbox_list_emails_handler(
    resolve_reader: ResolveReader, list_accounts: Callable[[], list[str]]
):
    """Search Gmail across one account (account_id) or ALL connected accounts (default).

    Fetches matches in metadata form (headers + snippet, no body) so a multi-inbox
    search stays light; each result is tagged with its ``account``. Per-account
    failures are isolated into ``errors`` and never abort the whole search.
    """

    def handler(args: dict, **_kwargs: Any) -> str:
        a = args or {}
        query = a.get("query") or "in:inbox"
        try:
            max_results = int(a.get("max_results") or 20)
        except (TypeError, ValueError):
            max_results = 20
        account_id = a.get("account_id") or None
        if account_id:
            targets = [account_id]
        else:
            try:
                targets = list(list_accounts())
            except Exception as exc:
                return _err(f"failed to list accounts: {exc}")
            if not targets:
                return _err("no accounts connected")
        messages: list[dict] = []
        errors: list[dict] = []
        for email in targets:
            reader = resolve_reader(email)
            if reader is None:
                errors.append({"account": email, "error": "not connected"})
                continue
            try:
                refs = reader.list_messages(query, max_results)
                for r in refs:
                    if r.get("id"):
                        msg = parse_message(reader.get_message(r["id"], format="metadata"))
                        msg["account"] = email
                        messages.append(msg)
            except Exception as exc:
                errors.append({"account": email, "error": str(exc)})
        out: dict[str, Any] = {
            "query": query,
            "accounts_searched": targets,
            "count": len(messages),
            "messages": messages,
        }
        if errors:
            out["errors"] = errors
        return json.dumps(out)

    return handler


def make_inbox_get_email_handler(resolve_reader: ResolveReader):
    def handler(args: dict, **_kwargs: Any) -> str:
        a = args or {}
        account_id, message_id = a.get("account_id"), a.get("message_id")
        if not account_id or not message_id:
            return _err("account_id and message_id are required")
        reader = resolve_reader(account_id)
        if reader is None:
            return _need_connect(account_id)
        try:
            msg = reader.get_message(message_id)
        except Exception as exc:
            return _err(f"get_email failed: {exc}")
        return json.dumps(parse_message(msg))

    return handler


# inbox_list_emails is registered separately in __init__ (it needs the all-accounts
# lister for multi-inbox search) — intentionally not in this generic resolver list.
READ_TOOLS = [
    (INBOX_GET_THREAD_SCHEMA, make_inbox_get_thread_handler),
    (INBOX_GET_EMAIL_SCHEMA, make_inbox_get_email_handler),
]
