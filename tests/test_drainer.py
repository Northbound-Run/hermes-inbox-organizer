"""History drainer (pagination, INBOX routing, dedupe, 404→StaleCursor) + wake_draft."""

from __future__ import annotations

import pytest

from hermes_inbox_organizer.draft_trigger import NoopPoster, wake_draft
from hermes_inbox_organizer.drainer import StaleCursor, drain_history


class _Exec:
    def __init__(self, val):
        self._val = val

    def execute(self):
        return self._val


class _History:
    def __init__(self, by_token):
        self._by = by_token

    def list(self, userId, startHistoryId, historyTypes, pageToken=None):
        return _Exec(self._by[pageToken])


class _Users:
    def __init__(self, history):
        self._history = history

    def history(self):
        return self._history


class _Service:
    def __init__(self, history):
        self._history = history

    def users(self):
        return _Users(self._history)


def _added(mid, labels):
    return {"messagesAdded": [{"message": {"id": mid, "labelIds": labels}}]}


def test_drain_collects_inbox_ids_across_pages_and_advances_cursor() -> None:
    by_token = {
        None: {"historyId": "100", "history": [_added("m1", ["INBOX"]), _added("m2", ["SENT"])], "nextPageToken": "p1"},
        "p1": {"historyId": "120", "history": [_added("m3", ["INBOX", "UNREAD"]), _added("m1", ["INBOX"])]},
    }
    processed: list[str] = []
    cursor = drain_history(
        service=_Service(_History(by_token)),
        start_history_id="90",
        process_fn=processed.append,
    )
    assert processed == ["m1", "m3"]  # m2 (SENT) skipped; m1 deduped
    assert cursor == "120"


def test_drain_routes_sent_vs_inbox() -> None:
    by_token = {
        None: {
            "historyId": "100",
            "history": [_added("m1", ["INBOX"]), _added("s1", ["SENT"]), _added("s2", ["SENT", "IMPORTANT"])],
        }
    }
    inbox: list[str] = []
    sent: list[str] = []
    drain_history(
        service=_Service(_History(by_token)),
        start_history_id="90",
        process_fn=inbox.append,
        sent_fn=sent.append,
    )
    assert inbox == ["m1"]
    assert sent == ["s1", "s2"]


def test_drain_404_raises_stale_cursor() -> None:
    class _Resp:
        status = 404

    class _Err(Exception):
        resp = _Resp()

    class _H:
        def list(self, **kwargs):
            raise _Err()

    with pytest.raises(StaleCursor):
        drain_history(service=_Service(_H()), start_history_id="90", process_fn=lambda x: None)


def test_wake_draft_posts_instruction() -> None:
    poster = NoopPoster()
    ok = wake_draft(account_id="a1", thread_id="t1", sender="x@y.com", subject="Hi", poster=poster)
    assert ok is True
    assert len(poster.posted) == 1 and "t1" in poster.posted[0]


def test_wake_draft_returns_false_on_poster_error() -> None:
    class Boom:
        def post(self, content):
            raise RuntimeError("api down")

    assert wake_draft(account_id="a1", thread_id="t1", poster=Boom()) is False
