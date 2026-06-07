"""Tests for the SQLite persistence layer + config module."""

from __future__ import annotations

import contextlib
import sqlite3
import threading

from hermes_inbox_organizer import config, db


def _db(tmp_path) -> sqlite3.Connection:
    return db.connect(tmp_path / "state.db")


def test_connect_creates_schema_and_is_idempotent(tmp_path) -> None:
    conn = _db(tmp_path)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"accounts", "draft_requests", "classified_messages", "thread_state"} <= names
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    # Re-connecting the same path is a no-op that keeps the data intact.
    conn2 = db.connect(tmp_path / "state.db")
    assert conn2.execute("SELECT count(*) FROM accounts").fetchone()[0] == 0


def test_cursor_roundtrip_and_isolation(tmp_path) -> None:
    conn = _db(tmp_path)
    assert db.get_cursor(conn, "a@x.com") is None
    db.set_cursor(conn, "a@x.com", "100")
    db.set_cursor(conn, "b@x.com", "200")
    assert db.get_cursor(conn, "a@x.com") == "100"
    assert db.get_cursor(conn, "b@x.com") == "200"
    db.set_cursor(conn, "a@x.com", "150")  # update doesn't touch the other account
    assert db.get_cursor(conn, "a@x.com") == "150"
    assert db.get_cursor(conn, "b@x.com") == "200"


def test_draft_requests_dedup_and_per_account(tmp_path) -> None:
    conn = _db(tmp_path)
    assert not db.draft_already_requested(conn, "a@x.com", "t1")
    db.mark_draft_requested(conn, "a@x.com", "t1")
    db.mark_draft_requested(conn, "a@x.com", "t1")  # idempotent
    assert db.draft_already_requested(conn, "a@x.com", "t1")
    assert conn.execute("SELECT count(*) FROM draft_requests").fetchone()[0] == 1
    # Same thread id under a different account is independent.
    assert not db.draft_already_requested(conn, "b@x.com", "t1")
    db.set_draft_id(conn, "a@x.com", "t1", "r-123")
    row = conn.execute(
        "SELECT gmail_draft_id FROM draft_requests WHERE account='a@x.com' AND thread_id='t1'"
    ).fetchone()
    assert row["gmail_draft_id"] == "r-123"


def test_set_draft_id_creates_row_when_unmarked(tmp_path) -> None:
    conn = _db(tmp_path)
    db.set_draft_id(conn, "a@x.com", "t9", "r-9")
    assert db.draft_already_requested(conn, "a@x.com", "t9")


def test_classified_messages_upsert(tmp_path) -> None:
    conn = _db(tmp_path)
    db.record_classified_message(
        conn, account="a@x.com", message_id="m1", thread_id="t1", category="To Respond",
        from_addr="x@y.com", subject="Q", confidence=900, source="llm",
        llm_input_tokens=10, llm_output_tokens=5, llm_cost_usd_micros=42,
    )
    rows = conn.execute("SELECT * FROM classified_messages").fetchall()
    assert len(rows) == 1
    assert rows[0]["category"] == "To Respond" and rows[0]["confidence"] == 900
    assert rows[0]["llm_cost_usd_micros"] == 42
    # Re-classifying the same message upserts (no duplicate row).
    db.record_classified_message(
        conn, account="a@x.com", message_id="m1", thread_id="t1", category="FYI", source="pre"
    )
    rows = conn.execute("SELECT * FROM classified_messages").fetchall()
    assert len(rows) == 1 and rows[0]["category"] == "FYI" and rows[0]["source"] == "pre"


def test_thread_state_upsert_and_get(tmp_path) -> None:
    conn = _db(tmp_path)
    assert db.get_thread_state(conn, "a@x.com", "t1") is None
    db.upsert_thread_state(
        conn, account="a@x.com", thread_id="t1", last_message_id="m1", last_category="To Respond"
    )
    st = db.get_thread_state(conn, "a@x.com", "t1")
    assert st["last_category"] == "To Respond" and st["last_message_id"] == "m1"
    db.upsert_thread_state(
        conn, account="a@x.com", thread_id="t1", last_message_id="m2", last_category="Actioned"
    )
    st = db.get_thread_state(conn, "a@x.com", "t1")
    assert st["last_category"] == "Actioned" and st["last_message_id"] == "m2"
    assert conn.execute("SELECT count(*) FROM thread_state").fetchone()[0] == 1


def test_config_defaults(monkeypatch) -> None:
    for k in ("INBOX_DATA_DIR", "INBOX_DB_PATH", "INBOX_CONFIG_DIR", "INBOX_KEY_FILE", "INBOX_TOKEN_DIR"):
        monkeypatch.delenv(k, raising=False)
    config.reset_config()
    c = config.get_config()
    assert c.data_dir == "/opt/data/inbox-organizer"
    assert c.db_path == "/opt/data/inbox-organizer/state.db"
    assert c.key_file == "/opt/data/config/inbox-encryption-key"
    assert c.token_dir == "/opt/data/inbox-organizer/accounts"
    assert c.sa_key_file == "/opt/data/config/inbox-pubsub-sa.json"
    config.reset_config()


def test_config_data_dir_override_moves_db_and_tokens(monkeypatch) -> None:
    monkeypatch.setenv("INBOX_DATA_DIR", "/tmp/inbox")
    monkeypatch.delenv("INBOX_DB_PATH", raising=False)
    monkeypatch.delenv("INBOX_TOKEN_DIR", raising=False)
    config.reset_config()
    c = config.get_config()
    assert c.db_path == "/tmp/inbox/state.db"
    assert c.token_dir == "/tmp/inbox/accounts"
    config.reset_config()


def test_config_owner_ids_parsed(monkeypatch) -> None:
    monkeypatch.setenv("INBOX_OWNER_MATRIX_IDS", " @a:x , @b:y ,")
    config.reset_config()
    assert config.get_config().owner_matrix_ids == frozenset({"@a:x", "@b:y"})
    config.reset_config()


def test_config_wake_timeout_and_research(monkeypatch) -> None:
    monkeypatch.setenv("INBOX_WAKE_TIMEOUT_S", "90")
    monkeypatch.setenv("INBOX_DRAFT_RESEARCH", "off")
    config.reset_config()
    c = config.get_config()
    assert c.wake_timeout_s == 90
    assert c.draft_research_enabled is False
    config.reset_config()


def test_note_once_dedup_and_scoping(tmp_path) -> None:
    conn = _db(tmp_path)
    # First call records + returns True; repeats return False (deduped exactly once).
    assert db.note_once(conn, "twofa", "a@x.com", "msg-1") is True
    assert db.note_once(conn, "twofa", "a@x.com", "msg-1") is False
    assert db.was_notified(conn, "twofa", "a@x.com", "msg-1") is True
    # Scoped by (module, account, key): a different key/account/module is independent.
    assert db.note_once(conn, "twofa", "a@x.com", "msg-2") is True
    assert db.note_once(conn, "twofa", "b@x.com", "msg-1") is True
    assert db.note_once(conn, "shipping", "a@x.com", "msg-1") is True
    assert db.was_notified(conn, "twofa", "a@x.com", "never-seen") is False


def test_claim_draft_lifecycle(tmp_path) -> None:
    conn = _db(tmp_path)
    A, T = "a@x.com", "t1"
    # first claim wins (new row, attempt 1)
    assert db.claim_draft(conn, A, T, from_addr="al@x.com", subject="Hi",
                          ttl_ms=1000, max_attempts=3, now_ms=10_000) is True
    # immediate re-claim within the ttl window -> in-flight -> lost
    assert db.claim_draft(conn, A, T, ttl_ms=1000, max_attempts=3, now_ms=10_500) is False
    # past the ttl -> eligible -> wins (attempt 2); empty metadata must NOT clobber
    assert db.claim_draft(conn, A, T, ttl_ms=1000, max_attempts=3, now_ms=12_000) is True
    # past the ttl -> wins (attempt 3)
    assert db.claim_draft(conn, A, T, ttl_ms=1000, max_attempts=3, now_ms=14_000) is True
    # attempts now == max -> never claims again, even far past the ttl
    assert db.claim_draft(conn, A, T, ttl_ms=1000, max_attempts=3, now_ms=99_000) is False
    row = db.get_draft_request(conn, A, T)
    assert row["attempts"] == 3
    assert row["from_addr"] == "al@x.com" and row["subject"] == "Hi"  # preserved across empty re-claims


def test_claim_draft_skips_fulfilled(tmp_path) -> None:
    conn = _db(tmp_path)
    assert db.claim_draft(conn, "a", "t", ttl_ms=1000, max_attempts=3, now_ms=1) is True
    db.set_draft_id(conn, "a", "t", "draft-1")  # fulfilled
    # even far past the ttl, a fulfilled thread is never re-claimed
    assert db.claim_draft(conn, "a", "t", ttl_ms=1000, max_attempts=3, now_ms=10_000_000) is False


def test_unfulfilled_and_exhausted_drafts(tmp_path) -> None:
    conn = _db(tmp_path)
    db.claim_draft(conn, "a", "t1", ttl_ms=1000, max_attempts=3, now_ms=1)      # stale -> retryable
    db.claim_draft(conn, "a", "t2", ttl_ms=1000, max_attempts=3, now_ms=1)      # fulfilled below
    db.set_draft_id(conn, "a", "t2", "d-2")
    for now in (1, 3000, 6000):                                                 # t3 -> exhausted
        db.claim_draft(conn, "a", "t3", ttl_ms=1000, max_attempts=3, now_ms=now)
    unfulfilled = db.unfulfilled_drafts(conn, ttl_ms=1000, max_attempts=3, now_ms=1_000_000)
    assert {r["thread_id"] for r in unfulfilled} == {"t1"}  # t2 fulfilled, t3 exhausted
    assert {r["thread_id"] for r in db.exhausted_drafts(conn, max_attempts=3)} == {"t3"}


def test_claim_draft_atomic_under_concurrency(tmp_path) -> None:
    # AC19: many threads race the same (account, thread); exactly one wins the claim.
    dbp = tmp_path / "state.db"
    with contextlib.closing(db.connect(dbp)):  # initialize schema once before threading
        pass
    results: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker() -> None:
        with contextlib.closing(db.connect(dbp)) as conn:
            barrier.wait()  # maximize contention
            won = db.claim_draft(conn, "a@x.com", "t1", from_addr="x", subject="s",
                                 ttl_ms=900_000, max_attempts=3, now_ms=1000)
        with lock:
            results.append(won)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r) == 1  # exactly one winner
    with contextlib.closing(db.connect(dbp)) as conn:
        assert db.get_draft_request(conn, "a@x.com", "t1")["attempts"] == 1  # losers didn't bump


def test_draft_id_round_trips_across_threads(tmp_path) -> None:
    # AC22: the record_draft seam writes on one connection/thread; the daemon reads
    # it on another. Validates cross-thread durability under WAL (de-risks R5).
    dbp = tmp_path / "state.db"
    with contextlib.closing(db.connect(dbp)) as conn:
        db.claim_draft(conn, "a@x.com", "t1", ttl_ms=1000, max_attempts=3, now_ms=1)

    def writer() -> None:
        with contextlib.closing(db.connect(dbp)) as conn:
            db.set_draft_id(conn, "a@x.com", "t1", "draft-9")

    t = threading.Thread(target=writer)
    t.start()
    t.join()
    with contextlib.closing(db.connect(dbp)) as conn:  # different connection, main thread
        assert db.get_draft_request(conn, "a@x.com", "t1")["gmail_draft_id"] == "draft-9"


def test_migration_v1_to_v2_adds_columns_preserving_data(tmp_path) -> None:
    # AC7: an existing v1 state.db upgrades in place — new columns added, rows kept.
    dbp = tmp_path / "state.db"
    raw = sqlite3.connect(str(dbp))
    raw.executescript(
        """
        CREATE TABLE draft_requests (
            account TEXT NOT NULL, thread_id TEXT NOT NULL,
            gmail_draft_id TEXT, created_at_ms INTEGER NOT NULL,
            PRIMARY KEY (account, thread_id)
        );
        INSERT INTO draft_requests (account, thread_id, gmail_draft_id, created_at_ms)
             VALUES ('a@x.com', 't1', 'd-1', 123);
        PRAGMA user_version = 1;
        """
    )
    raw.commit()
    raw.close()
    with contextlib.closing(db.connect(dbp)) as conn:  # triggers the v1 -> v2 migration
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        cols = {r[1] for r in conn.execute("PRAGMA table_info(draft_requests)")}
        assert {"from_addr", "subject", "attempts", "last_attempt_ms"} <= cols
        row = db.get_draft_request(conn, "a@x.com", "t1")
        assert row["gmail_draft_id"] == "d-1"  # data preserved
        assert row["attempts"] == 0            # NOT NULL DEFAULT 0 backfilled


def test_sender_profiles_table_exists(tmp_path) -> None:
    conn = _db(tmp_path)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "sender_profiles" in names


def test_sender_profile_upsert_get_and_bump(tmp_path) -> None:
    conn = _db(tmp_path)
    assert db.get_sender_profile(conn, "a@x.com", "bob@y.com") is None
    db.upsert_sender_profile(
        conn, account="a@x.com", sender_email="bob@y.com",
        display_name="Bob", relationship="coworker", voice_notes="warm, brief",
        tone_hints="casual", source="backfill",
    )
    p = db.get_sender_profile(conn, "a@x.com", "bob@y.com")
    assert p["display_name"] == "Bob" and p["relationship"] == "coworker"
    assert p["voice_notes"] == "warm, brief" and p["tone_hints"] == "casual"
    assert p["source"] == "backfill" and p["draft_count"] == 0
    # partial update via COALESCE keeps existing non-None fields
    db.upsert_sender_profile(
        conn, account="a@x.com", sender_email="bob@y.com", tone_hints="formal", source="agent"
    )
    p = db.get_sender_profile(conn, "a@x.com", "bob@y.com")
    assert p["tone_hints"] == "formal" and p["voice_notes"] == "warm, brief"  # preserved
    assert p["source"] == "agent"
    # bump increments draft_count + stamps last_drafted_at_ms
    db.bump_sender_draft_count(conn, "a@x.com", "bob@y.com")
    p = db.get_sender_profile(conn, "a@x.com", "bob@y.com")
    assert p["draft_count"] == 1 and p["last_drafted_at_ms"] is not None
    # per-account scoping
    assert db.get_sender_profile(conn, "b@x.com", "bob@y.com") is None
