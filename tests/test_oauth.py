"""OAuth plumbing: client config, PKCE, auth URL, code exchange, pending store."""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse

import pytest

from hermes_inbox_organizer import oauth
from hermes_inbox_organizer.oauth import OAuthClient, PendingStore

CLIENT = OAuthClient(client_id="cid.apps", client_secret="csecret", redirect_uri="https://x.run/")


def test_new_pkce_is_s256_of_verifier() -> None:
    verifier, challenge = oauth.new_pkce()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    assert "=" not in verifier and "=" not in challenge  # base64url, unpadded


def test_build_auth_url_has_required_params() -> None:
    url = oauth.build_auth_url(CLIENT, state="st-123", code_challenge="chal")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert url.startswith(oauth.AUTH_ENDPOINT + "?")
    assert q["response_type"] == "code"
    assert q["client_id"] == "cid.apps"
    assert q["redirect_uri"] == "https://x.run/"
    assert q["scope"] == "https://www.googleapis.com/auth/gmail.modify"
    assert q["access_type"] == "offline"
    assert q["prompt"] == "consent"
    assert q["code_challenge"] == "chal"
    assert q["code_challenge_method"] == "S256"
    assert q["state"] == "st-123"


def test_load_oauth_client_flat_shape(tmp_path) -> None:
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"client_id": "a", "client_secret": "b", "redirect_uri": "https://r/"}))
    c = oauth.load_oauth_client(str(p))
    assert (c.client_id, c.client_secret, c.redirect_uri) == ("a", "b", "https://r/")


def test_load_oauth_client_google_web_shape_with_env_redirect(tmp_path, monkeypatch) -> None:
    p = tmp_path / "client_secret.json"
    p.write_text(json.dumps({"web": {"client_id": "a", "client_secret": "b", "redirect_uris": ["https://old/"]}}))
    monkeypatch.setenv("INBOX_OAUTH_REDIRECT_URI", "https://inbox-organizer.northbound.run/")
    c = oauth.load_oauth_client(str(p))
    assert c.client_id == "a" and c.redirect_uri == "https://inbox-organizer.northbound.run/"


def test_load_oauth_client_missing_fields_raises(tmp_path) -> None:
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"client_id": "a"}))  # no secret/redirect
    with pytest.raises(ValueError):
        oauth.load_oauth_client(str(p))


def test_exchange_code_builds_account_token() -> None:
    posted = {}

    def fake_post(url, form):
        posted["url"], posted["form"] = url, form
        return {"refresh_token": "r-tok", "access_token": "a-tok"}

    def fake_get(url, bearer):
        assert bearer == "a-tok" and url == oauth.PROFILE_ENDPOINT
        return {"emailAddress": "matt@example.com"}

    tok = oauth.exchange_code(CLIENT, code="auth-code", code_verifier="ver", http_post=fake_post, http_get=fake_get)
    assert tok.email == "matt@example.com"
    assert tok.refresh_token == "r-tok"
    assert tok.client_id == "cid.apps" and tok.client_secret == "csecret"
    assert tok.scopes == oauth.SCOPES
    # the exchange posted the code + PKCE verifier + auth grant
    assert posted["form"]["code"] == "auth-code"
    assert posted["form"]["code_verifier"] == "ver"
    assert posted["form"]["grant_type"] == "authorization_code"


def test_exchange_code_without_refresh_token_raises() -> None:
    tok_resp = {"access_token": "a-tok"}  # no refresh_token
    with pytest.raises(ValueError):
        oauth.exchange_code(CLIENT, code="c", code_verifier="v", http_post=lambda u, f: tok_resp, http_get=lambda u, b: {})


def test_pending_store_take_for_sender_newest_and_consumes() -> None:
    s = PendingStore(ttl_s=600)
    s.create(sender="@matt", verifier="v1", state="st1", now=100.0)
    s.create(sender="@matt", verifier="v2", state="st2", now=200.0)  # newer
    s.create(sender="@other", verifier="v3", state="st3", now=150.0)
    got = s.take_for_sender("@matt", now=210.0)
    assert got is not None and got.verifier == "v2"  # newest for @matt
    # consumed: a second take returns the older remaining one, then None
    again = s.take_for_sender("@matt", now=210.0)
    assert again is not None and again.verifier == "v1"
    assert s.take_for_sender("@matt", now=210.0) is None
    # other sender's entry untouched
    assert s.take_by_state("st3", now=210.0).verifier == "v3"


def test_pending_store_expires_entries() -> None:
    s = PendingStore(ttl_s=600)
    s.create(sender="@matt", verifier="v", state="st", now=100.0)
    assert s.take_for_sender("@matt", now=100.0 + 601) is None  # expired


def test_revoke_token_success_via_seam() -> None:
    calls = {}

    def post(url, form):
        calls["url"], calls["form"] = url, form
        return {}

    assert oauth.revoke_token("r-tok", http_post=post) is True
    assert calls["url"] == oauth.REVOKE_ENDPOINT and calls["form"] == {"token": "r-tok"}


def test_revoke_token_failure_returns_false() -> None:
    def boom(url, form):
        raise RuntimeError("network")

    assert oauth.revoke_token("r", http_post=boom) is False


def test_revoke_token_empty_token_is_false() -> None:
    assert oauth.revoke_token("", http_post=lambda u, f: {}) is False
