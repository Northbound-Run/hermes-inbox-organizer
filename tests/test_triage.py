"""Label seeding/apply + per-message orchestration (classify → label → wake)."""

from __future__ import annotations

from hermes_inbox_organizer.labels import CATEGORIES, label_name
from hermes_inbox_organizer.labels_apply import apply_category, ensure_labels
from hermes_inbox_organizer.triage import process_message

LABEL_IDS = {label_name(c): f"L{c.sort_order}" for c in CATEGORIES}


class _Exec:
    def __init__(self, val):
        self._val = val

    def execute(self):
        return self._val


class _Labels:
    def __init__(self, existing, created_log):
        self._existing = existing
        self._created = created_log
        self._n = [0]

    def list(self, userId):
        return _Exec({"labels": [{"name": n, "id": i} for n, i in self._existing.items()]})

    def create(self, userId, body):
        self._n[0] += 1
        self._created.append(body["name"])
        return _Exec({"id": f"new-{self._n[0]}", "name": body["name"]})


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
    def __init__(self, modify_log):
        self._modify = modify_log

    def modify(self, userId, id, body):
        self._modify.append({"id": id, **body})
        return _Exec({})


class _Users:
    def __init__(self, svc):
        self._svc = svc

    def labels(self):
        return _Labels(self._svc.existing, self._svc.created)

    def messages(self):
        return _Messages(self._svc.msg, self._svc.modify)

    def threads(self):
        return _Threads(self._svc.modify)


class FakeService:
    def __init__(self, existing=None, msg=None):
        self.existing = existing or {}
        self.msg = msg or {}
        self.created: list[str] = []
        self.modify: list[dict] = []

    def users(self):
        return _Users(self)


def test_ensure_labels_creates_missing() -> None:
    svc = FakeService(existing={"1: To Respond": "L_TR"})
    out = ensure_labels(svc)
    assert len(out) == 8
    assert out["1: To Respond"] == "L_TR"  # kept existing
    assert "8: Marketing" in svc.created  # created the rest


def test_apply_to_respond_keeps_inbox() -> None:
    svc = FakeService()
    apply_category(svc, "m1", "To Respond", LABEL_IDS)
    body = svc.modify[0]
    assert body["addLabelIds"] == ["L1"]
    assert "INBOX" not in body["removeLabelIds"]
    assert "L2" in body["removeLabelIds"]  # other category labels removed


def test_apply_marketing_archives() -> None:
    svc = FakeService()
    apply_category(svc, "m1", "Marketing", LABEL_IDS)
    body = svc.modify[0]
    assert body["addLabelIds"] == ["L8"]
    assert "INBOX" in body["removeLabelIds"]


def _msg(thread_id="t1", frm="a@x.com", subject="Q"):
    return {
        "id": "m1",
        "threadId": thread_id,
        "payload": {"headers": [{"name": "From", "value": frm}, {"name": "Subject", "value": subject}]},
    }


def test_process_message_to_respond_wakes() -> None:
    svc = FakeService(msg=_msg())
    woke: list[dict] = []
    cat = process_message(
        message_id="m1", account_id="acct", service=svc, label_ids=LABEL_IDS,
        classify_fn=lambda parsed: "To Respond",
        wake_fn=lambda **kw: woke.append(kw),
    )
    assert cat == "To Respond"
    assert len(woke) == 1 and woke[0]["thread_id"] == "t1"
    assert svc.modify[0]["addLabelIds"] == ["L1"]


def test_process_message_marketing_no_wake() -> None:
    svc = FakeService(msg=_msg())
    woke: list[dict] = []
    cat = process_message(
        message_id="m1", account_id="acct", service=svc, label_ids=LABEL_IDS,
        classify_fn=lambda parsed: "Marketing",
        wake_fn=lambda **kw: woke.append(kw),
    )
    assert cat == "Marketing"
    assert woke == []  # not a reply-worthy category
    assert "INBOX" in svc.modify[0]["removeLabelIds"]


def test_apply_category_thread_level_replaces_siblings() -> None:
    # With a thread_id, the modify targets the THREAD so older messages can't keep
    # a stale category; without one it falls back to a message-level modify.
    svc = FakeService()
    apply_category(svc, "m3", "To Respond", LABEL_IDS, thread_id="t1")
    body = svc.modify[0]
    assert body["id"] == "t1"  # thread-level, not "m3"
    assert body["addLabelIds"] == ["L1"]
    assert "L7" in body["removeLabelIds"]  # Actioned cleared off the whole thread
    assert "INBOX" not in body["removeLabelIds"]


def test_reclassify_clears_stale_actioned_across_thread() -> None:
    # Reported bug: a thread the sent-handler moved to "7: Actioned" gets a new
    # reply classified "To Respond". The new label must REPLACE Actioned across the
    # whole thread (thread-level modify), not sit beside it on the newer message.
    svc = FakeService(msg=_msg())
    process_message(
        message_id="m3", account_id="acct", service=svc, label_ids=LABEL_IDS,
        classify_fn=lambda parsed: "To Respond",
        wake_fn=lambda **kw: None,
    )
    body = svc.modify[0]
    assert body["id"] == "t1"  # applied to the thread, not message "m3"
    assert body["addLabelIds"] == ["L1"]  # To Respond
    assert "L7" in body["removeLabelIds"]  # Actioned no longer accurate → removed
    assert "INBOX" not in body["removeLabelIds"]  # To Respond stays in the inbox


def test_process_message_persists_classification_and_thread_state(tmp_path) -> None:
    import contextlib

    from hermes_inbox_organizer import db

    svc = FakeService(msg=_msg())
    with contextlib.closing(db.connect(tmp_path / "state.db")) as conn:
        cat = process_message(
            message_id="m1", account_id="acct@x.com", service=svc, label_ids=LABEL_IDS,
            classify_fn=lambda parsed: "To Respond", wake_fn=lambda **kw: None, conn=conn,
        )
        assert cat == "To Respond"
        cm = conn.execute(
            "SELECT * FROM classified_messages WHERE account='acct@x.com' AND message_id='m1'"
        ).fetchone()
        assert cm["category"] == "To Respond"
        assert cm["thread_id"] == "t1" and cm["from_addr"] == "a@x.com" and cm["subject"] == "Q"
        ts = db.get_thread_state(conn, "acct@x.com", "t1")
        assert ts["last_category"] == "To Respond" and ts["last_message_id"] == "m1"


def test_process_message_without_conn_skips_db(tmp_path) -> None:
    # No conn → no persistence (back-compat for callers that don't pass one).
    svc = FakeService(msg=_msg())
    cat = process_message(
        message_id="m1", account_id="acct@x.com", service=svc, label_ids=LABEL_IDS,
        classify_fn=lambda parsed: "FYI",
    )
    assert cat == "FYI"  # returns normally, nothing recorded
