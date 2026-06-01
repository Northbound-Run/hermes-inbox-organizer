"""Onboarding tools: connect (auth URL + pending) and complete (exchange + store + hot-add)."""

from __future__ import annotations

import json

from hermes_inbox_organizer import oauth
from hermes_inbox_organizer.oauth import OAuthClient, PendingStore
from hermes_inbox_organizer.onboarding_tools import make_disconnect_tool, make_onboarding_tools
from hermes_inbox_organizer.token_store import AccountToken

CLIENT = OAuthClient("cid", "csec", "https://r/")


def _fake_token() -> AccountToken:
    return AccountToken("new@x.com", "r-tok", "cid", "csec", "uri", ["s"])


def _tools(**over):
    pending = over.get("pending") or PendingStore()
    deps = dict(
        load_client=over.get("load_client", lambda: CLIENT),
        pending=pending,
        resolve_sender=over.get("resolve_sender", lambda: "@matt"),
        save_token=over.get("save_token", lambda t: None),
        hot_add=over.get("hot_add", lambda t: True),
        exchange=over.get("exchange", lambda c, code, code_verifier: _fake_token()),
    )
    pairs = make_onboarding_tools(**deps)
    return {schema["name"]: handler for schema, handler in pairs}, pending


def test_connect_returns_auth_url_and_records_pending() -> None:
    tools, pending = _tools()
    out = json.loads(tools["inbox_connect_account"]({}))
    assert out["ok"] and out["auth_url"].startswith(oauth.AUTH_ENDPOINT)
    rec = pending.take_for_sender("@matt")  # a pending was stored for the sender
    assert rec is not None and rec.sender == "@matt" and rec.verifier


def test_connect_blocks_when_sender_unknown() -> None:
    tools, _ = _tools(resolve_sender=lambda: None)
    assert "error" in json.loads(tools["inbox_connect_account"]({}))


def test_complete_exchanges_saves_and_hot_adds() -> None:
    saved: list = []
    hot: list = []
    pending = PendingStore()
    pending.create(sender="@matt", verifier="ver", state="st")
    tools, _ = _tools(
        pending=pending,
        save_token=lambda t: saved.append(t),
        hot_add=lambda t: (hot.append(t), True)[1],
    )
    out = json.loads(tools["inbox_complete_connection"]({"code": "abc"}))
    assert out["ok"] and out["email"] == "new@x.com" and out["live"] is True
    assert saved and saved[0].email == "new@x.com"  # persisted (encrypted in prod)
    assert hot and hot[0].email == "new@x.com"       # hot-added to runtime
    # pending consumed → a repeat fails
    assert "error" in json.loads(tools["inbox_complete_connection"]({"code": "abc"}))


def test_complete_requires_code() -> None:
    tools, _ = _tools()
    assert "error" in json.loads(tools["inbox_complete_connection"]({}))


def test_complete_no_pending_errors() -> None:
    tools, _ = _tools()
    assert "error" in json.loads(tools["inbox_complete_connection"]({"code": "abc"}))


def test_complete_exchange_failure_is_caught() -> None:
    pending = PendingStore()
    pending.create(sender="@matt", verifier="v", state="s")

    def boom(c, code, code_verifier):
        raise ValueError("bad code")

    tools, _ = _tools(pending=pending, exchange=boom)
    out = json.loads(tools["inbox_complete_connection"]({"code": "x"}))
    assert "error" in out and "could not complete" in out["error"]


def test_complete_saved_even_if_hot_add_fails() -> None:
    saved: list = []
    pending = PendingStore()
    pending.create(sender="@matt", verifier="v", state="s")

    def hot_boom(t):
        raise RuntimeError("runtime not running")

    tools, _ = _tools(pending=pending, save_token=lambda t: saved.append(t), hot_add=hot_boom)
    out = json.loads(tools["inbox_complete_connection"]({"code": "x"}))
    assert out["ok"] and out["live"] is False  # token saved; live add failed gracefully
    assert saved and saved[0].email == "new@x.com"


def _disc(**over):
    _, handler = make_disconnect_tool(
        resolve_sender=over.get("resolve_sender", lambda: "@matt"),
        load_token=over.get("load_token", lambda e: _fake_token()),
        delete_token=over.get("delete_token", lambda e: True),
        remove_account=over.get("remove_account", lambda e: True),
        revoke=over.get("revoke", lambda r: True),
    )
    return handler


def test_disconnect_revokes_deletes_and_removes() -> None:
    seen = {}
    h = _disc(
        revoke=lambda r: (seen.update(revoked=r), True)[1],
        delete_token=lambda e: (seen.update(deleted=e), True)[1],
        remove_account=lambda e: (seen.update(removed=e), True)[1],
    )
    out = json.loads(h({"email": "new@x.com"}))
    assert out["ok"] and out["email"] == "new@x.com"
    assert out["revoked"] and out["deleted"] and out["removed"]
    assert seen["revoked"] == "r-tok"  # revoked the token's refresh_token
    assert seen["deleted"] == "new@x.com" and seen["removed"] == "new@x.com"


def test_disconnect_requires_email() -> None:
    assert "error" in json.loads(_disc()({}))


def test_disconnect_not_found_errors() -> None:
    h = _disc(load_token=lambda e: None, delete_token=lambda e: False, remove_account=lambda e: False)
    assert "error" in json.loads(h({"email": "ghost@x.com"}))


def test_disconnect_blocked_when_sender_unknown() -> None:
    assert "error" in json.loads(_disc(resolve_sender=lambda: None)({"email": "x@y.com"}))
