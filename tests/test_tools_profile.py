"""Sender-profile tools: get/save round-trip (AC11) + backfill handler wiring."""

from __future__ import annotations

import json

from hermes_inbox_organizer import config
from hermes_inbox_organizer.tools_profile import (
    make_backfill_profiles_handler,
    make_get_sender_profile_handler,
    make_save_sender_profile_handler,
)


def _use_temp_db(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("INBOX_DB_PATH", str(tmp_path / "state.db"))
    config.reset_config()


def test_save_then_get_round_trips(tmp_path, monkeypatch) -> None:
    _use_temp_db(tmp_path, monkeypatch)
    save = make_save_sender_profile_handler()
    get = make_get_sender_profile_handler()
    out = json.loads(save({
        "account_id": "a@x.com", "sender_email": "Bob@Y.com",
        "relationship": "friend", "voice_notes": "casual",
    }))
    assert out["ok"] is True and out["sender_email"] == "bob@y.com"  # normalized
    got = json.loads(get({"account_id": "a@x.com", "sender_email": "bob@y.com"}))
    assert got["profile"]["relationship"] == "friend"
    assert got["profile"]["voice_notes"] == "casual"
    assert got["profile"]["source"] == "agent"
    config.reset_config()


def test_get_missing_returns_null(tmp_path, monkeypatch) -> None:
    _use_temp_db(tmp_path, monkeypatch)
    out = json.loads(make_get_sender_profile_handler()({"account_id": "a@x.com", "sender_email": "nobody@y.com"}))
    assert out["profile"] is None
    config.reset_config()


def test_handlers_validate_required_args(tmp_path, monkeypatch) -> None:
    _use_temp_db(tmp_path, monkeypatch)
    assert "error" in json.loads(make_get_sender_profile_handler()({"account_id": "a@x.com"}))
    assert "error" in json.loads(make_save_sender_profile_handler()({"sender_email": "x@y.com"}))
    config.reset_config()


def test_backfill_handler_wraps_runner() -> None:
    calls = []

    def fake_run(account_id, force):
        calls.append((account_id, force))
        return {"a@x.com": {"profiled": ["bob@y.com"], "skipped": [], "errors": []}}

    out = json.loads(make_backfill_profiles_handler(fake_run)({"account_id": "a@x.com", "force": True}))
    assert out["ok"] is True
    assert out["result"]["a@x.com"]["profiled"] == ["bob@y.com"]
    assert calls == [("a@x.com", True)]


def test_backfill_handler_never_raises() -> None:
    def boom(account_id, force):
        raise RuntimeError("nope")

    out = json.loads(make_backfill_profiles_handler(boom)({}))
    assert "error" in out  # returned as JSON, not raised
