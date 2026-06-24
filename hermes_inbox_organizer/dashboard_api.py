"""Backend logic for the Hermes dashboard plugin (UI tab).

Phase 1 is read-only: list the connected Google accounts. The *pure* functions
here take no FastAPI dependency and read the same encrypted token store the
daemon uses (via :mod:`config`), so they unit-test without Hermes, FastAPI, or
live Google — preserving the project's "tests run without Hermes" seam.

The projected ``dashboard/plugin_api.py`` (copied into ``$HERMES_HOME/plugins``
by :mod:`dashboard_assets`) is a thin wrapper that calls :func:`build_router`.
FastAPI is imported lazily inside ``build_router`` because it ships with the
Hermes dashboard process that mounts the router — not with this package.

NOTE: this runs in the *dashboard* process, separate from the gateway process
where the daemon's in-memory state lives. It only touches shared on-disk state
(the encrypted token files), so cross-process reads are safe; gateway-only
in-memory flags (e.g. needs-reconnect) are intentionally not surfaced here.
"""

from __future__ import annotations

import contextlib
import glob
import logging
import os
import re
from collections.abc import Callable
from typing import Any

from . import config, db, oauth, token_store

logger = logging.getLogger(__name__)

# In-flight dashboard connects expire after this long (matches oauth.PendingStore's
# 600s). Google's auth codes expire on their side too, so this is mostly GC.
OAUTH_PENDING_TTL_MS = 10 * 60 * 1000

_CONNECT_INSTRUCTIONS = (
    "Open the sign-in link and approve access. If Google warns the app isn't "
    "verified, choose Advanced → Continue. Then copy the code shown on the "
    "connect page and paste it below to finish."
)


def list_accounts() -> list[dict[str, Any]]:
    """The connected Gmail accounts, as ``[{"email", "scopes"}]`` sorted by email.

    Decrypts each blob in the token dir (the filename is a lossy slug, so the
    real address only comes from the decrypted token). Missing key file → ``[]``;
    an unreadable/corrupt blob is skipped (logged), never fatal.
    """
    cfg = config.get_config()
    if not os.path.exists(cfg.key_file):
        return []
    try:
        key = open(cfg.key_file).read().strip()
    except OSError:
        logger.exception("inbox-dashboard: cannot read encryption key %s", cfg.key_file)
        return []

    # Dedup by email: there can be more than one token file per account (an older
    # filename slug plus a re-connect). The daemon keys on email too (its token map
    # is a dict), so surface exactly one row per account — first file wins.
    by_email: dict[str, dict[str, Any]] = {}
    for path in sorted(glob.glob(os.path.join(cfg.token_dir, "*.json"))):
        try:
            tok = token_store.load_token(key, path)
        except Exception:
            logger.warning("inbox-dashboard: skipping unreadable token blob %s", os.path.basename(path))
            continue
        by_email.setdefault(tok.email, {"email": tok.email, "scopes": list(tok.scopes or [])})
    return [by_email[email] for email in sorted(by_email)]


def accounts_payload() -> dict[str, Any]:
    """JSON body for ``GET /api/plugins/inbox_organizer/accounts``."""
    accounts = list_accounts()
    return {"accounts": accounts, "count": len(accounts)}


# --- connect flow (copy-paste, single-user) -------------------------------------
# Mirrors the chat onboarding tools (onboarding_tools.py) but: (a) no sender/owner
# concept — the dashboard's own auth is the gate (decision #1); (b) the PKCE
# verifier is persisted in state.db, not the in-memory oauth.PendingStore, because
# this runs in the dashboard process, not the gateway/daemon one (decision #3).

def _safe_email(email: str) -> str:
    """Filename-safe slug for an account's token file (matches __init__._safe_email)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", email)


def _save_token(token: Any) -> None:
    """Encrypt + persist a freshly connected account's token (config-driven paths)."""
    cfg = config.get_config()
    key = open(cfg.key_file).read().strip()
    token_store.save_token(token, key, cfg.token_path(_safe_email(token.email)))


def connect_start(
    *, load_client: Callable[..., oauth.OAuthClient] = oauth.load_oauth_client
) -> dict[str, Any]:
    """Begin a connect: build the Google consent URL + stash the PKCE verifier in
    state.db (keyed by an opaque ``state``). Returns ``{ok, auth_url, state,
    instructions}`` or ``{error}``. ``load_client`` is a seam for tests."""
    cfg = config.get_config()
    try:
        client = load_client(cfg.oauth_client_file)
    except Exception as exc:
        return {"error": f"OAuth client not configured: {exc}"}
    verifier, challenge = oauth.new_pkce()
    state = oauth.new_state()
    try:
        with contextlib.closing(db.connect(cfg.db_path)) as conn:
            db.sweep_oauth_pending(conn, before_ms=db.now_ms() - OAUTH_PENDING_TTL_MS)
            db.create_oauth_pending(conn, state=state, verifier=verifier)
    except Exception:
        logger.exception("inbox-dashboard: failed to persist pending connect")
        return {"error": "could not start the connection (database error)"}
    return {
        "ok": True,
        "auth_url": oauth.build_auth_url(client, state=state, code_challenge=challenge),
        "state": state,
        "instructions": _CONNECT_INSTRUCTIONS,
    }


def connect_complete(
    code: str,
    state: str = "",
    *,
    load_client: Callable[..., oauth.OAuthClient] = oauth.load_oauth_client,
    exchange: Callable[..., Any] = oauth.exchange_code,
) -> dict[str, Any]:
    """Finish a connect: look up the stashed verifier by ``state``, exchange the
    pasted ``code`` for a refresh token, and persist the encrypted account. Returns
    ``{ok, email}`` or ``{error}``. The daemon (separate process) picks up the new
    account on its next reconcile tick (Phase 3) or on restart. ``load_client`` /
    ``exchange`` are seams for tests (so unit tests never reach Google)."""
    code = (code or "").strip()
    if not code:
        return {"error": "code is required (paste the code from the connect page)"}
    state = (state or "").strip()
    if not state:
        return {"error": "state is required (start the connection again)"}
    cfg = config.get_config()
    try:
        with contextlib.closing(db.connect(cfg.db_path)) as conn:
            verifier = db.take_oauth_pending(
                conn, state, ttl_ms=OAUTH_PENDING_TTL_MS, now_ms=db.now_ms()
            )
    except Exception:
        logger.exception("inbox-dashboard: failed to read pending connect")
        return {"error": "could not complete the connection (database error)"}
    if not verifier:
        return {"error": "no pending connection for that code — it may have expired; start again"}
    try:
        client = load_client(cfg.oauth_client_file)
        token = exchange(client, code=code, code_verifier=verifier)
    except Exception as exc:
        return {"error": f"could not complete the connection: {exc}"}
    try:
        _save_token(token)
    except Exception as exc:
        return {"error": f"connected but failed to store the token: {exc}"}
    return {"ok": True, "email": token.email}


def _account_token_files(email: str) -> list[tuple[str, Any]]:
    """All ``(path, AccountToken)`` pairs whose decrypted email matches.

    Filename-agnostic (matches the decrypted address, like __init__._delete_account_token)
    AND returns every match, not just the first — there can be more than one file per
    account (an older filename slug plus a re-connect), and a disconnect must remove
    them all."""
    cfg = config.get_config()
    if not os.path.exists(cfg.key_file):
        return []
    try:
        key = open(cfg.key_file).read().strip()
    except OSError:
        return []
    out: list[tuple[str, Any]] = []
    for path in sorted(glob.glob(os.path.join(cfg.token_dir, "*.json"))):
        try:
            tok = token_store.load_token(key, path)
        except Exception:
            continue
        if tok.email == email:
            out.append((path, tok))
    return out


def disconnect(
    email: str, *, revoke: Callable[[str], bool] = oauth.revoke_token
) -> dict[str, Any]:
    """Disconnect an account: revoke its refresh token at Google (best effort) +
    delete the stored blob(s). Returns ``{ok, email, revoked, deleted}`` or
    ``{error}``. Removes ALL token files for the email (handles duplicate slugs).
    The daemon (a separate process) drops it from routing on its next reconcile tick
    — this only mutates the shared token files. ``revoke`` is a seam for tests."""
    email = (email or "").strip()
    if not email:
        return {"error": "email is required"}
    matches = _account_token_files(email)
    if not matches:
        return {"error": f"no connected account found for {email!r}"}
    revoked = False
    for _path, tok in matches:
        try:
            if revoke(tok.refresh_token):
                revoked = True
        except Exception:
            pass
    deleted = 0
    for path, _tok in matches:
        try:
            os.remove(path)
            deleted += 1
        except OSError:
            logger.exception("inbox-dashboard: failed to delete token file %s", path)
    if deleted == 0:
        return {"error": "could not delete the stored token"}
    return {"ok": True, "email": email, "revoked": revoked, "deleted": True}


def build_router() -> Any:
    """Wire the pure functions above onto a FastAPI ``APIRouter``.

    Imported lazily: FastAPI lives in the Hermes dashboard process that loads the
    projected ``plugin_api.py``, not in this package's runtime deps, so importing
    this module stays FastAPI-free for unit tests.
    """
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/accounts")
    async def get_accounts() -> dict[str, Any]:  # mounted at /api/plugins/inbox_organizer/accounts
        return accounts_payload()

    @router.post("/connect/start")
    async def post_connect_start() -> dict[str, Any]:
        return connect_start()

    @router.post("/connect/complete")
    async def post_connect_complete(body: dict) -> dict[str, Any]:
        return connect_complete(code=body.get("code", ""), state=body.get("state", ""))

    @router.post("/disconnect")
    async def post_disconnect(body: dict) -> dict[str, Any]:
        return disconnect(email=body.get("email", ""))

    return router
