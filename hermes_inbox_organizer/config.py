"""Single source of truth for plugin configuration.

Every ``INBOX_*`` knob the plugin reads is resolved here, once, instead of
scattering ``os.environ.get("INBOX_…", "/opt/data/…")`` (with duplicated
hard-coded defaults) across the modules. Paths default under two roots:

* ``INBOX_CONFIG_DIR`` (default ``/opt/data/config``) — the READ-ONLY config
  mount: secrets the operator installs (encryption key, OAuth client, Pub/Sub
  service-account key).
* ``INBOX_DATA_DIR`` (default ``/opt/data/inbox-organizer``) — the WRITABLE
  data dir in the Hermes volume: the SQLite DB, per-account token files.

Both live under Hermes's ``HERMES_HOME`` (``/opt/data``), so everything persists
across restarts / ``docker compose up``. Resolved once and cached; call
``reset_config()`` in tests that monkeypatch the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional


@dataclass(frozen=True)
class Config:
    # Roots
    config_dir: str
    data_dir: str
    # Persistence
    db_path: str
    token_dir: str
    # Secrets (in the read-only config mount)
    key_file: str
    oauth_client_file: str
    sa_key_file: str
    pubsub_config_file: str
    # OAuth / onboarding
    oauth_redirect_uri: Optional[str]
    owner_matrix_ids: frozenset
    # LLM / wake
    classifier_model: str
    wake_model: Optional[str]
    hermes_api_url: Optional[str]
    api_server_key: Optional[str]
    # Notifications (proactive push to the owner — see notifier.py).
    # OPTIONAL override; default destination is Hermes's /sethome home channel.
    notify_target: Optional[str]  # INBOX_NOTIFY_TARGET: a room/chat id, e.g. "!room:server"
    # Modules
    module_2fa_enabled: bool  # INBOX_2FA_ENABLED (default on)
    twofa_sender_allowlist: frozenset  # INBOX_2FA_SENDER_ALLOWLIST (lowercased addrs/domains; empty = push all)

    def token_path(self, safe_email: str) -> str:
        """Path to an account's encrypted token file (``accounts/<email>.json``)."""
        return os.path.join(self.token_dir, f"{safe_email}.json")


def _env(name: str) -> Optional[str]:
    val = os.environ.get(name)
    return val if val else None


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@lru_cache(maxsize=1)
def get_config() -> Config:
    config_dir = os.environ.get("INBOX_CONFIG_DIR", "/opt/data/config")
    data_dir = os.environ.get("INBOX_DATA_DIR", "/opt/data/inbox-organizer")

    def cfg(name: str, filename: str) -> str:
        return os.environ.get(name) or os.path.join(config_dir, filename)

    owners = {s.strip() for s in os.environ.get("INBOX_OWNER_MATRIX_IDS", "").split(",") if s.strip()}
    twofa_allow = {
        s.strip().lower()
        for s in os.environ.get("INBOX_2FA_SENDER_ALLOWLIST", "").split(",")
        if s.strip()
    }

    return Config(
        config_dir=config_dir,
        data_dir=data_dir,
        db_path=os.environ.get("INBOX_DB_PATH") or os.path.join(data_dir, "state.db"),
        token_dir=os.environ.get("INBOX_TOKEN_DIR") or os.path.join(data_dir, "accounts"),
        key_file=cfg("INBOX_KEY_FILE", "inbox-encryption-key"),
        oauth_client_file=cfg("INBOX_OAUTH_CLIENT_FILE", "inbox-oauth-client.json"),
        sa_key_file=cfg("INBOX_PUBSUB_SA_FILE", "inbox-pubsub-sa.json"),
        pubsub_config_file=cfg("INBOX_PUBSUB_CONFIG_FILE", "inbox-pubsub.json"),
        oauth_redirect_uri=_env("INBOX_OAUTH_REDIRECT_URI"),
        owner_matrix_ids=frozenset(owners),
        classifier_model=os.environ.get("INBOX_CLASSIFIER_MODEL", "google/gemini-2.5-flash-lite"),
        wake_model=_env("INBOX_WAKE_MODEL"),
        hermes_api_url=_env("HERMES_API_URL"),
        api_server_key=_env("API_SERVER_KEY"),
        notify_target=_env("INBOX_NOTIFY_TARGET"),
        module_2fa_enabled=_env_bool("INBOX_2FA_ENABLED", True),
        twofa_sender_allowlist=frozenset(twofa_allow),
    )


def reset_config() -> None:
    """Drop the cached Config so the next get_config() re-reads the environment."""
    get_config.cache_clear()
