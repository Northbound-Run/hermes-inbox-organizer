"""Tests for the SQLite persistence layer + config module."""

from __future__ import annotations

import sqlite3

from hermes_inbox_organizer import config, db


def _db(tmp_path) -> sqlite3.Connection:
    return db.connect(tmp_path / "state.db")


def test_connect_creates_schema_and_is_idempotent(tmp_path) -> None:
    conn = _db(tmp_path)
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"accounts", "draft_requests", "classified_messages", "thread_state"} <= names
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
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
