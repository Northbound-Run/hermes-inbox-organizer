"""Agent + owner tools for sender profiles and the backfill.

* ``inbox_get_sender_profile`` / ``inbox_save_sender_profile`` — the agent reads +
  refines the per-correspondent voice profile while drafting. NOT owner-gated, so
  they work on a wake/draft turn (which carries no Matrix sender).
* ``inbox_backfill_profiles`` — owner-gated (added to the connect-tool gate in
  ``__init__``); seeds profiles from sent mail. Expensive (LLM over many senders),
  so it is owner-initiated, not something a draft turn can trigger.

Handlers open a short-lived ``db.connect``, return a JSON string, and never raise
(the Hermes tool contract).

TRUST: get/save are intentionally un-gated so they work on wake/draft turns (which
carry no Matrix sender). This assumes the single-owner deployment (see MEMORY) where
the only caller is the owner's own agent. If multi-tenant rooms are ever in scope,
gate ``inbox_save_sender_profile`` writes; the brief already fences stored notes as
defense-in-depth.
"""

from __future__ import annotations

import contextlib
import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


INBOX_GET_SENDER_PROFILE_SCHEMA: dict[str, Any] = {
    "name": "inbox_get_sender_profile",
    "description": (
        "Get what we know about a correspondent (how the owner writes to them, the "
        "relationship, tone) so you can draft a reply in the owner's voice. Pass the "
        "connected account email and the correspondent's email address."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "account_id": {"type": "string", "description": "Connected Gmail account id."},
            "sender_email": {"type": "string", "description": "The correspondent's email address."},
        },
        "required": ["account_id", "sender_email"],
    },
}

INBOX_SAVE_SENDER_PROFILE_SCHEMA: dict[str, Any] = {
    "name": "inbox_save_sender_profile",
    "description": (
        "Save or refine what we know about a correspondent so future replies match "
        "the owner's voice. Provide any of: relationship, voice_notes (how the owner "
        "writes to them), tone_hints, display_name. Fields you omit are left unchanged."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "account_id": {"type": "string", "description": "Connected Gmail account id."},
            "sender_email": {"type": "string", "description": "The correspondent's email address."},
            "display_name": {"type": "string"},
            "relationship": {"type": "string"},
            "voice_notes": {"type": "string"},
            "tone_hints": {"type": "string"},
        },
        "required": ["account_id", "sender_email"],
    },
}

INBOX_BACKFILL_PROFILES_SCHEMA: dict[str, Any] = {
    "name": "inbox_backfill_profiles",
    "description": (
        "Seed voice profiles from the owner's sent mail: profile the most-frequent "
        "recipients so replies match the owner's voice from the start. Owner only. "
        "Optionally pass account_id (default: all connected accounts) and force=true "
        "to re-profile senders that already have a profile."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "account_id": {"type": "string", "description": "Optional; omit for all accounts."},
            "force": {"type": "boolean", "description": "Re-profile already-known senders.", "default": False},
        },
    },
}


def _err(message: str) -> str:
    return json.dumps({"error": message})


def make_get_sender_profile_handler():
    def handler(args: dict, **_kw: Any) -> str:
        a = args or {}
        account_id = a.get("account_id", "")
        sender_email = (a.get("sender_email") or "").strip().lower()
        if not account_id or not sender_email:
            return _err("account_id and sender_email are required")
        from . import db
        from .config import get_config

        try:
            with contextlib.closing(db.connect(get_config().db_path)) as conn:
                row = db.get_sender_profile(conn, account_id, sender_email)
        except Exception as exc:  # never raise out of a tool handler
            return _err(f"profile lookup failed: {exc}")
        if row is None:
            return json.dumps({"profile": None, "sender_email": sender_email})
        return json.dumps({"profile": {k: row[k] for k in row.keys()}})

    return handler


def make_save_sender_profile_handler():
    def handler(args: dict, **_kw: Any) -> str:
        a = args or {}
        account_id = a.get("account_id", "")
        sender_email = (a.get("sender_email") or "").strip().lower()
        if not account_id or not sender_email:
            return _err("account_id and sender_email are required")
        from . import db
        from .config import get_config

        try:
            with contextlib.closing(db.connect(get_config().db_path)) as conn:
                db.upsert_sender_profile(
                    conn,
                    account=account_id,
                    sender_email=sender_email,
                    display_name=a.get("display_name"),
                    relationship=a.get("relationship"),
                    voice_notes=a.get("voice_notes"),
                    tone_hints=a.get("tone_hints"),
                    source="agent",
                )
        except Exception as exc:  # never raise out of a tool handler
            return _err(f"profile save failed: {exc}")
        return json.dumps({"ok": True, "sender_email": sender_email})

    return handler


def make_backfill_profiles_handler(run_backfill: Callable[[Any, bool], dict]):
    """``run_backfill(account_id: str | None, force: bool) -> dict`` is wired in __init__."""

    def handler(args: dict, **_kw: Any) -> str:
        a = args or {}
        account_id = a.get("account_id") or None
        force = bool(a.get("force"))
        try:
            result = run_backfill(account_id, force)
        except Exception as exc:  # never raise out of a tool handler
            return _err(f"backfill failed: {exc}")
        return json.dumps({"ok": True, "result": result})

    return handler
