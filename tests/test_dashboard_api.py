"""Dashboard backend — read-only account listing + the copy-paste connect flow.

Exercises the pure logic (no FastAPI / Hermes / live Google) that the projected
``dashboard/plugin_api.py`` wraps via ``build_router()``. The OAuth exchange is
injected so unit tests never reach Google.
"""

from __future__ import annotations

import contextlib

import pytest

from hermes_inbox_organizer import config, crypto, dashboard_api, db, oauth
from hermes_inbox_organizer.token_store import AccountToken, save_token


@pytest.fixture(autouse=True)
def _reset_cfg():
    # get_config() is lru_cached; clear it around each test so env tweaks take and
    # don't bleed the temp paths into other modules' tests.
    config.reset_config()
    yield
    config.reset_config()


def _paths(tmp_path, monkeypatch):
    """Point key/token/db at tmp; return the encryption key hex."""
    key = crypto.generate_key()
    key_file = tmp_path / "key"
    key_file.write_text(key)
    monkeypatch.setenv("INBOX_KEY_FILE", str(key_file))
    monkeypatch.setenv("INBOX_TOKEN_DIR", str(tmp_path / "accounts"))
    monkeypatch.setenv("INBOX_DB_PATH", str(tmp_path / "state.db"))
    config.reset_config()
    return key


def _seed(tmp_path, monkeypatch, emails):
    key = _paths(tmp_path, monkeypatch)
    token_dir = tmp_path / "accounts"
    for i, em in enumerate(emails):
        tok = AccountToken(
            email=em,
            refresh_token="r",
            client_id="c",
            client_secret="s",
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/gmail.modify"],
        )
        save_token(tok, key, str(token_dir / f"acct{i}.json"))
    return key, token_dir


def _client(_path=None):
    return oauth.OAuthClient(
        client_id="c.apps", client_secret="s", redirect_uri="https://inbox-organizer.northbound.run/"
    )


def _should_not_call(*_a, **_k):
    raise AssertionError("exchange must not be called when there is no pending state")


# ── accounts listing ──────────────────────────────────────────────────────────

def test_lists_connected_accounts_sorted(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, ["b@gmail.com", "a@example.com"])
    payload = dashboard_api.accounts_payload()
    assert payload["count"] == 2
    assert [a["email"] for a in payload["accounts"]] == ["a@example.com", "b@gmail.com"]
    assert payload["accounts"][0]["scopes"] == ["https://www.googleapis.com/auth/gmail.modify"]


def test_empty_when_key_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("INBOX_KEY_FILE", str(tmp_path / "nope"))
    monkeypatch.setenv("INBOX_TOKEN_DIR", str(tmp_path / "accounts"))
    config.reset_config()
    assert dashboard_api.accounts_payload() == {"accounts": [], "count": 0}


def test_corrupt_blob_is_skipped(tmp_path, monkeypatch):
    _key, token_dir = _seed(tmp_path, monkeypatch, ["good@gmail.com"])
    (token_dir / "corrupt.json").write_text("not-a-valid-blob")
    payload = dashboard_api.accounts_payload()
    assert payload["count"] == 1
    assert payload["accounts"][0]["email"] == "good@gmail.com"


# ── connect flow ──────────────────────────────────────────────────────────────

def test_connect_start_returns_auth_url_and_persists_pending(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    out = dashboard_api.connect_start(load_client=_client)
    assert out["ok"] is True
    assert "accounts.google.com" in out["auth_url"]
    assert out["state"] and out["state"] in out["auth_url"]
    with contextlib.closing(db.connect(config.get_config().db_path)) as conn:
        row = conn.execute(
            "SELECT verifier FROM oauth_pending WHERE state = ?", (out["state"],)
        ).fetchone()
    assert row is not None and row["verifier"]


def test_connect_complete_exchanges_saves_and_lists(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    start = dashboard_api.connect_start(load_client=_client)
    seen = {}

    def fake_exchange(client, *, code, code_verifier):
        seen["code"] = code
        seen["verifier"] = code_verifier
        return AccountToken(
            email="new@gmail.com",
            refresh_token="r",
            client_id="c",
            client_secret="s",
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/gmail.modify"],
        )

    out = dashboard_api.connect_complete(
        code="the-code", state=start["state"], load_client=_client, exchange=fake_exchange
    )
    assert out == {"ok": True, "email": "new@gmail.com"}
    assert seen["code"] == "the-code" and seen["verifier"]
    # token persisted → now visible in the account list
    assert [a["email"] for a in dashboard_api.accounts_payload()["accounts"]] == ["new@gmail.com"]
    # pending consumed (single-use)
    with contextlib.closing(db.connect(config.get_config().db_path)) as conn:
        assert conn.execute(
            "SELECT 1 FROM oauth_pending WHERE state = ?", (start["state"],)
        ).fetchone() is None


def test_connect_complete_rejects_unknown_state(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    out = dashboard_api.connect_complete(
        code="x", state="nope", load_client=_client, exchange=_should_not_call
    )
    assert "no pending connection" in out["error"]


def test_connect_complete_requires_code(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    out = dashboard_api.connect_complete(code="   ", state="whatever")
    assert "code is required" in out["error"]


# ── disconnect flow ───────────────────────────────────────────────────────────

def test_disconnect_revokes_and_deletes(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, ["gone@gmail.com", "stay@gmail.com"])
    revoked = []
    out = dashboard_api.disconnect("gone@gmail.com", revoke=lambda rt: revoked.append(rt) or True)
    assert out == {"ok": True, "email": "gone@gmail.com", "revoked": True, "deleted": True}
    assert revoked == ["r"]  # the seeded refresh_token was revoked
    # only the other account remains on disk
    assert [a["email"] for a in dashboard_api.accounts_payload()["accounts"]] == ["stay@gmail.com"]


def test_disconnect_unknown_email_errors(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, ["only@gmail.com"])
    out = dashboard_api.disconnect("nobody@gmail.com", revoke=_should_not_call)
    assert "no connected account found" in out["error"]


def test_disconnect_requires_email(tmp_path, monkeypatch):
    _paths(tmp_path, monkeypatch)
    assert dashboard_api.disconnect("   ")["error"] == "email is required"


def test_list_and_disconnect_dedup_multiple_files_per_email(tmp_path, monkeypatch):
    # Two token FILES for the same account (an old slug + a re-connect). The daemon
    # keys on email, so the dashboard should show ONE row and disconnect should
    # remove BOTH files.
    key = _paths(tmp_path, monkeypatch)
    token_dir = tmp_path / "accounts"

    def _write(name):
        save_token(
            AccountToken(
                email="dup@gmail.com", refresh_token="r", client_id="c", client_secret="s",
                token_uri="https://oauth2.googleapis.com/token",
                scopes=["https://www.googleapis.com/auth/gmail.modify"],
            ),
            key, str(token_dir / name),
        )

    _write("dup_a.json")
    _write("dup_b.json")

    payload = dashboard_api.accounts_payload()
    assert payload["count"] == 1 and payload["accounts"][0]["email"] == "dup@gmail.com"

    revoked = []
    out = dashboard_api.disconnect("dup@gmail.com", revoke=lambda rt: revoked.append(rt) or True)
    assert out["ok"] is True and out["revoked"] is True
    assert len(revoked) == 2  # revoked each file's token
    assert dashboard_api.accounts_payload()["count"] == 0  # both files removed


def test_build_router_exposes_routes():
    # FastAPI isn't a runtime dep — only assert the wiring where it's installed.
    pytest.importorskip("fastapi")
    router = dashboard_api.build_router()
    paths = {r.path for r in router.routes}
    assert {"/accounts", "/connect/start", "/connect/complete", "/disconnect"} <= paths
