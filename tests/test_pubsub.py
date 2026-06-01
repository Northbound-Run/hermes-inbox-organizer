"""Pub/Sub decode + the ack/nack durability contract."""

from __future__ import annotations

import base64
import json

import pytest

from hermes_inbox_organizer.pubsub import (
    Ack,
    GmailNotification,
    NotificationDecodeError,
    decode_gmail_notification,
    handle_pubsub_message,
)


def _payload(email: str = "u@gmail.com", history_id: int = 123) -> str:
    return base64.b64encode(
        json.dumps({"emailAddress": email, "historyId": history_id}).encode()
    ).decode()


def test_decode_valid_payload() -> None:
    notif = decode_gmail_notification(_payload("alice@gmail.com", 999))
    assert notif == GmailNotification(email_address="alice@gmail.com", history_id=999)


def test_decode_accepts_raw_json_from_pull_client() -> None:
    # The streaming-pull client delivers already-decoded JSON (str or bytes).
    assert decode_gmail_notification('{"emailAddress": "u@gmail.com", "historyId": 42}') == GmailNotification(
        email_address="u@gmail.com", history_id=42
    )
    assert decode_gmail_notification(b'{"emailAddress": "a@x.com", "historyId": 7}').history_id == 7


def test_decode_rejects_bad_base64_and_json_and_missing_keys() -> None:
    with pytest.raises(NotificationDecodeError):
        decode_gmail_notification("!!!not-base64!!!")
    with pytest.raises(NotificationDecodeError):
        decode_gmail_notification(base64.b64encode(b"not json").decode())
    with pytest.raises(NotificationDecodeError):
        decode_gmail_notification(base64.b64encode(b'{"historyId": 1}').decode())


class _Recorder:
    def __init__(self) -> None:
        self.persisted: list = []
        self.audits: list = []

    def audit(self, event: str, detail: str) -> None:
        self.audits.append((event, detail))


def test_known_account_persists_then_acks() -> None:
    rec = _Recorder()
    decision = handle_pubsub_message(
        _payload("u@gmail.com"),
        resolve_account=lambda email: "acct-1",
        persist=lambda acct, notif: rec.persisted.append((acct, notif)),
        audit=rec.audit,
    )
    assert decision is Ack.ACK
    assert len(rec.persisted) == 1
    assert rec.persisted[0][0] == "acct-1"


def test_unknown_account_acks_and_audits_without_persisting() -> None:
    rec = _Recorder()

    def _persist(acct, notif):
        raise AssertionError("must not persist for unknown account")

    decision = handle_pubsub_message(
        _payload("stranger@gmail.com"),
        resolve_account=lambda email: None,
        persist=_persist,
        audit=rec.audit,
    )
    assert decision is Ack.ACK
    assert rec.audits == [("pubsub_unknown_account", "stranger@gmail.com")]


def test_malformed_payload_acks_and_audits() -> None:
    rec = _Recorder()
    decision = handle_pubsub_message(
        "!!!garbage!!!",
        resolve_account=lambda email: "acct-1",
        persist=lambda acct, notif: rec.persisted.append((acct, notif)),
        audit=rec.audit,
    )
    assert decision is Ack.ACK
    assert rec.persisted == []
    assert rec.audits and rec.audits[0][0] == "pubsub_decode_failed"


def test_persist_failure_nacks_for_redelivery() -> None:
    rec = _Recorder()

    def _persist(acct, notif):
        raise RuntimeError("db down")

    decision = handle_pubsub_message(
        _payload("u@gmail.com"),
        resolve_account=lambda email: "acct-1",
        persist=_persist,
        audit=rec.audit,
    )
    assert decision is Ack.NACK  # not acked — Pub/Sub will redeliver
