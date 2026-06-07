"""Shipping module: detection + register + poll/notify + tool (no network/creds)."""

from __future__ import annotations

import contextlib
import json

from hermes_inbox_organizer import db
from hermes_inbox_organizer.modules import InboundEvent, ModuleRegistry
from hermes_inbox_organizer.modules.shipping import (
    ShippingModule,
    detect_tracking_numbers,
)
from hermes_inbox_organizer.modules.track17 import FakeTrack17Client
from hermes_inbox_organizer.notifier import FakeNotifier

UPS = "1Z999AA10123456784"


def _ev(account="a@x.com", message_id="m1", subject="Your order shipped", body="", **kw):
    parsed = {"from": "ship@store.com", "subject": subject, "body": body}
    base = dict(
        account_id=account, message_id=message_id, thread_id="t1", parsed=parsed, category="Notification"
    )
    base.update(kw)
    return InboundEvent(**base)


def _mod(tmp_path, notifier, client, **kw):
    return ShippingModule(
        notifier=notifier, client=client, db_connect=lambda: db.connect(tmp_path / "state.db"), **kw
    )


def _active(tmp_path):
    with contextlib.closing(db.connect(tmp_path / "state.db")) as conn:
        return db.get_active_packages(conn)


# ── detection ─────────────────────────────────────────────────────────────────


def test_detect_ups_number():
    assert detect_tracking_numbers({"subject": "Shipped", "body": f"Tracking: {UPS}"}) == [UPS]


def test_detect_explicit_tracking_number():
    n = "9400111899223817200000"
    assert detect_tracking_numbers({"subject": "Shipment update", "body": f"Tracking number: {n}"}) == [n]


def test_detect_requires_shipping_cue():
    # Same long number, but no shipping context -> not extracted.
    assert detect_tracking_numbers({"subject": "Invoice", "body": "Reference 9400111899223817200000"}) == []


def test_detect_no_number_returns_empty():
    assert detect_tracking_numbers({"subject": "Your order shipped", "body": "thanks!"}) == []


# ── FakeTrack17Client ─────────────────────────────────────────────────────────


def test_fake_client_register_and_status():
    c = FakeTrack17Client(reject={"BAD"})
    res = c.register([UPS, "BAD"])
    assert res.accepted == [UPS] and "BAD" in res.rejected
    c.set_status(UPS, "InTransit")
    statuses = c.get_statuses([UPS, "missing"])
    assert statuses[0].stage == "InTransit" and statuses[1].stage is None


# ── on_inbound: register + dedup + caps ───────────────────────────────────────


def test_on_inbound_registers_new_parcel(tmp_path):
    c = FakeTrack17Client()
    _mod(tmp_path, FakeNotifier(), c).on_inbound(_ev(body=f"Tracking: {UPS}"))
    assert c.registered == [UPS]
    rows = _active(tmp_path)
    assert len(rows) == 1 and rows[0]["tracking_number"] == UPS and rows[0]["registered"] == 1


def test_on_inbound_dedups_repeat_parcel(tmp_path):
    c = FakeTrack17Client()
    m = _mod(tmp_path, FakeNotifier(), c)
    m.on_inbound(_ev(body=f"Tracking: {UPS}"))
    m.on_inbound(_ev(message_id="m2", body=f"Re: your shipment, tracking {UPS}"))
    assert c.registered == [UPS]  # registered exactly once


def test_on_inbound_respects_max_active(tmp_path):
    c = FakeTrack17Client()
    m = _mod(tmp_path, FakeNotifier(), c, max_active=1)
    m.on_inbound(_ev(message_id="m1", body=f"Tracking: {UPS}"))
    m.on_inbound(_ev(message_id="m2", body="Tracking: 1Z888BB20123456789"))
    assert c.registered == [UPS]  # second skipped — at the cap


def test_on_inbound_quota_exhausted_degrades(tmp_path):
    c = FakeTrack17Client(quota_exhausted=True)
    _mod(tmp_path, FakeNotifier(), c).on_inbound(_ev(body=f"Tracking: {UPS}"))
    assert _active(tmp_path) == []  # recorded but not registered (label only), never crashes


# ── poll: notify on change, terminal cleanup, retry-on-failure ────────────────


def test_poll_notifies_on_stage_change_and_marks_terminal(tmp_path):
    n = FakeNotifier()
    c = FakeTrack17Client()
    m = _mod(tmp_path, n, c)
    m.on_inbound(_ev(body=f"Tracking: {UPS}"))

    c.set_status(UPS, "InTransit")
    m.poll_once()
    assert len(n.sent) == 1 and UPS in n.sent[0]["text"] and "transit" in n.sent[0]["text"].lower()
    assert n.sent[0]["urgent"] is False  # shipping is not urgent

    m.poll_once()  # same stage -> no new push
    assert len(n.sent) == 1

    c.set_status(UPS, "Delivered")
    m.poll_once()
    assert len(n.sent) == 2 and "delivered" in n.sent[1]["text"].lower()
    assert _active(tmp_path) == []  # terminal -> dropped from the poll set

    c.set_status(UPS, "InTransit")  # ignored — terminal parcels aren't polled
    m.poll_once()
    assert len(n.sent) == 2


def test_poll_retries_after_failed_push(tmp_path):
    # B3: a failed push must not advance last_notified_stage -> re-fires next poll.
    class _FailOnce:
        def __init__(self):
            self.calls = 0

        def send(self, text, *, urgent=False):
            self.calls += 1
            return self.calls > 1  # first push fails, second succeeds

    n = _FailOnce()
    c = FakeTrack17Client()
    m = _mod(tmp_path, n, c)
    m.on_inbound(_ev(body=f"Tracking: {UPS}"))
    c.set_status(UPS, "OutForDelivery")
    m.poll_once()  # push fails -> marker not advanced
    m.poll_once()  # same stage, re-fires -> succeeds
    assert n.calls == 2


def test_poll_batches_in_groups_of_40(tmp_path):
    c = FakeTrack17Client()
    m = _mod(tmp_path, FakeNotifier(), c, max_active=100)
    with contextlib.closing(db.connect(tmp_path / "state.db")) as conn:
        for i in range(45):
            num = f"PKG{i:020d}"
            db.add_tracked_package(conn, "a@x.com", num)
            db.mark_package_registered(conn, "a@x.com", num)
    m.poll_once()
    assert len(c.status_calls) == 2  # 45 -> 40 + 5
    assert len(c.status_calls[0]) == 40 and len(c.status_calls[1]) == 5


# ── tool + registry gating ────────────────────────────────────────────────────


def test_inbox_track_packages_tool(tmp_path):
    c = FakeTrack17Client()
    m = _mod(tmp_path, FakeNotifier(), c)
    m.on_inbound(_ev(body=f"Tracking: {UPS}"))
    c.set_status(UPS, "InTransit")
    m.poll_once()
    handler = m.tools()[0].handler
    out = json.loads(handler({}))
    assert out["count"] == 1
    pkg = out["packages"][0]
    assert pkg["tracking_number"] == UPS
    assert "transit" in (pkg["stage"] or "").lower()
    assert pkg["delivered"] is False


def test_disabled_without_client(tmp_path):
    m = ShippingModule(
        notifier=FakeNotifier(), client=None, db_connect=lambda: db.connect(tmp_path / "s.db")
    )
    assert m.enabled is False
    reg = ModuleRegistry([m])
    try:
        assert reg.modules == []  # no client -> disabled -> dropped by the registry
    finally:
        reg.shutdown()
