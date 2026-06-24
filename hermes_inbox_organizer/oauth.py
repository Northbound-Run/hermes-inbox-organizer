"""Google OAuth (authorization-code + PKCE) for chat-based account onboarding.

The plugin can't host a public callback (api_server is loopback-only), so the
redirect target is a static page (``inbox-organizer.northbound.run``) that shows
the auth code; the user pastes it back into chat and we exchange it here. Client
creds come from a config file — the same Web OAuth client ``connect.py`` uses.

Network calls (token exchange, profile lookup) sit behind ``http_post`` /
``http_get`` seams so the logic is unit-tested without hitting Google.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from .token_store import AccountToken

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
PROFILE_ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

HttpPost = Callable[[str, dict], dict]
HttpGet = Callable[[str, str], dict]


@dataclass
class OAuthClient:
    client_id: str
    client_secret: str
    redirect_uri: str


def load_oauth_client(path: str | None = None) -> OAuthClient:
    """Load the Web OAuth client (client_id/secret/redirect_uri).

    Accepts our flat ``{client_id, client_secret, redirect_uri}`` shape or a
    Google ``client_secret.json`` (``web``/``installed`` node). ``redirect_uri``
    may be overridden via ``INBOX_OAUTH_REDIRECT_URI``.
    """
    path = path or os.environ.get(
        "INBOX_OAUTH_CLIENT_FILE", "/opt/data/config/inbox-oauth-client.json"
    )
    data = json.loads(open(path).read())
    node = data.get("web") or data.get("installed") or data
    cid = node.get("client_id")
    csec = node.get("client_secret")
    redirect = os.environ.get("INBOX_OAUTH_REDIRECT_URI") or node.get("redirect_uri")
    if not redirect:
        uris = node.get("redirect_uris") or []
        redirect = uris[0] if uris else None
    if not (cid and csec and redirect):
        raise ValueError("oauth client config missing client_id/client_secret/redirect_uri")
    return OAuthClient(client_id=cid, client_secret=csec, redirect_uri=redirect)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def new_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def new_state() -> str:
    return secrets.token_urlsafe(24)


def build_auth_url(client: OAuthClient, *, state: str, code_challenge: str) -> str:
    """Google consent URL: offline + forced consent (so we always get a refresh token)."""
    params = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": client.redirect_uri,
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)


def _default_post(url: str, form: dict) -> dict:
    body = urllib.parse.urlencode(form).encode("ascii")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _default_get(url: str, bearer: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bearer}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def exchange_code(
    client: OAuthClient,
    *,
    code: str,
    code_verifier: str,
    http_post: HttpPost | None = None,
    http_get: HttpGet | None = None,
) -> AccountToken:
    """Exchange an auth code (+PKCE verifier) for a refresh token; return an AccountToken.

    Fetches the mailbox address via Gmail ``users.getProfile`` (allowed by the
    gmail.modify scope) so the token is keyed by the real account email.
    """
    post = http_post or _default_post
    get = http_get or _default_get
    tok = post(
        TOKEN_ENDPOINT,
        {
            "code": code,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
            "redirect_uri": client.redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        },
    )
    refresh = tok.get("refresh_token")
    access = tok.get("access_token")
    if not refresh:
        raise ValueError(
            "no refresh_token in token response "
            "(need access_type=offline + prompt=consent, and a first-time grant)"
        )
    if not access:
        raise ValueError("no access_token in token response")
    email = (get(PROFILE_ENDPOINT, access) or {}).get("emailAddress")
    if not email:
        raise ValueError("could not resolve account email from getProfile")
    return AccountToken(
        email=email,
        refresh_token=refresh,
        client_id=client.client_id,
        client_secret=client.client_secret,
        token_uri=TOKEN_ENDPOINT,
        scopes=list(SCOPES),
    )


def revoke_token(refresh_token: str, *, http_post: HttpPost | None = None) -> bool:
    """Best-effort revoke a refresh token at Google. Returns True on success.

    The revoke endpoint returns an empty 200 body, so this doesn't reuse the
    JSON ``_default_post``; never raises (revocation is best-effort on disconnect).
    """
    if not refresh_token:
        return False
    if http_post is not None:
        try:
            http_post(REVOKE_ENDPOINT, {"token": refresh_token})
            return True
        except Exception:
            return False
    body = urllib.parse.urlencode({"token": refresh_token}).encode("ascii")
    req = urllib.request.Request(
        REVOKE_ENDPOINT, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:
        return False


@dataclass
class Pending:
    sender: str
    verifier: str
    state: str
    created_at: float


class PendingStore:
    """Short-lived store of in-flight connect requests, keyed by ``state``.

    Multi-user-ready: each entry records the requesting Matrix ``sender``. The
    PKCE ``verifier`` is held server-side only (never leaves the process), so the
    code the user pastes is useless to an eavesdropper.
    """

    def __init__(self, ttl_s: float = 600.0) -> None:
        self._ttl = ttl_s
        self._by_state: dict[str, Pending] = {}

    def create(self, *, sender: str, verifier: str, state: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._sweep(now)
        self._by_state[state] = Pending(sender=sender, verifier=verifier, state=state, created_at=now)

    def take_for_sender(self, sender: str, *, now: float | None = None) -> Pending | None:
        """Consume + return the newest non-expired pending for ``sender`` (or None)."""
        now = time.time() if now is None else now
        self._sweep(now)
        candidates = [p for p in self._by_state.values() if p.sender == sender]
        if not candidates:
            return None
        chosen = max(candidates, key=lambda p: p.created_at)
        return self._by_state.pop(chosen.state, None)

    def take_by_state(self, state: str, *, now: float | None = None) -> Pending | None:
        """Consume + return the pending for an exact ``state`` (or None if missing/expired)."""
        now = time.time() if now is None else now
        self._sweep(now)
        return self._by_state.pop(state, None)

    def _sweep(self, now: float) -> None:
        for state in [s for s, p in self._by_state.items() if now - p.created_at > self._ttl]:
            self._by_state.pop(state, None)
