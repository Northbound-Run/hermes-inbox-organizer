"""Conversational account-onboarding tools the agent calls.

``inbox_connect_account`` hands back a Google sign-in link; the user approves and
pastes the code from the connect page; ``inbox_complete_connection`` exchanges it
(against the server-held PKCE verifier), stores the encrypted token, and hot-adds
the account to the running runtime. Owner-gating is enforced upstream in the
``pre_tool_call`` hook (see ``__init__``); these handlers assume an authorized
caller and resolve *which* sender via the injected ``resolve_sender`` seam.

Per the Hermes tool contract: handlers take the LLM ``args`` dict and return a
JSON **string**, and never raise.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

from . import oauth
from .token_store import AccountToken

INBOX_CONNECT_ACCOUNT_SCHEMA: dict[str, Any] = {
    "name": "inbox_connect_account",
    "description": (
        "Start connecting a Gmail account. Returns a Google sign-in link — give it "
        "to the user and ask them to approve, then paste back the code shown on the "
        "page. Follow up with inbox_complete_connection using that code."
    ),
    "parameters": {"type": "object", "properties": {}},
}

INBOX_COMPLETE_CONNECTION_SCHEMA: dict[str, Any] = {
    "name": "inbox_complete_connection",
    "description": (
        "Finish connecting a Gmail account using the code the user pasted from the "
        "connect page. Call this after inbox_connect_account."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "The code the user pasted from the connect page."}
        },
        "required": ["code"],
    },
}

INBOX_DISCONNECT_ACCOUNT_SCHEMA: dict[str, Any] = {
    "name": "inbox_disconnect_account",
    "description": (
        "Disconnect a Gmail account: revoke its access at Google, delete the stored "
        "token, and stop triaging it. Use the account's email address."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "email": {"type": "string", "description": "The connected account email to disconnect."}
        },
        "required": ["email"],
    },
}

# Seams resolved in __init__ at register() time.
ResolveSender = Callable[[], Optional[str]]
SaveToken = Callable[[AccountToken], None]
HotAdd = Callable[[AccountToken], bool]
Exchange = Callable[..., AccountToken]


def make_onboarding_tools(
    *,
    load_client: Callable[[], oauth.OAuthClient],
    pending: oauth.PendingStore,
    resolve_sender: ResolveSender,
    save_token: SaveToken,
    hot_add: HotAdd,
    exchange: Exchange = oauth.exchange_code,
) -> list[tuple[dict, Callable]]:
    """Build (schema, handler) pairs for the two onboarding tools."""

    def connect_handler(args: dict, **_kw: Any) -> str:
        sender = resolve_sender()
        if not sender:
            return json.dumps({"error": "could not determine the requesting user"})
        try:
            client = load_client()
        except Exception as exc:
            return json.dumps({"error": f"OAuth client not configured: {exc}"})
        verifier, challenge = oauth.new_pkce()
        state = oauth.new_state()
        pending.create(sender=sender, verifier=verifier, state=state)
        url = oauth.build_auth_url(client, state=state, code_challenge=challenge)
        return json.dumps(
            {
                "ok": True,
                "auth_url": url,
                "instructions": (
                    "Open this link and approve access. You may see a Google "
                    '"hasn\'t verified this app" screen — that is expected; choose '
                    "Advanced → Continue. Then copy the code shown on the page and "
                    "paste it back here to finish connecting."
                ),
            }
        )

    def complete_handler(args: dict, **_kw: Any) -> str:
        a = args or {}
        code = (a.get("code") or "").strip()
        if not code:
            return json.dumps({"error": "code is required (the code shown on the connect page)"})
        sender = resolve_sender()
        if not sender:
            return json.dumps({"error": "could not determine the requesting user"})
        state = (a.get("state") or "").strip()
        rec = pending.take_by_state(state) if state else pending.take_for_sender(sender)
        if rec is None:
            return json.dumps(
                {"error": "no pending connection — run inbox_connect_account first (or it expired)"}
            )
        if rec.sender != sender:  # defense in depth: pending must belong to this sender
            return json.dumps({"error": "that pending connection doesn't belong to you"})
        try:
            client = load_client()
            token = exchange(client, code=code, code_verifier=rec.verifier)
        except Exception as exc:
            return json.dumps({"error": f"could not complete connection: {exc}"})
        try:
            save_token(token)
        except Exception as exc:
            return json.dumps({"error": f"connected but failed to store the token: {exc}"})
        live = False
        try:
            live = bool(hot_add(token))
        except Exception:
            live = False  # token is saved; it'll be picked up on next restart
        return json.dumps({"ok": True, "email": token.email, "live": live})

    return [
        (INBOX_CONNECT_ACCOUNT_SCHEMA, connect_handler),
        (INBOX_COMPLETE_CONNECTION_SCHEMA, complete_handler),
    ]


def make_disconnect_tool(
    *,
    resolve_sender: ResolveSender,
    load_token: Callable[[str], Optional[AccountToken]],
    delete_token: Callable[[str], bool],
    remove_account: Callable[[str], bool],
    revoke: Callable[[str], bool] = oauth.revoke_token,
) -> tuple[dict, Callable]:
    """Build the (schema, handler) for inbox_disconnect_account."""

    def handler(args: dict, **_kw: Any) -> str:
        a = args or {}
        email = (a.get("email") or "").strip()
        if not email:
            return json.dumps({"error": "email is required"})
        if not resolve_sender():
            return json.dumps({"error": "could not determine the requesting user"})
        revoked = False
        try:
            tok = load_token(email)
            if tok is not None:
                revoked = bool(revoke(tok.refresh_token))
        except Exception:
            revoked = False
        try:
            deleted = bool(delete_token(email))
        except Exception:
            deleted = False
        try:
            removed = bool(remove_account(email))
        except Exception:
            removed = False
        if not (deleted or removed):
            return json.dumps({"error": f"no connected account found for {email!r}"})
        return json.dumps(
            {"ok": True, "email": email, "revoked": revoked, "deleted": deleted, "removed": removed}
        )

    return (INBOX_DISCONNECT_ACCOUNT_SCHEMA, handler)
