"""SQLite persistence — connection, schema, and typed accessors.

Replaces the earlier flat-file state (``cursor-<email>.txt`` history cursors and
the ``drafted-threads.txt`` draft ledger) with one SQLite DB, and adds the
operational tables the rest of the hardening work needs (classified messages,
thread state). The DB lives at ``<INBOX_DATA_DIR>/state.db`` — inside the Hermes
``/opt/data`` volume, so it persists across restarts / ``docker compose up``.

Connection handling mirrors Hermes's own SQLite plugins (``hermes_cli/kanban_db``):
autocommit (``isolation_level=None``), ``sqlite3.Row`` rows, WAL + ``foreign_keys=ON``,
and an idempotent ``CREATE TABLE IF NOT EXISTS`` init cached per path. The
``BEGIN IMMEDIATE`` write-lock pattern kanban uses for task claiming is the same
one the future DB-backed per-account job queue/leasing will use.

The module is pure stdlib ``sqlite3`` so the unit tests run without Hermes.
Single-owner design: tables are keyed by ``account`` (the email) — there is no
users/auth table (the agent is owner-gated) and labels stay code constants.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional

from .config import get_config

_SCHEMA_VERSION = 2

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    account                   TEXT PRIMARY KEY,            -- email; the per-account key everywhere
    history_cursor            TEXT,                        -- Gmail history id (was cursor-<email>.txt)
    watch_expiration_ms       INTEGER,
    last_pull_at_ms           INTEGER,
    paused                    INTEGER NOT NULL DEFAULT 0,
    paused_reason             TEXT,
    openrouter_consent_at_ms  INTEGER,
    cost_usd_micros_today     INTEGER NOT NULL DEFAULT 0,
    cost_window_start_ms      INTEGER,
    updated_at_ms             INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS draft_requests (
    account         TEXT NOT NULL,
    thread_id       TEXT NOT NULL,
    gmail_draft_id  TEXT,                                  -- set once the draft is created (fulfilment = idempotency)
    from_addr       TEXT,                                  -- stored on claim so the retry loop can rebuild the wake
    subject         TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,            -- dispatch attempts; capped by MAX_DRAFT_ATTEMPTS
    last_attempt_ms INTEGER,                               -- last dispatch time; in-flight window = within RETRY_TTL_MS
    created_at_ms   INTEGER NOT NULL,
    PRIMARY KEY (account, thread_id)
);

CREATE TABLE IF NOT EXISTS classified_messages (
    account              TEXT NOT NULL,
    message_id           TEXT NOT NULL,
    thread_id            TEXT NOT NULL,
    from_addr            TEXT NOT NULL DEFAULT '',
    subject              TEXT NOT NULL DEFAULT '',
    category             TEXT NOT NULL,
    confidence           INTEGER NOT NULL DEFAULT 0,
    source               TEXT NOT NULL DEFAULT 'llm',      -- 'pre' | 'llm'
    llm_input_tokens     INTEGER,
    llm_output_tokens    INTEGER,
    llm_cost_usd_micros  INTEGER,
    classified_at_ms     INTEGER NOT NULL,
    PRIMARY KEY (account, message_id)
);
CREATE INDEX IF NOT EXISTS idx_cm_account_classified_at ON classified_messages (account, classified_at_ms);
CREATE INDEX IF NOT EXISTS idx_cm_account_thread        ON classified_messages (account, thread_id);

CREATE TABLE IF NOT EXISTS thread_state (
    account               TEXT NOT NULL,
    thread_id             TEXT NOT NULL,
    last_message_id       TEXT NOT NULL,
    last_category         TEXT NOT NULL,
    last_processed_at_ms  INTEGER NOT NULL,
    PRIMARY KEY (account, thread_id)
);

-- Generic once-only dedup for notifying modules (e.g. the 2FA module keys on
-- message_id so a Pub/Sub redelivery or the poll reconciler re-draining the
-- same message notifies exactly once). dedup_key MUST NOT contain a secret or
-- PII (never the 2FA code itself) — key on message_id. Mirrors the
-- draft_requests check-then-insert dedup pattern.
CREATE TABLE IF NOT EXISTS module_notified (
    module          TEXT NOT NULL,
    account         TEXT NOT NULL,
    dedup_key       TEXT NOT NULL,
    notified_at_ms  INTEGER NOT NULL,
    PRIMARY KEY (module, account, dedup_key)
);

-- Packages the shipping module is tracking via 17track. Polled (no webhooks:
-- no public ingress). ``last_notified_stage`` advances ONLY after a successful
-- push (so a failed notify re-fires next poll). ``terminal`` (delivered /
-- exception / expired) drops a parcel out of the poll set. Keyed on
-- (account, tracking_number) so repeat emails about the same parcel are no-ops.
CREATE TABLE IF NOT EXISTS tracked_packages (
    account              TEXT NOT NULL,
    tracking_number      TEXT NOT NULL,
    carrier              INTEGER,                      -- 17track carrier id; NULL = auto-detect
    registered           INTEGER NOT NULL DEFAULT 0,   -- 1 once 17track accepted it
    last_stage           TEXT,                         -- normalized stage from the last poll
    last_notified_stage  TEXT,                         -- last stage actually pushed (B3)
    terminal             INTEGER NOT NULL DEFAULT 0,   -- stop polling
    created_at_ms        INTEGER NOT NULL,
    updated_at_ms        INTEGER NOT NULL,
    PRIMARY KEY (account, tracking_number)
);
CREATE INDEX IF NOT EXISTS idx_pkg_active ON tracked_packages (registered, terminal);

-- Per-correspondent voice/relationship profile feeding the drafting brief. Keyed
-- by the normalized sender email (bare, lowercased). Seeded by the sent-mail
-- backfill (source='backfill') and refined by the agent (source='agent'). A new
-- table, so it is created via SCHEMA_SQL on the next connect (no version bump).
CREATE TABLE IF NOT EXISTS sender_profiles (
    account             TEXT NOT NULL,
    sender_email        TEXT NOT NULL,            -- normalized: bare lowercased address
    display_name        TEXT,
    relationship        TEXT,
    voice_notes         TEXT,                     -- how the owner writes to this person
    tone_hints          TEXT,
    draft_count         INTEGER NOT NULL DEFAULT 0,
    last_drafted_at_ms  INTEGER,
    source              TEXT,                      -- 'backfill' | 'agent' | 'manual'
    updated_at_ms       INTEGER NOT NULL,
    PRIMARY KEY (account, sender_email)
);
"""

_INITIALIZED_PATHS: set[str] = set()


def now_ms() -> int:
    return int(time.time() * 1000)


def default_db_path() -> Path:
    return Path(get_config().db_path)


def _apply_wal(conn: sqlite3.Connection) -> None:
    # WAL lets the daemon read while a writer holds the lock. On a WAL-incompatible
    # filesystem (NFS/SMB/FUSE) SQLite silently keeps the existing journal mode —
    # correctness is unaffected, only write concurrency degrades, so we don't fail.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= _SCHEMA_VERSION:
        return
    if version < 2:
        # v2: draft_requests grows retry/idempotency columns. Additive ALTERs so an
        # existing v1 state.db upgrades in place; a fresh DB already has them from
        # SCHEMA_SQL, so add only what's missing. Forward-only + rollback-safe (a v1
        # image sees user_version>=1 and no-ops; the extra columns go unused).
        existing = {r[1] for r in conn.execute("PRAGMA table_info(draft_requests)")}
        for col, decl in (
            ("from_addr", "TEXT"),
            ("subject", "TEXT"),
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("last_attempt_ms", "INTEGER"),
        ):
            if col not in existing:
                conn.execute(f"ALTER TABLE draft_requests ADD COLUMN {col} {decl}")
    conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")


def connect(db_path: Optional[Path | str] = None) -> sqlite3.Connection:
    """Open (and on first touch, initialize) the plugin DB.

    The first connection to a given path runs the schema + migrations; later
    connections to the same path in this process skip it via a module cache.
    """
    path = Path(db_path) if db_path is not None else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved = str(path.resolve())
    needs_init = resolved not in _INITIALIZED_PATHS

    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    _apply_wal(conn)
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")

    if needs_init:
        conn.executescript(SCHEMA_SQL)
        _migrate(conn)
        _INITIALIZED_PATHS.add(resolved)
    return conn


# ── Accounts: history cursor (replaces cursor-<email>.txt) ──────────────────────

def get_cursor(conn: sqlite3.Connection, account: str) -> Optional[str]:
    row = conn.execute(
        "SELECT history_cursor FROM accounts WHERE account = ?", (account,)
    ).fetchone()
    return row["history_cursor"] if row and row["history_cursor"] is not None else None


def set_cursor(conn: sqlite3.Connection, account: str, history_id: str) -> None:
    conn.execute(
        """INSERT INTO accounts (account, history_cursor, updated_at_ms)
                VALUES (?, ?, ?)
           ON CONFLICT(account) DO UPDATE SET history_cursor = excluded.history_cursor,
                                              updated_at_ms  = excluded.updated_at_ms""",
        (account, history_id, now_ms()),
    )


# ── Draft requests (replaces the drafted-threads.txt ledger) ────────────────────

def draft_already_requested(conn: sqlite3.Connection, account: str, thread_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM draft_requests WHERE account = ? AND thread_id = ?",
        (account, thread_id),
    ).fetchone() is not None


def mark_draft_requested(conn: sqlite3.Connection, account: str, thread_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO draft_requests (account, thread_id, created_at_ms) VALUES (?, ?, ?)",
        (account, thread_id, now_ms()),
    )


def set_draft_id(conn: sqlite3.Connection, account: str, thread_id: str, gmail_draft_id: str) -> None:
    """Record the created Gmail draft id (durable idempotency past the request mark)."""
    conn.execute(
        """INSERT INTO draft_requests (account, thread_id, gmail_draft_id, created_at_ms)
                VALUES (?, ?, ?, ?)
           ON CONFLICT(account, thread_id) DO UPDATE SET gmail_draft_id = excluded.gmail_draft_id""",
        (account, thread_id, gmail_draft_id, now_ms()),
    )


def get_draft_request(conn: sqlite3.Connection, account: str, thread_id: str) -> Optional[sqlite3.Row]:
    """The draft_requests row for a thread, or None."""
    return conn.execute(
        "SELECT * FROM draft_requests WHERE account = ? AND thread_id = ?",
        (account, thread_id),
    ).fetchone()


def claim_draft(
    conn: sqlite3.Connection,
    account: str,
    thread_id: str,
    *,
    ttl_ms: int,
    max_attempts: int,
    now_ms: int,
    from_addr: str = "",
    subject: str = "",
) -> bool:
    """Atomically claim the right to dispatch a draft for ``(account, thread_id)``.

    Returns True to exactly ONE caller per eligible window. A single conditional
    UPSERT (mirrors :func:`note_once`) so every dispatch path — the notify
    ``_dedup_wake``, the poll reconciler (also routed through ``_dedup_wake``), and
    the retry loop — races safely without relying on an external lock:

    * brand-new thread                                   -> INSERT (attempt 1) -> True
    * unfulfilled, last attempt older than ``ttl_ms``,
      still under ``max_attempts``                       -> UPDATE (attempt +1) -> True
    * fulfilled (``gmail_draft_id`` set), in-flight
      (within ``ttl_ms``), or exhausted (>= max)         -> no change          -> False

    ``from_addr``/``subject`` are captured on the first claim and refreshed only
    when a non-empty value is supplied, so the retry loop can rebuild the wake.
    """
    cur = conn.execute(
        """
        INSERT INTO draft_requests
               (account, thread_id, from_addr, subject, attempts, last_attempt_ms, created_at_ms)
             VALUES (:acct, :tid, :from_addr, :subject, 1, :now, :now)
        ON CONFLICT(account, thread_id) DO UPDATE SET
               attempts        = attempts + 1,
               last_attempt_ms = :now,
               from_addr       = COALESCE(NULLIF(excluded.from_addr, ''), draft_requests.from_addr),
               subject         = COALESCE(NULLIF(excluded.subject, ''), draft_requests.subject)
             WHERE draft_requests.gmail_draft_id IS NULL
               AND draft_requests.attempts < :max
               AND (draft_requests.last_attempt_ms IS NULL
                    OR draft_requests.last_attempt_ms <= :now - :ttl)
        """,
        {
            "acct": account, "tid": thread_id, "from_addr": from_addr, "subject": subject,
            "now": now_ms, "max": max_attempts, "ttl": ttl_ms,
        },
    )
    return cur.rowcount == 1


def unfulfilled_drafts(
    conn: sqlite3.Connection, *, ttl_ms: int, max_attempts: int, now_ms: int
) -> list[sqlite3.Row]:
    """Rows eligible for a retry: no draft yet, under max attempts, past the ttl window."""
    return conn.execute(
        """SELECT * FROM draft_requests
            WHERE gmail_draft_id IS NULL
              AND attempts < ?
              AND (last_attempt_ms IS NULL OR last_attempt_ms <= ? - ?)
            ORDER BY created_at_ms""",
        (max_attempts, now_ms, ttl_ms),
    ).fetchall()


def exhausted_drafts(conn: sqlite3.Connection, *, max_attempts: int) -> list[sqlite3.Row]:
    """Stuck drafts: max attempts reached with no draft created (durable, queryable state)."""
    return conn.execute(
        "SELECT * FROM draft_requests WHERE gmail_draft_id IS NULL AND attempts >= ? "
        "ORDER BY created_at_ms",
        (max_attempts,),
    ).fetchall()


# ── Classified messages ─────────────────────────────────────────────────────────

def record_classified_message(
    conn: sqlite3.Connection,
    *,
    account: str,
    message_id: str,
    thread_id: str,
    category: str,
    from_addr: str = "",
    subject: str = "",
    confidence: int = 0,
    source: str = "llm",
    llm_input_tokens: Optional[int] = None,
    llm_output_tokens: Optional[int] = None,
    llm_cost_usd_micros: Optional[int] = None,
) -> None:
    """Upsert a message's classification (idempotent on re-processing)."""
    conn.execute(
        """INSERT INTO classified_messages
               (account, message_id, thread_id, from_addr, subject, category, confidence,
                source, llm_input_tokens, llm_output_tokens, llm_cost_usd_micros, classified_at_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(account, message_id) DO UPDATE SET
               thread_id           = excluded.thread_id,
               from_addr           = excluded.from_addr,
               subject             = excluded.subject,
               category            = excluded.category,
               confidence          = excluded.confidence,
               source              = excluded.source,
               llm_input_tokens    = excluded.llm_input_tokens,
               llm_output_tokens   = excluded.llm_output_tokens,
               llm_cost_usd_micros = excluded.llm_cost_usd_micros,
               classified_at_ms    = excluded.classified_at_ms""",
        (account, message_id, thread_id, from_addr, subject, category, confidence,
         source, llm_input_tokens, llm_output_tokens, llm_cost_usd_micros, now_ms()),
    )


# ── Thread state ────────────────────────────────────────────────────────────────

def upsert_thread_state(
    conn: sqlite3.Connection,
    *,
    account: str,
    thread_id: str,
    last_message_id: str,
    last_category: str,
) -> None:
    conn.execute(
        """INSERT INTO thread_state (account, thread_id, last_message_id, last_category, last_processed_at_ms)
                VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(account, thread_id) DO UPDATE SET
               last_message_id      = excluded.last_message_id,
               last_category        = excluded.last_category,
               last_processed_at_ms = excluded.last_processed_at_ms""",
        (account, thread_id, last_message_id, last_category, now_ms()),
    )


def get_thread_state(conn: sqlite3.Connection, account: str, thread_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM thread_state WHERE account = ? AND thread_id = ?",
        (account, thread_id),
    ).fetchone()


# ── Sender profiles (voice/relationship for the drafting brief) ───────────────────

def get_sender_profile(
    conn: sqlite3.Connection, account: str, sender_email: str
) -> Optional[sqlite3.Row]:
    """The profile row for a (normalized) sender, or None."""
    return conn.execute(
        "SELECT * FROM sender_profiles WHERE account = ? AND sender_email = ?",
        (account, sender_email),
    ).fetchone()


def upsert_sender_profile(
    conn: sqlite3.Connection,
    *,
    account: str,
    sender_email: str,
    display_name: Optional[str] = None,
    relationship: Optional[str] = None,
    voice_notes: Optional[str] = None,
    tone_hints: Optional[str] = None,
    source: Optional[str] = None,
) -> None:
    """Insert or update a sender profile. ``COALESCE`` keeps existing non-NULL fields
    when a partial update passes None, so a later refinement never clobbers prior data."""
    conn.execute(
        """INSERT INTO sender_profiles
               (account, sender_email, display_name, relationship, voice_notes, tone_hints,
                source, draft_count, updated_at_ms)
               VALUES (:acct, :addr, :dn, :rel, :vn, :th, :src, 0, :now)
           ON CONFLICT(account, sender_email) DO UPDATE SET
               display_name  = COALESCE(:dn, display_name),
               relationship  = COALESCE(:rel, relationship),
               voice_notes   = COALESCE(:vn, voice_notes),
               tone_hints    = COALESCE(:th, tone_hints),
               source        = COALESCE(:src, source),
               updated_at_ms = :now""",
        {"acct": account, "addr": sender_email, "dn": display_name, "rel": relationship,
         "vn": voice_notes, "th": tone_hints, "src": source, "now": now_ms()},
    )


def bump_sender_draft_count(conn: sqlite3.Connection, account: str, sender_email: str) -> None:
    """Increment draft_count + stamp last_drafted_at_ms (no-op if no profile exists yet)."""
    conn.execute(
        """UPDATE sender_profiles
              SET draft_count = draft_count + 1, last_drafted_at_ms = ?, updated_at_ms = ?
            WHERE account = ? AND sender_email = ?""",
        (now_ms(), now_ms(), account, sender_email),
    )


# ── Module dedup (generic once-only marker) ──────────────────────────────────────

def note_once(conn: sqlite3.Connection, module: str, account: str, dedup_key: str) -> bool:
    """Record ``(module, account, dedup_key)`` the first time only; True iff new.

    Atomic check-then-insert (``INSERT OR IGNORE`` → rowcount): returns True on
    the first call for a key and False on every later call, so a notifying module
    fires exactly once even when the same message is re-drained (Pub/Sub
    redelivery, poll reconciler, StaleCursor reset). ``dedup_key`` must not carry
    a secret/PII — key on ``message_id`` (or carrier+tracking+stage), never a code.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO module_notified (module, account, dedup_key, notified_at_ms) "
        "VALUES (?, ?, ?, ?)",
        (module, account, dedup_key, now_ms()),
    )
    return cur.rowcount == 1


def was_notified(conn: sqlite3.Connection, module: str, account: str, dedup_key: str) -> bool:
    """True if ``note_once`` has already recorded this key (read-only check)."""
    return conn.execute(
        "SELECT 1 FROM module_notified WHERE module = ? AND account = ? AND dedup_key = ?",
        (module, account, dedup_key),
    ).fetchone() is not None


# ── Tracked packages (shipping module) ───────────────────────────────────────

def add_tracked_package(
    conn: sqlite3.Connection, account: str, tracking_number: str, carrier: Optional[int] = None
) -> bool:
    """Insert a newly-detected parcel; True iff new (repeat emails are no-ops)."""
    cur = conn.execute(
        """INSERT OR IGNORE INTO tracked_packages
               (account, tracking_number, carrier, created_at_ms, updated_at_ms)
               VALUES (?, ?, ?, ?, ?)""",
        (account, tracking_number, carrier, now_ms(), now_ms()),
    )
    return cur.rowcount == 1


def mark_package_registered(
    conn: sqlite3.Connection, account: str, tracking_number: str, carrier: Optional[int] = None
) -> None:
    """Flag a parcel as accepted by 17track (and record the resolved carrier)."""
    conn.execute(
        """UPDATE tracked_packages
              SET registered = 1,
                  carrier = COALESCE(?, carrier),
                  updated_at_ms = ?
            WHERE account = ? AND tracking_number = ?""",
        (carrier, now_ms(), account, tracking_number),
    )


def count_active_packages(conn: sqlite3.Connection) -> int:
    """Registered, non-terminal parcels — the live poll set size (for the cap)."""
    return conn.execute(
        "SELECT count(*) FROM tracked_packages WHERE registered = 1 AND terminal = 0"
    ).fetchone()[0]


def get_active_packages(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Registered, non-terminal parcels across all accounts (the poll set)."""
    return conn.execute(
        "SELECT * FROM tracked_packages WHERE registered = 1 AND terminal = 0"
    ).fetchall()


def update_package_stage(
    conn: sqlite3.Connection,
    account: str,
    tracking_number: str,
    stage: str,
    *,
    terminal: bool = False,
) -> None:
    """Record the latest polled stage (and whether the parcel is now terminal)."""
    conn.execute(
        """UPDATE tracked_packages
              SET last_stage = ?, terminal = ?, updated_at_ms = ?
            WHERE account = ? AND tracking_number = ?""",
        (stage, 1 if terminal else 0, now_ms(), account, tracking_number),
    )


def set_package_notified_stage(
    conn: sqlite3.Connection, account: str, tracking_number: str, stage: str
) -> None:
    """Record the stage we actually pushed (advanced only after a successful send)."""
    conn.execute(
        """UPDATE tracked_packages
              SET last_notified_stage = ?, updated_at_ms = ?
            WHERE account = ? AND tracking_number = ?""",
        (stage, now_ms(), account, tracking_number),
    )


def list_tracked_packages(
    conn: sqlite3.Connection, account: Optional[str] = None, *, include_terminal: bool = False
) -> list[sqlite3.Row]:
    """Tracked parcels for the on-demand tool (optionally scoped / incl. delivered)."""
    sql = "SELECT * FROM tracked_packages"
    clauses, params = [], []
    if account:
        clauses.append("account = ?")
        params.append(account)
    if not include_terminal:
        clauses.append("terminal = 0")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY updated_at_ms DESC"
    return conn.execute(sql, params).fetchall()
