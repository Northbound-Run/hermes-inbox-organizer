"""2FA module: deterministic code detection + push + dedup (no LLM, no creds)."""

from __future__ import annotations

import contextlib

from hermes_inbox_organizer import db
from hermes_inbox_organizer.modules import InboundEvent, ModuleRegistry
from hermes_inbox_organizer.modules.twofa import (
    TwoFactorModule,
    _sender_email,
    detect_code,
)
from hermes_inbox_organizer.notifier import FakeNotifier


def _ev(frm="Okta <noreply@okta.com>", subject="Your code", body="", **kw) -> InboundEvent:
    parsed = {"from": frm, "subject": subject, "body": body}
    base = dict(
        account_id="a@x.com", message_id="m1", thread_id="t1", parsed=parsed, category="Notification"
    )
    base.update(kw)
    return InboundEvent(**base)


def _mod(tmp_path, notifier, **kw) -> TwoFactorModule:
    return TwoFactorModule(
        notifier=notifier, db_connect=lambda: db.connect(tmp_path / "state.db"), **kw
    )


# ── detect_code ───────────────────────────────────────────────────────────────


def test_detect_code_common_phrasings():
    assert detect_code({"body": "Your verification code is 123456."}) == "123456"
    assert detect_code({"subject": "Sign in", "body": "OTP: 4821"}) == "4821"
    assert detect_code({"body": "Enter passcode 9087 to continue"}) == "9087"
    assert detect_code({"body": "Your code: (135790)"}) == "135790"
    assert detect_code({"body": "Use G-558211 to verify your identity"}) == "G-558211"
    assert detect_code({"body": "123456 is your verification code"}) == "123456"
    assert detect_code({"body": "Your code is 482 913 now"}) == "482 913"


def test_detect_code_from_subject():
    assert detect_code({"subject": "Your login code is 246802", "body": ""}) == "246802"


def test_detect_code_negatives():
    assert detect_code({"subject": "Your order 12345 shipped", "body": "see tracking"}) is None
    assert detect_code({"subject": "Re: lunch?", "body": "see you at 1230 tomorrow"}) is None
    assert detect_code({"subject": "Invoice", "body": "Amount due 4200 USD by 2026"}) is None
    assert detect_code({"subject": "", "body": ""}) is None


def test_detect_code_bounded_input_returns_quickly():
    # Length-bounded scan — a huge body can't hang the worker (no ReDoS).
    assert detect_code({"subject": "x" * 5000, "body": "no code here " * 5000}) is None


# ── _sender_email ─────────────────────────────────────────────────────────────


def test_sender_email_strips_display_name():
    assert _sender_email("Okta <noreply@okta.com>") == "noreply@okta.com"
    assert _sender_email("plain@example.com") == "plain@example.com"
    # A crafted display name with decoy angle brackets -> still the real (last) addr.
    assert _sender_email('"Evil <ignore me>" <attacker@bad.com>') == "attacker@bad.com"


# ── on_inbound: push + dedup + allowlist ──────────────────────────────────────


def test_on_inbound_pushes_code_urgently(tmp_path):
    n = FakeNotifier()
    _mod(tmp_path, n).on_inbound(_ev(body="Your verification code is 246813."))
    assert len(n.sent) == 1
    assert n.sent[0]["urgent"] is True
    assert "246813" in n.sent[0]["text"]
    assert "noreply@okta.com" in n.sent[0]["text"]


def test_on_inbound_dedups_per_message(tmp_path):
    n = FakeNotifier()
    m = _mod(tmp_path, n)
    ev = _ev(body="code: 111222")
    m.on_inbound(ev)
    m.on_inbound(ev)  # same message re-drained (redelivery / poll)
    assert len(n.sent) == 1  # pushed exactly once


def test_on_inbound_ignores_non_2fa_mail(tmp_path):
    n = FakeNotifier()
    _mod(tmp_path, n).on_inbound(_ev(subject="lunch?", body="see you at noon"))
    assert n.sent == []


def test_on_inbound_allowlist_blocks_unknown_sender(tmp_path):
    n = FakeNotifier()
    m = _mod(tmp_path, n, sender_allowlist=frozenset({"okta.com"}))
    m.on_inbound(_ev(frm="noreply@okta.com", body="code 121212"))  # allowed domain
    m.on_inbound(_ev(frm="phish@evil.com", message_id="m2", body="code 343434"))  # blocked
    assert len(n.sent) == 1
    assert "121212" in n.sent[0]["text"]


def test_code_is_never_persisted(tmp_path):
    n = FakeNotifier()
    _mod(tmp_path, n).on_inbound(_ev(body="Your code is 777888"))
    with contextlib.closing(db.connect(tmp_path / "state.db")) as conn:
        rows = conn.execute("SELECT * FROM module_notified").fetchall()
    assert len(rows) == 1
    assert rows[0]["dedup_key"] == "m1"  # keyed on message_id
    flat = " ".join(str(v) for r in rows for v in tuple(r))
    assert "777888" not in flat  # the code appears in no column


# ── registry integration ──────────────────────────────────────────────────────


def test_disabled_module_excluded_from_registry(tmp_path):
    m = _mod(tmp_path, FakeNotifier(), enabled=False)
    reg = ModuleRegistry([m])
    try:
        assert reg.modules == []  # disabled -> dropped at construction
    finally:
        reg.shutdown()
