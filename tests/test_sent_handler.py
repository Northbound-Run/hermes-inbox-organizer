"""sent-handler: replied-to-To-Respond → Actioned; new outbound → Awaiting Reply."""

from __future__ import annotations

import base64

from hermes_inbox_organizer.labels import CATEGORIES, label_name
from hermes_inbox_organizer.modules import InlineExecutor, Module, ModuleRegistry
from hermes_inbox_organizer.sent_handler import handle_sent, sent_awaits_reply

LABEL_IDS = {label_name(c): f"L{c.sort_order}" for c in CATEGORIES}  # L1..L8


class _SentRec(Module):
    name = "rec"

    def __init__(self) -> None:
        self.sent: list = []

    def on_sent(self, event) -> None:
        self.sent.append(event)


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


class _Exec:
    def __init__(self, val):
        self._val = val

    def execute(self):
        return self._val


class _Messages:
    def __init__(self, thread_id, body=""):
        self._tid = thread_id
        self._body = body

    def get(self, userId, id, format):
        if not self._tid:
            return _Exec({})
        msg: dict = {"threadId": self._tid}
        if self._body:
            msg["payload"] = {"mimeType": "text/plain", "body": {"data": _b64(self._body)}}
        return _Exec(msg)


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
    def __init__(self, tid, thread, modify_log, body=""):
        self._tid, self._thread, self._modify, self._body = tid, thread, modify_log, body

    def messages(self):
        return _Messages(self._tid, self._body)

    def threads(self):
        return _Threads(self._thread, self._modify)


class FakeService:
    def __init__(self, thread_id="t1", thread=None, sent_body=""):
        self.thread_id = thread_id
        self.thread = thread or {"messages": []}
        self.sent_body = sent_body
        self.modify: list[dict] = []

    def users(self):
        return _Users(self.thread_id, self.thread, self.modify, self.sent_body)


def test_terminal_reply_to_to_respond_thread_becomes_actioned() -> None:
    # Replied to a flagged thread with no open ask of your own → you closed it.
    svc = FakeService(
        thread={"messages": [{"labelIds": ["L1", "INBOX"]}]},  # has To Respond
        sent_body="Thanks, got it — all set on my end.",
    )
    target = handle_sent(message_id="s1", account_id="a", service=svc, label_ids=LABEL_IDS)
    assert target == "Actioned"
    body = svc.modify[0]
    assert body["addLabelIds"] == ["L7"]  # Actioned
    assert "INBOX" in body["removeLabelIds"] and "L1" in body["removeLabelIds"]


def test_reply_with_open_question_stays_awaiting_reply() -> None:
    # Even on a flagged thread, if your reply asks something you're now waiting.
    svc = FakeService(
        thread={"messages": [{"labelIds": ["L1", "INBOX"]}]},  # has To Respond
        sent_body="Nothing at all. Could you give me an update on the business?\n\nMatthew",
    )
    target = handle_sent(message_id="s1", account_id="a", service=svc, label_ids=LABEL_IDS)
    assert target == "Awaiting Reply"
    body = svc.modify[0]
    assert body["addLabelIds"] == ["L6"]  # Awaiting Reply
    # To Respond is still cleared (you no longer owe *them* a response).
    assert "INBOX" in body["removeLabelIds"] and "L1" in body["removeLabelIds"]


def test_new_outbound_becomes_awaiting_reply() -> None:
    svc = FakeService(thread={"messages": [{"labelIds": ["SENT"]}]})  # no To Respond
    target = handle_sent(message_id="s1", account_id="a", service=svc, label_ids=LABEL_IDS)
    assert target == "Awaiting Reply"
    body = svc.modify[0]
    assert body["addLabelIds"] == ["L6"]  # Awaiting Reply
    assert "INBOX" in body["removeLabelIds"]
    assert "L6" not in body["removeLabelIds"]  # never remove the label we just added


def test_reply_clears_other_stale_category() -> None:
    # A thread carrying an inbound "2: FYI" that you reply to must not keep FYI
    # beside the new state — every other category is cleared, not just To Respond.
    svc = FakeService(
        thread={"messages": [{"labelIds": ["L1", "L2", "INBOX"]}]},  # To Respond + FYI
        sent_body="Thanks, got it — all set.",
    )
    target = handle_sent(message_id="s1", account_id="a", service=svc, label_ids=LABEL_IDS)
    assert target == "Actioned"
    body = svc.modify[0]
    assert body["addLabelIds"] == ["L7"]  # Actioned
    assert "L1" in body["removeLabelIds"]  # To Respond cleared
    assert "L2" in body["removeLabelIds"]  # stale FYI cleared
    assert "L7" not in body["removeLabelIds"]  # not the target


def test_no_thread_id_is_noop() -> None:
    svc = FakeService(thread_id="")
    assert handle_sent(message_id="s1", account_id="a", service=svc, label_ids=LABEL_IDS) == ""
    assert svc.modify == []


def test_handle_sent_dispatches_sent_event() -> None:
    rec = _SentRec()
    reg = ModuleRegistry([rec], executor=InlineExecutor())
    svc = FakeService(
        thread={"messages": [{"labelIds": ["L1", "INBOX"]}]},
        sent_body="Thanks, got it — all set.",
    )
    target = handle_sent(
        message_id="s1", account_id="a@x.com", service=svc, label_ids=LABEL_IDS, registry=reg
    )
    assert target == "Actioned"
    assert len(rec.sent) == 1
    ev = rec.sent[0]
    assert (ev.target_category, ev.message_id, ev.account_id) == ("Actioned", "s1", "a@x.com")
    assert ev.thread_id == "t1"


def test_handle_sent_no_dispatch_on_noop() -> None:
    rec = _SentRec()
    reg = ModuleRegistry([rec], executor=InlineExecutor())
    svc = FakeService(thread_id="")  # no thread → no-op
    assert (
        handle_sent(message_id="s1", account_id="a", service=svc, label_ids=LABEL_IDS, registry=reg)
        == ""
    )
    assert rec.sent == []  # no move happened → no SentEvent


def test_sent_awaits_reply_heuristic() -> None:
    # Question not at the very end (signature follows) still counts.
    assert sent_awaits_reply("Could you give me an update?\n\nMatthew\nmatthewhall.com")
    # Request cue without a "?".
    assert sent_awaits_reply("Sounds good. Please send over the deck when you can.")
    # Terminal reply → not awaiting.
    assert not sent_awaits_reply("Thanks, got it — all set.")
    # A "?" only in the *quoted* original must not count as your ask.
    quoted = "Sounds great, thanks!\n\nOn Thu, May 28 2026 John wrote:\n> Can you confirm?\n"
    assert not sent_awaits_reply(quoted)
