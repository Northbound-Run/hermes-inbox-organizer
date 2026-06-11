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
    wake_timeout_s: int  # INBOX_WAKE_TIMEOUT_S (wake POST timeout; default 300)
    draft_research_enabled: bool  # INBOX_DRAFT_RESEARCH (brief encourages research; default on)
    # Notifications (proactive push to the owner — see notifier.py).
    # OPTIONAL override; default destination is Hermes's /sethome home channel.
    notify_target: Optional[str]  # INBOX_NOTIFY_TARGET: a room/chat id, e.g. "!room:server"
    # Core label system (the 8 numbered labels + archiving). Off = the plugin
    # never mutates the mailbox: no label creation/apply, no archiving, no
    # sent-handler thread moves. Classification, DB persistence, module
    # dispatch, and To-Respond draft wakes all still run.
    labels_enabled: bool  # INBOX_LABELS_ENABLED (default on)
    # Modules
    module_2fa_enabled: bool  # INBOX_2FA_ENABLED (default on)
    twofa_sender_allowlist: frozenset  # INBOX_2FA_SENDER_ALLOWLIST (lowercased addrs/domains; empty = push all)
    module_shipping_enabled: bool  # INBOX_SHIPPING_ENABLED (default on)
    track17_key_file: str  # config-mount path to the 17track API key
    shipping_poll_interval_s: int  # INBOX_SHIPPING_POLL_INTERVAL_S (default 4h)
    shipping_max_active: int  # INBOX_SHIPPING_MAX_ACTIVE (cap on concurrently-tracked parcels)
    # Sender-profile backfill (Phase 2): seed voice profiles from sent mail.
    backfill_on_start: bool  # INBOX_BACKFILL_ON_START (default on)
    backfill_max_senders: int  # INBOX_BACKFILL_MAX_SENDERS (top-N recipients to profile)
    backfill_sample_per_sender: int  # INBOX_BACKFILL_SAMPLE_PER_SENDER (sent msgs sampled/sender)
    # Draft reinforcement feedback loop: learn from draft→sent deltas.
    draft_feedback_enabled: bool  # INBOX_DRAFT_FEEDBACK_ENABLED (default on)
    draft_feedback_capture_all_sent: bool  # INBOX_DRAFT_FEEDBACK_CAPTURE_ALL_SENT (default on; seeds gold-example pool for non-drafted threads)
    draft_feedback_no_reply_hours: int  # INBOX_DRAFT_FEEDBACK_NO_REPLY_HOURS (window before a pending outcome is marked no_reply; default 72)
    draft_feedback_sweep_interval_s: int  # INBOX_DRAFT_FEEDBACK_SWEEP_INTERVAL_S (default 6 h)
    draft_feedback_max_examples: int  # INBOX_DRAFT_FEEDBACK_MAX_EXAMPLES (gold examples injected into brief; default 3)
    draft_feedback_max_lessons: int  # INBOX_DRAFT_FEEDBACK_MAX_LESSONS (lessons injected into brief; default 8)
    draft_feedback_retention_days: int  # INBOX_DRAFT_FEEDBACK_RETENTION_DAYS (prune learned outcomes older than N days; default 90; 0 = off)
    draft_feedback_verbatim_threshold: int  # INBOX_DRAFT_FEEDBACK_VERBATIM_THRESHOLD (similarity >= N → sent_verbatim; default 92)
    draft_feedback_edit_threshold: int  # INBOX_DRAFT_FEEDBACK_EDIT_THRESHOLD (similarity >= N → sent_edited; else sent_ignored; default 45)

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


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


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
        wake_timeout_s=_env_int("INBOX_WAKE_TIMEOUT_S", 300),
        draft_research_enabled=_env_bool("INBOX_DRAFT_RESEARCH", True),
        notify_target=_env("INBOX_NOTIFY_TARGET"),
        labels_enabled=_env_bool("INBOX_LABELS_ENABLED", True),
        module_2fa_enabled=_env_bool("INBOX_2FA_ENABLED", True),
        twofa_sender_allowlist=frozenset(twofa_allow),
        module_shipping_enabled=_env_bool("INBOX_SHIPPING_ENABLED", True),
        track17_key_file=cfg("INBOX_17TRACK_KEY_FILE", "inbox-17track-key"),
        shipping_poll_interval_s=_env_int("INBOX_SHIPPING_POLL_INTERVAL_S", 4 * 3600),
        shipping_max_active=_env_int("INBOX_SHIPPING_MAX_ACTIVE", 50),
        backfill_on_start=_env_bool("INBOX_BACKFILL_ON_START", True),
        backfill_max_senders=_env_int("INBOX_BACKFILL_MAX_SENDERS", 50),
        backfill_sample_per_sender=_env_int("INBOX_BACKFILL_SAMPLE_PER_SENDER", 15),
        draft_feedback_enabled=_env_bool("INBOX_DRAFT_FEEDBACK_ENABLED", True),
        draft_feedback_capture_all_sent=_env_bool("INBOX_DRAFT_FEEDBACK_CAPTURE_ALL_SENT", True),
        draft_feedback_no_reply_hours=_env_int("INBOX_DRAFT_FEEDBACK_NO_REPLY_HOURS", 72),
        draft_feedback_sweep_interval_s=_env_int("INBOX_DRAFT_FEEDBACK_SWEEP_INTERVAL_S", 6 * 3600),
        draft_feedback_max_examples=_env_int("INBOX_DRAFT_FEEDBACK_MAX_EXAMPLES", 3),
        draft_feedback_max_lessons=_env_int("INBOX_DRAFT_FEEDBACK_MAX_LESSONS", 8),
        draft_feedback_retention_days=_env_int("INBOX_DRAFT_FEEDBACK_RETENTION_DAYS", 90),
        draft_feedback_verbatim_threshold=_env_int("INBOX_DRAFT_FEEDBACK_VERBATIM_THRESHOLD", 92),
        draft_feedback_edit_threshold=_env_int("INBOX_DRAFT_FEEDBACK_EDIT_THRESHOLD", 45),
    )


def reset_config() -> None:
    """Drop the cached Config so the next get_config() re-reads the environment."""
    get_config.cache_clear()
