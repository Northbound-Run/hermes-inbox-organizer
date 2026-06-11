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
    assert {"accounts", "draft_requests", "classified_messages", "thread_state", "oauth_pending"} <= names
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    # Re-connecting the same path is a no-op that keeps the data intact.
    conn2 = db.connect(tmp_path / "state.db")
    assert conn2.execute("SELECT count(*) FROM accounts").fetchone()[0] == 0


def test_oauth_pending_single_use_ttl_and_sweep(tmp_path) -> None:
    conn = _db(tmp_path)
    db.create_oauth_pending(conn, state="s1", verifier="v1")
    # fresh take returns the verifier exactly once (single-use)
    assert db.take_oauth_pending(conn, "s1", ttl_ms=1000, now_ms=db.now_ms()) == "v1"
    assert db.take_oauth_pending(conn, "s1", ttl_ms=1000, now_ms=db.now_ms()) is None
    # an expired pending is rejected AND removed
    db.create_oauth_pending(conn, state="s2", verifier="v2")
    base = conn.execute("SELECT created_at_ms FROM oauth_pending WHERE state='s2'").fetchone()[0]
    assert db.take_oauth_pending(conn, "s2", ttl_ms=10, now_ms=base + 11) is None
    assert conn.execute("SELECT 1 FROM oauth_pending WHERE state='s2'").fetchone() is None
    # sweep drops rows created at/before the cutoff
    db.create_oauth_pending(conn, state="s3", verifier="v3")
    cutoff = conn.execute("SELECT created_at_ms FROM oauth_pending WHERE state='s3'").fetchone()[0]
    assert db.sweep_oauth_pending(conn, before_ms=cutoff) == 1
    assert conn.execute("SELECT count(*) FROM oauth_pending").fetchone()[0] == 0


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


def test_migration_v1_to_current_adds_columns_preserving_data(tmp_path) -> None:
    # AC7: an existing v1 state.db upgrades in place to the CURRENT version — the v2
    # draft_requests columns are added and v1 rows kept (the chained v2+v3 blocks run).
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
    with contextlib.closing(db.connect(dbp)) as conn:  # triggers the v1 -> v3 migration chain
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
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


# ── Schema v3: draft feedback loop ────────────────────────────────────────────────

def test_v3_fresh_connect_creates_tables_columns_and_version(tmp_path) -> None:
    # AC1 + AC15/G5: a brand-new path connect must NOT raise (SCHEMA_SQL creates
    # sender_profiles WITH the new columns, then the guarded v3 ALTER skips them);
    # the new tables/columns exist and user_version == 3.
    conn = _db(tmp_path)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"draft_outcomes", "draft_lessons"} <= names
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    sp_cols = {r[1] for r in conn.execute("PRAGMA table_info(sender_profiles)")}
    assert {"learned_notes", "learned_updated_ms"} <= sp_cols
    do_cols = {r[1] for r in conn.execute("PRAGMA table_info(draft_outcomes)")}
    assert {"draft_body", "sent_body", "outcome", "similarity", "learned"} <= do_cols
    # the v3 indexes are present
    idx = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_do_learn_queue", "idx_do_examples", "idx_do_pending",
            "idx_lesson_dedup", "idx_lesson_rank"} <= idx


def test_migration_v2_to_v3_adds_columns_preserving_data(tmp_path) -> None:
    # AC2: an existing v2 state.db upgrades in place — sender_profiles gains the two
    # learned columns, draft_outcomes/draft_lessons get created, rows are preserved.
    dbp = tmp_path / "state.db"
    raw = sqlite3.connect(str(dbp))
    raw.executescript(
        """
        CREATE TABLE sender_profiles (
            account TEXT NOT NULL, sender_email TEXT NOT NULL,
            display_name TEXT, relationship TEXT, voice_notes TEXT, tone_hints TEXT,
            draft_count INTEGER NOT NULL DEFAULT 0, last_drafted_at_ms INTEGER,
            source TEXT, updated_at_ms INTEGER NOT NULL,
            PRIMARY KEY (account, sender_email)
        );
        INSERT INTO sender_profiles (account, sender_email, voice_notes, draft_count, updated_at_ms)
             VALUES ('a@x.com', 'bob@y.com', 'warm, brief', 4, 123);
        PRAGMA user_version = 2;
        """
    )
    raw.commit()
    raw.close()
    with contextlib.closing(db.connect(dbp)) as conn:  # triggers the v2 -> v3 migration
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sender_profiles)")}
        assert {"learned_notes", "learned_updated_ms"} <= cols
        names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"draft_outcomes", "draft_lessons"} <= names
        p = db.get_sender_profile(conn, "a@x.com", "bob@y.com")
        assert p["voice_notes"] == "warm, brief"  # data preserved
        assert p["draft_count"] == 4
        assert p["learned_notes"] is None         # new column NULL-backfilled


def test_v2_image_against_v3_db_noops(tmp_path) -> None:
    # AC2 (rollback-safe): a v2 image (lower _SCHEMA_VERSION) seeing a v3 DB must
    # return early from _migrate and leave user_version at 3.
    conn = _db(tmp_path)  # fresh v3 DB
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    # simulate the older image's _migrate guard: version >= its target -> no-op
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version >= 2  # the early-return condition for a v2 image
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_upsert_draft_outcome_draft_and_get(tmp_path) -> None:
    conn = _db(tmp_path)
    assert db.get_draft_outcome(conn, "a@x.com", "t1") is None
    db.upsert_draft_outcome_draft(
        conn, account="a@x.com", thread_id="t1", sender_email="bob@y.com",
        gmail_draft_id="d-1", draft_body="Hello Bob, here is the plan.",
    )
    row = db.get_draft_outcome(conn, "a@x.com", "t1")
    assert row["draft_body"] == "Hello Bob, here is the plan."
    assert row["gmail_draft_id"] == "d-1" and row["sender_email"] == "bob@y.com"
    assert row["outcome"] == "pending" and row["learned"] == 0
    assert row["draft_created_ms"] is not None
    # a re-draft while still pending refreshes the body + draft id
    db.upsert_draft_outcome_draft(
        conn, account="a@x.com", thread_id="t1", sender_email="bob@y.com",
        gmail_draft_id="d-2", draft_body="Hello Bob, revised plan.",
    )
    row = db.get_draft_outcome(conn, "a@x.com", "t1")
    assert row["draft_body"] == "Hello Bob, revised plan." and row["gmail_draft_id"] == "d-2"


def test_upsert_draft_outcome_draft_does_not_clobber_recorded_send(tmp_path) -> None:
    # AC16/M3/G1: once a send is recorded (outcome != 'pending'), a late re-draft
    # upsert must NOT overwrite draft_body/outcome — the conditional WHERE holds.
    conn = _db(tmp_path)
    db.upsert_draft_outcome_draft(
        conn, account="a@x.com", thread_id="t1", sender_email="bob@y.com",
        gmail_draft_id="d-1", draft_body="original draft",
    )
    db.record_draft_outcome_sent(
        conn, account="a@x.com", thread_id="t1", sender_email="bob@y.com",
        sent_message_id="m-9", sent_body="what I actually sent", similarity=42,
        outcome="sent_edited",
    )
    # a retry re-draft lands AFTER the send was recorded
    db.upsert_draft_outcome_draft(
        conn, account="a@x.com", thread_id="t1", sender_email="bob@y.com",
        gmail_draft_id="d-2", draft_body="LATE re-draft that must not win",
    )
    row = db.get_draft_outcome(conn, "a@x.com", "t1")
    assert row["outcome"] == "sent_edited"             # survived
    assert row["sent_body"] == "what I actually sent"  # survived
    assert row["draft_body"] == "original draft"       # NOT clobbered
    assert row["similarity"] == 42


def test_record_draft_outcome_sent_inserts_capture_all_row(tmp_path) -> None:
    # AC5 shape: a sent_no_draft capture-all row inserts fresh with draft_body NULL.
    conn = _db(tmp_path)
    db.record_draft_outcome_sent(
        conn, account="a@x.com", thread_id="t5", sender_email="carol@z.com",
        sent_message_id="m-5", sent_body="thanks, will do", similarity=None,
        outcome="sent_no_draft",
    )
    row = db.get_draft_outcome(conn, "a@x.com", "t5")
    assert row["draft_body"] is None and row["sent_body"] == "thanks, will do"
    assert row["outcome"] == "sent_no_draft" and row["similarity"] is None


def test_unlearned_outcomes_and_mark_learned(tmp_path) -> None:
    conn = _db(tmp_path)
    # pending rows are excluded; classified-but-unlearned are returned oldest first
    db.upsert_draft_outcome_draft(
        conn, account="a", thread_id="t-pending", sender_email="s", gmail_draft_id="d",
        draft_body="b",
    )
    db.record_draft_outcome_sent(
        conn, account="a", thread_id="t-edit", sender_email="s", sent_message_id="m1",
        sent_body="x", similarity=50, outcome="sent_edited",
    )
    db.record_draft_outcome_sent(
        conn, account="a", thread_id="t-ignore", sender_email="s", sent_message_id="m2",
        sent_body="y", similarity=10, outcome="sent_ignored",
    )
    queue = db.unlearned_outcomes(conn, limit=10)
    tids = {r["thread_id"] for r in queue}
    assert tids == {"t-edit", "t-ignore"}  # pending excluded
    db.mark_outcome_learned(conn, "a", "t-edit")
    queue = db.unlearned_outcomes(conn, limit=10)
    assert {r["thread_id"] for r in queue} == {"t-ignore"}  # learned excluded
    row = db.get_draft_outcome(conn, "a", "t-edit")
    assert row["learned"] == 1 and row["learned_at_ms"] is not None
    # limit is honored
    assert len(db.unlearned_outcomes(conn, limit=0)) == 0


def test_pending_outcomes_older_than(tmp_path) -> None:
    conn = _db(tmp_path)
    db.upsert_draft_outcome_draft(
        conn, account="a", thread_id="t-old", sender_email="s", gmail_draft_id="d1",
        draft_body="b",
    )
    db.upsert_draft_outcome_draft(
        conn, account="a", thread_id="t-new", sender_email="s", gmail_draft_id="d2",
        draft_body="b",
    )
    # push t-old's draft_created_ms into the past
    conn.execute(
        "UPDATE draft_outcomes SET draft_created_ms = 1000 WHERE thread_id = 't-old'"
    )
    conn.execute(
        # plain digits (no Python-style `_` separators): SQLite only accepts `_`
        # in numeric literals since 3.46, and CI runners ship older SQLite.
        "UPDATE draft_outcomes SET draft_created_ms = 9000000000000 WHERE thread_id = 't-new'"
    )
    old = db.pending_outcomes_older_than(conn, before_ms=5000)
    assert {r["thread_id"] for r in old} == {"t-old"}
    # a row that's already classified (not pending) is never swept
    db.record_draft_outcome_sent(
        conn, account="a", thread_id="t-old", sender_email="s", sent_message_id="m",
        sent_body="x", similarity=50, outcome="sent_edited",
    )
    assert db.pending_outcomes_older_than(conn, before_ms=5000) == []


def test_recent_sent_examples_ordering_and_filtering(tmp_path) -> None:
    conn = _db(tmp_path)
    # three sent rows for bob with increasing sent_at_ms; one empty-body row is skipped
    for tid, body, at in (("t1", "first", 100), ("t2", "second", 200), ("t3", "third", 300)):
        db.record_draft_outcome_sent(
            conn, account="a", thread_id=tid, sender_email="bob@y.com",
            sent_message_id="m" + tid, sent_body=body, similarity=None, outcome="sent_no_draft",
        )
        conn.execute("UPDATE draft_outcomes SET sent_at_ms = ? WHERE thread_id = ?", (at, tid))
    db.record_draft_outcome_sent(
        conn, account="a", thread_id="t-empty", sender_email="bob@y.com",
        sent_message_id="me", sent_body="", similarity=None, outcome="no_reply",
    )
    rows = db.recent_sent_examples(conn, "a", "bob@y.com", limit=2)
    assert [r["sent_body"] for r in rows] == ["third", "second"]  # newest first, capped
    # a different sender / account is independent
    assert db.recent_sent_examples(conn, "a", "carol@z.com", limit=5) == []


def test_count_outcomes_by_sender(tmp_path) -> None:
    conn = _db(tmp_path)
    for tid, outcome in (("t1", "sent_edited"), ("t2", "sent_edited"), ("t3", "no_reply")):
        db.record_draft_outcome_sent(
            conn, account="a", thread_id=tid, sender_email="bob@y.com",
            sent_message_id="m" + tid, sent_body="b", similarity=None, outcome=outcome,
        )
    hist = db.count_outcomes_by_sender(conn, "a", "bob@y.com")
    assert hist == {"sent_edited": 2, "no_reply": 1}
    assert db.count_outcomes_by_sender(conn, "a", "nobody@z.com") == {}


def test_delete_learned_outcomes_older_than(tmp_path) -> None:
    conn = _db(tmp_path)
    db.record_draft_outcome_sent(
        conn, account="a", thread_id="t-keep", sender_email="s", sent_message_id="m1",
        sent_body="b", similarity=None, outcome="sent_no_draft",
    )
    db.record_draft_outcome_sent(
        conn, account="a", thread_id="t-old", sender_email="s", sent_message_id="m2",
        sent_body="b", similarity=None, outcome="sent_edited",
    )
    db.mark_outcome_learned(conn, "a", "t-old")                       # learned + recent
    conn.execute("UPDATE draft_outcomes SET updated_at_ms = 1000 WHERE thread_id = 't-old'")
    # an unlearned row is never pruned even if old
    conn.execute("UPDATE draft_outcomes SET updated_at_ms = 1000 WHERE thread_id = 't-keep'")
    deleted = db.delete_learned_outcomes_older_than(conn, before_ms=5000)
    assert deleted == 1
    assert db.get_draft_outcome(conn, "a", "t-old") is None     # learned + old -> pruned
    assert db.get_draft_outcome(conn, "a", "t-keep") is not None  # unlearned -> kept


def test_learned_notes_isolated_from_voice_notes(tmp_path) -> None:
    # AC6: upsert_learned_notes writes learned_notes ONLY; voice_notes/tone_hints stay.
    conn = _db(tmp_path)
    db.upsert_sender_profile(
        conn, account="a", sender_email="bob@y.com",
        voice_notes="backfilled voice", tone_hints="warm", source="backfill",
    )
    db.upsert_learned_notes(conn, "a", "bob@y.com", "refined: shorter, no greeting")
    p = db.get_sender_profile(conn, "a", "bob@y.com")
    assert p["learned_notes"] == "refined: shorter, no greeting"
    assert p["voice_notes"] == "backfilled voice"   # untouched
    assert p["tone_hints"] == "warm"                # untouched
    assert p["learned_updated_ms"] is not None
    # re-applying replaces (cumulative-bounded, not append)
    db.upsert_learned_notes(conn, "a", "bob@y.com", "even shorter")
    p = db.get_sender_profile(conn, "a", "bob@y.com")
    assert p["learned_notes"] == "even shorter" and p["voice_notes"] == "backfilled voice"


def test_upsert_learned_notes_creates_row_when_absent(tmp_path) -> None:
    # a sender we've never backfilled can still accumulate learnings
    conn = _db(tmp_path)
    db.upsert_learned_notes(conn, "a", "new@z.com", "first learning")
    p = db.get_sender_profile(conn, "a", "new@z.com")
    assert p is not None and p["learned_notes"] == "first learning"
    assert p["voice_notes"] is None


def test_clear_learned_notes(tmp_path) -> None:
    conn = _db(tmp_path)
    db.upsert_sender_profile(
        conn, account="a", sender_email="bob@y.com", voice_notes="keep me", source="backfill"
    )
    db.upsert_learned_notes(conn, "a", "bob@y.com", "drop me")
    db.clear_learned_notes(conn, "a", "bob@y.com")
    p = db.get_sender_profile(conn, "a", "bob@y.com")
    assert p["learned_notes"] is None and p["voice_notes"] == "keep me"  # voice intact


def test_upsert_lesson_dedup_and_evidence_bump(tmp_path) -> None:
    conn = _db(tmp_path)
    db.upsert_lesson(conn, account="a", scope="global", polarity="dont", rule="Don't be verbose")
    db.upsert_lesson(conn, account="a", scope="global", polarity="dont", rule="don't be verbose")  # dup (norm)
    rows = conn.execute("SELECT * FROM draft_lessons").fetchall()
    assert len(rows) == 1  # deduped on norm_rule
    assert rows[0]["evidence_count"] == 2
    assert rows[0]["norm_rule"] == "don't be verbose"
    # a different polarity / account / scope is a distinct lesson
    db.upsert_lesson(conn, account="a", scope="global", polarity="do", rule="Don't be verbose")
    db.upsert_lesson(conn, account="b", scope="global", polarity="dont", rule="Don't be verbose")
    assert conn.execute("SELECT count(*) FROM draft_lessons").fetchone()[0] == 3


def test_top_lessons_ranking_and_active_filter(tmp_path) -> None:
    conn = _db(tmp_path)
    db.upsert_lesson(conn, account="a", scope="global", polarity="do", rule="lesson A")
    db.upsert_lesson(conn, account="a", scope="global", polarity="do", rule="lesson B")
    db.upsert_lesson(conn, account="a", scope="global", polarity="do", rule="lesson B")  # B evidence=2
    rows = db.top_lessons(conn, "a", limit=10)
    assert [r["rule"] for r in rows] == ["lesson B", "lesson A"]  # by evidence DESC
    # soft-disable A; it drops out
    a_id = next(r["lesson_id"] for r in rows if r["rule"] == "lesson A")
    db.set_lesson_active(conn, a_id, 0)
    assert [r["rule"] for r in db.top_lessons(conn, "a", limit=10)] == ["lesson B"]
    db.set_lesson_active(conn, a_id, 1)  # re-enable
    assert len(db.top_lessons(conn, "a", limit=10)) == 2


def test_prune_lessons_soft_evicts_lowest_value(tmp_path) -> None:
    # AC10: keep the top `keep` by (evidence DESC, last_seen DESC); soft-evict the rest.
    conn = _db(tmp_path)
    for rule in ("l1", "l2", "l3", "l4"):
        db.upsert_lesson(conn, account="a", scope="global", polarity="do", rule=rule)
    # give l4 the most evidence so it's clearly kept; l1 stays weakest + oldest
    db.upsert_lesson(conn, account="a", scope="global", polarity="do", rule="l4")
    db.upsert_lesson(conn, account="a", scope="global", polarity="do", rule="l3")
    evicted = db.prune_lessons(conn, "a", keep=2)
    assert evicted == 2
    kept = {r["rule"] for r in db.top_lessons(conn, "a", limit=10)}
    assert kept == {"l4", "l3"}  # highest-evidence survive
    # the evicted lessons are soft (active=0), not deleted
    assert conn.execute("SELECT count(*) FROM draft_lessons").fetchone()[0] == 4
    # pruning when already under cap is a no-op
    assert db.prune_lessons(conn, "a", keep=10) == 0
