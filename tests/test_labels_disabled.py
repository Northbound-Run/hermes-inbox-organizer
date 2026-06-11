"""INBOX_LABELS_ENABLED=0 — the label system off: no mailbox mutations anywhere
(no label creation/apply, no archiving, no sent-handler moves) while
classification, persistence, module dispatch, and draft wakes keep running.
"""

from __future__ import annotations

import base64
import contextlib

import pytest

from hermes_inbox_organizer.config import get_config, reset_config
from hermes_inbox_organizer.labels import CATEGORIES, label_name
from hermes_inbox_organizer.modules import InlineExecutor, Module, ModuleRegistry
from hermes_inbox_organizer.sent_handler import handle_sent
from hermes_inbox_organizer.triage import process_message

LABEL_IDS = {label_name(c): f"L{c.sort_order}" for c in CATEGORIES}


@pytest.fixture(autouse=True)
def _fresh_config():
    """Never leak a cached Config (ours or a prior test's) across tests."""
    reset_config()
    yield
    reset_config()


@pytest.fixture
def labels_off(monkeypatch):
    monkeypatch.setenv("INBOX_LABELS_ENABLED", "0")
    reset_config()


# ---------------------------------------------------------------------------
# Config knob
# ---------------------------------------------------------------------------


def test_labels_enabled_default_true() -> None:
    assert get_config().labels_enabled is True


@pytest.mark.parametrize("val", ["0", "false", "off", "no"])
def test_labels_disabled_via_env(monkeypatch, val) -> None:
    monkeypatch.setenv("INBOX_LABELS_ENABLED", val)
    reset_config()
    assert get_config().labels_enabled is False


# ---------------------------------------------------------------------------
# Triage path (fakes mirror tests/test_triage.py)
# ---------------------------------------------------------------------------


class _Exec:
    def __init__(self, val):
        self._val = val

    def execute(self):
        return self._val


class _Messages:
    def __init__(self, msg, modify_log):
        self._msg = msg
        self._modify = modify_log

    def get(self, userId, id, format):
        return _Exec(self._msg)

    def modify(self, userId, id, body):
        self._modify.append({"id": id, **body})
        return _Exec({})


class _Threads:
    def __init__(self, thread, modify_log):
        self._thread = thread
        self._modify = modify_log

    def get(self, userId, id, format):
        return _Exec(self._thread)

    def modify(self, userId, id, body):
        self._modify.append({"id": id, **body})
        return _Exec({})


class _Users:
    def __init__(self, svc):
        self._svc = svc

    def messages(self):
        return _Messages(self._svc.msg, self._svc.modify)

    def threads(self):
        return _Threads(self._svc.thread, self._svc.modify)


class FakeService:
    """Serves one message + one thread; records every modify (must stay empty)."""

    def __init__(self, msg=None, thread=None):
        self.msg = msg or {}
        self.thread = thread or {"messages": []}
        self.modify: list[dict] = []

    def users(self):
        return _Users(self)


def _msg(thread_id="t1", frm="a@x.com", subject="Q"):
    return {
        "id": "m1",
        "threadId": thread_id,
        "payload": {"headers": [{"name": "From", "value": frm}, {"name": "Subject", "value": subject}]},
    }


class _Rec(Module):
    name = "rec"

    def __init__(self) -> None:
        self.inbound: list = []
        self.sent: list = []

    def on_inbound(self, event) -> None:
        self.inbound.append(event)

    def on_sent(self, event) -> None:
        self.sent.append(event)


def test_triage_disabled_skips_apply_but_classifies_and_wakes(labels_off) -> None:
    svc = FakeService(msg=_msg())
    woke: list[dict] = []
    cat = process_message(
        message_id="m1", account_id="acct", service=svc, label_ids=LABEL_IDS,
        classify_fn=lambda parsed: "To Respond",
        wake_fn=lambda **kw: woke.append(kw),
    )
    assert cat == "To Respond"
    assert svc.modify == []  # the one thing that must NOT happen
    assert len(woke) == 1 and woke[0]["thread_id"] == "t1"  # draft wake still fires


def test_triage_disabled_skip_inbox_category_does_not_archive(labels_off) -> None:
    # Marketing normally removes INBOX; disabled means not even that mutation.
    svc = FakeService(msg=_msg())
    cat = process_message(
        message_id="m1", account_id="acct", service=svc, label_ids=LABEL_IDS,
        classify_fn=lambda parsed: "Marketing",
    )
    assert cat == "Marketing"
    assert svc.modify == []


def test_triage_disabled_still_persists_and_dispatches(labels_off, tmp_path) -> None:
    from hermes_inbox_organizer import db

    svc = FakeService(msg=_msg())
    rec = _Rec()
    reg = ModuleRegistry([rec], classify_fn=lambda parsed: "FYI", executor=InlineExecutor())
    with contextlib.closing(db.connect(tmp_path / "state.db")) as conn:
        cat = process_message(
            message_id="m1", account_id="acct@x.com", service=svc, label_ids=LABEL_IDS,
            registry=reg, conn=conn,
        )
        assert cat == "FYI"
        assert svc.modify == []
        row = conn.execute(
            "SELECT category FROM classified_messages WHERE account='acct@x.com' AND message_id='m1'"
        ).fetchone()
        assert row["category"] == "FYI"  # classification still recorded
    assert len(rec.inbound) == 1  # observers still notified


# ---------------------------------------------------------------------------
# Sent-handler path
# ---------------------------------------------------------------------------


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


def _sent_msg(thread_id="t1", body="Thanks, got it — all set."):
    return {
        "threadId": thread_id,
        "payload": {"mimeType": "text/plain", "body": {"data": _b64(body)}},
    }


def test_sent_disabled_no_move_but_event_dispatches(labels_off) -> None:
    # Empty label_ids (runtime never seeded labels) — the legacy code bailed with
    # "" before dispatching; disabled mode must still feed the feedback loop.
    rec = _Rec()
    reg = ModuleRegistry([rec], executor=InlineExecutor())
    svc = FakeService(msg=_sent_msg(body="Could you send the deck?"))
    target = handle_sent(
        message_id="s1", account_id="a@x.com", service=svc, label_ids={}, registry=reg
    )
    assert target == "Awaiting Reply"  # computed without labels (no To Respond known)
    assert svc.modify == []  # thread NOT moved
    assert len(rec.sent) == 1
    assert rec.sent[0].target_category == "Awaiting Reply"


def test_sent_disabled_with_known_labels_still_no_move(labels_off) -> None:
    # Populated label_ids (e.g. flag flipped off mid-flight): the target is still
    # computed from the thread's labels, but no mutation is issued.
    svc = FakeService(
        msg=_sent_msg(body="Thanks, got it — all set."),
        thread={"messages": [{"labelIds": ["L1", "INBOX"]}]},  # carries To Respond
    )
    target = handle_sent(message_id="s1", account_id="a", service=svc, label_ids=LABEL_IDS)
    assert target == "Actioned"  # closed a flagged thread, terminal reply
    assert svc.modify == []


def test_sent_enabled_unchanged() -> None:
    # Default env: the move still happens exactly as before.
    svc = FakeService(
        msg=_sent_msg(body="Thanks, got it — all set."),
        thread={"messages": [{"labelIds": ["L1", "INBOX"]}]},
    )
    target = handle_sent(message_id="s1", account_id="a", service=svc, label_ids=LABEL_IDS)
    assert target == "Actioned"
    assert svc.modify and svc.modify[0]["addLabelIds"] == ["L7"]


# ---------------------------------------------------------------------------
# Runtime arm path
# ---------------------------------------------------------------------------


def test_add_account_disabled_skips_ensure_labels(labels_off, tmp_path, monkeypatch) -> None:
    import hermes_inbox_organizer.labels_apply as la
    import hermes_inbox_organizer.runtime as rt_mod
    from hermes_inbox_organizer.runtime import Account, InboxRuntime

    def _boom(svc):
        raise AssertionError("ensure_labels must not be called with labels disabled")

    monkeypatch.setattr(la, "ensure_labels", _boom)
    monkeypatch.setattr(rt_mod, "arm_watch", lambda svc, topic: ("999", 123))

    rt = InboxRuntime(
        accounts=[], project="p", topic="t", subscription="s", sa_key_path="x",
        db_path=str(tmp_path / "state.db"),
    )
    acct = Account(email="new@gmail.com", build_service=lambda: object())
    assert rt.add_account(acct) is True
    assert acct.label_ids == {}  # no labels seeded
    assert acct.watch_expiration == 123  # watch still armed (classify/draft run on)
