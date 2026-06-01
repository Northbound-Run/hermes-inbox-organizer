"""InboxRuntime: DB cursor persistence + notification → drain → triage → wake."""

from __future__ import annotations

import contextlib

from hermes_inbox_organizer import db
from hermes_inbox_organizer.labels import CATEGORIES, label_name
from hermes_inbox_organizer.pubsub import GmailNotification
from hermes_inbox_organizer.runtime import (
    Account,
    InboxRuntime,
    _is_auth_error,
    should_renew,
)

LABEL_IDS = {label_name(c): f"L{c.sort_order}" for c in CATEGORIES}


def _dbp(tmp_path) -> str:
    return str(tmp_path / "state.db")


def _seed_cursor(dbp: str, email: str, hid: str) -> None:
    with contextlib.closing(db.connect(dbp)) as conn:
        db.set_cursor(conn, email, hid)


def _read_cursor(dbp: str, email: str):
    with contextlib.closing(db.connect(dbp)) as conn:
        return db.get_cursor(conn, email)


def test_should_renew_watch() -> None:
    now = 1_000_000_000_000
    assert should_renew(now, now + 7 * 24 * 3600 * 1000) is False  # fresh
    assert should_renew(now, now + 12 * 3600 * 1000) is True  # within 24h buffer
    assert should_renew(now, now - 1000) is True  # already expired
    assert should_renew(now, 0) is True  # never armed


class _Exec:
    def __init__(self, val):
        self._val = val

    def execute(self):
        return self._val


class _History:
    def __init__(self, pages):
        self._pages = pages

    def list(self, userId, startHistoryId, historyTypes, pageToken=None):
        return _Exec(self._pages[pageToken])


class _Messages:
    def __init__(self, msg, modify_log):
        self._msg = msg
        self._modify = modify_log

    def get(self, userId, id, format):
        return _Exec(self._msg)

    def modify(self, userId, id, body):
        self._modify.append(body)
        return _Exec({})


class _Threads:
    def __init__(self, modify_log):
        self._modify = modify_log

    def modify(self, userId, id, body):
        self._modify.append(body)
        return _Exec({})


class _Users:
    def __init__(self, hist, msgs, threads):
        self._h, self._m, self._t = hist, msgs, threads

    def history(self):
        return self._h

    def messages(self):
        return self._m

    def threads(self):
        return self._t


class FakeService:
    def __init__(self, pages, msg, modify_log):
        self._u = _Users(
            _History(pages), _Messages(msg, modify_log), _Threads(modify_log)
        )

    def users(self):
        return self._u


def _msg_pages():
    pages = {
        None: {
            "historyId": "200",
            "history": [{"messagesAdded": [{"message": {"id": "m1", "labelIds": ["INBOX"]}}]}],
        }
    }
    msg = {
        "id": "m1",
        "threadId": "t1",
        "payload": {"headers": [{"name": "From", "value": "a@x.com"}, {"name": "Subject", "value": "Q"}]},
    }
    return pages, msg


def test_handle_notification_drains_triages_and_advances_cursor(tmp_path) -> None:
    pages, msg = _msg_pages()
    modify_log: list[dict] = []
    woke: list[dict] = []
    dbp = _dbp(tmp_path)
    _seed_cursor(dbp, "a@gmail.com", "150")

    account = Account(email="a@gmail.com", build_service=lambda: FakeService(pages, msg, modify_log))
    account.label_ids = LABEL_IDS

    rt = InboxRuntime(
        accounts=[account], project="p", topic="t", subscription="s", sa_key_path="unused",
        classify_fn=lambda parsed: "To Respond",
        wake_fn=lambda **kw: woke.append(kw),
        db_path=dbp,
    )

    rt.handle_notification(GmailNotification(email_address="a@gmail.com", history_id=200))

    assert woke and woke[0]["thread_id"] == "t1"   # To Respond -> woke a draft
    assert modify_log and modify_log[0]["addLabelIds"] == ["L1"]  # labeled To Respond
    assert _read_cursor(dbp, "a@gmail.com") == "200"  # cursor advanced past the drain
    # the drain connection persisted the classification + thread state
    with contextlib.closing(db.connect(dbp)) as conn:
        row = conn.execute("SELECT category FROM classified_messages WHERE message_id='m1'").fetchone()
        assert row and row["category"] == "To Respond"
        assert db.get_thread_state(conn, "a@gmail.com", "t1")["last_category"] == "To Respond"


def test_handle_notification_routes_to_the_notified_account(tmp_path) -> None:
    pages, msg = _msg_pages()
    a_modify: list[dict] = []
    b_modify: list[dict] = []
    dbp = _dbp(tmp_path)
    _seed_cursor(dbp, "a@gmail.com", "150")
    _seed_cursor(dbp, "b@gmail.com", "150")

    acct_a = Account(email="a@gmail.com", build_service=lambda: FakeService(pages, msg, a_modify))
    acct_b = Account(email="b@gmail.com", build_service=lambda: FakeService(pages, msg, b_modify))
    acct_a.label_ids = LABEL_IDS
    acct_b.label_ids = LABEL_IDS

    rt = InboxRuntime(
        accounts=[acct_a, acct_b], project="p", topic="t", subscription="s", sa_key_path="x",
        classify_fn=lambda parsed: "FYI", db_path=dbp,
    )

    rt.handle_notification(GmailNotification(email_address="b@gmail.com", history_id=200))

    assert b_modify and not a_modify  # only the notified mailbox is mutated
    assert _read_cursor(dbp, "b@gmail.com") == "200"  # B's cursor advanced
    assert _read_cursor(dbp, "a@gmail.com") == "150"  # A's cursor untouched


def test_handle_notification_ignores_unknown_account(tmp_path) -> None:
    dbp = _dbp(tmp_path)
    built: list[int] = []
    acct = Account(email="known@gmail.com", build_service=lambda: built.append(1))

    rt = InboxRuntime(
        accounts=[acct], project="p", topic="t", subscription="s", sa_key_path="x", db_path=dbp
    )
    rt.handle_notification(GmailNotification(email_address="other@gmail.com", history_id=5))

    assert built == []  # unknown account never builds a service
    assert _read_cursor(dbp, "known@gmail.com") is None  # nothing drained or written


def test_add_account_hot_adds_arms_and_seeds_cursor(tmp_path, monkeypatch) -> None:
    import hermes_inbox_organizer.labels_apply as la
    import hermes_inbox_organizer.runtime as rt_mod

    monkeypatch.setattr(la, "ensure_labels", lambda svc: {"1: To Respond": "L1"})
    monkeypatch.setattr(rt_mod, "arm_watch", lambda svc, topic: ("999", 123))

    dbp = _dbp(tmp_path)
    rt = InboxRuntime(accounts=[], project="p", topic="t", subscription="s", sa_key_path="x", db_path=dbp)
    acct = Account(email="new@gmail.com", build_service=lambda: object())

    assert rt.add_account(acct) is True
    assert "new@gmail.com" in rt._by_email           # registered for routing
    assert acct.label_ids == {"1: To Respond": "L1"}  # labels ensured
    assert acct.watch_expiration == 123               # watch armed
    assert _read_cursor(dbp, "new@gmail.com") == "999"  # cursor seeded from watch
    assert rt.add_account(acct) is False              # idempotent on email


def test_dedup_wake_fires_once_per_thread(tmp_path) -> None:
    woke: list[str] = []
    rt = InboxRuntime(
        accounts=[], project="p", topic="t", subscription="s", sa_key_path="x",
        db_path=_dbp(tmp_path), wake_fn=lambda **kw: woke.append(kw["thread_id"]),
    )
    rt._dedup_wake(thread_id="t1", account_id="a", subject="x", sender="y")
    rt._dedup_wake(thread_id="t1", account_id="a", subject="x", sender="y")  # dup -> skipped
    rt._dedup_wake(thread_id="t2", account_id="a", subject="x", sender="y")
    assert woke == ["t1", "t2"]


def test_dedup_wake_is_per_account(tmp_path) -> None:
    # Same thread id under two accounts is independent (draft_requests keys on account+thread).
    woke: list[tuple] = []
    rt = InboxRuntime(
        accounts=[], project="p", topic="t", subscription="s", sa_key_path="x",
        db_path=_dbp(tmp_path),
        wake_fn=lambda **kw: woke.append((kw["account_id"], kw["thread_id"])),
    )
    rt._dedup_wake(thread_id="t1", account_id="a")
    rt._dedup_wake(thread_id="t1", account_id="b")  # different account -> still fires
    rt._dedup_wake(thread_id="t1", account_id="a")  # dup -> skipped
    assert woke == [("a", "t1"), ("b", "t1")]


def test_is_auth_error_distinguishes_dead_creds_from_transient() -> None:
    refresh = type("RefreshError", (Exception,), {})("bad")
    assert _is_auth_error(refresh)                                  # by class name
    assert _is_auth_error(Exception("invalid_grant: revoked"))      # by marker
    e401 = Exception("x"); e401.resp = type("R", (), {"status": 401})()
    e403 = Exception("x"); e403.resp = type("R", (), {"status": 403})()
    assert _is_auth_error(e401) and _is_auth_error(e403)            # by HTTP status
    e500 = Exception("x"); e500.resp = type("R", (), {"status": 500})()
    assert not _is_auth_error(e500)
    assert not _is_auth_error(Exception("temporary network blip"))


class _StoppableService:
    def __init__(self):
        self.stopped = False

    def users(self):
        outer = self

        class U:
            def stop(self, userId):
                class E:
                    def execute(_self):
                        outer.stopped = True
                        return {}
                return E()
        return U()


def test_remove_account_drops_routing_and_stops_watch(tmp_path) -> None:
    svc = _StoppableService()
    acct = Account(email="a@gmail.com", build_service=lambda: svc)
    rt = InboxRuntime(
        accounts=[acct], project="p", topic="t", subscription="s", sa_key_path="x", db_path=_dbp(tmp_path)
    )
    assert rt.remove_account("a@gmail.com") is True
    assert "a@gmail.com" not in rt._by_email and svc.stopped is True
    assert rt.remove_account("a@gmail.com") is False  # idempotent


class _AuthFailService:
    """history.list().execute() raises a revoked-credential error; supports watch stop."""

    def users(self):
        class U:
            def history(self):
                class H:
                    def list(self, **_k):
                        class E:
                            def execute(self):
                                raise Exception("invalid_grant: Token has been expired or revoked.")
                        return E()
                return H()

            def stop(self, userId):
                class E:
                    def execute(self):
                        return {}
                return E()
        return U()


def test_handle_notification_auth_failure_flags_and_removes(tmp_path) -> None:
    flagged: list[str] = []
    dbp = _dbp(tmp_path)
    _seed_cursor(dbp, "a@gmail.com", "150")
    acct = Account(email="a@gmail.com", build_service=lambda: _AuthFailService())
    acct.label_ids = LABEL_IDS
    rt = InboxRuntime(
        accounts=[acct], project="p", topic="t", subscription="s", sa_key_path="x",
        classify_fn=lambda parsed: "FYI", on_auth_failure=lambda e: flagged.append(e), db_path=dbp,
    )
    # must NOT raise (so the message gets acked, not nacked into a storm)
    rt.handle_notification(GmailNotification(email_address="a@gmail.com", history_id=200))
    assert flagged == ["a@gmail.com"]              # owner flagged once for reconnect
    assert "a@gmail.com" not in rt._by_email        # dropped from routing
    assert _read_cursor(dbp, "a@gmail.com") == "150"  # cursor NOT advanced (drain failed)


def test_poll_once_drains_from_cursor(tmp_path) -> None:
    # The reconciler drains from the stored cursor with NO notification — this is
    # what catches mail when Pub/Sub drops/delays a push.
    pages, msg = _msg_pages()
    modify_log: list[dict] = []
    dbp = _dbp(tmp_path)
    _seed_cursor(dbp, "a@gmail.com", "150")
    account = Account(email="a@gmail.com", build_service=lambda: FakeService(pages, msg, modify_log))
    account.label_ids = LABEL_IDS
    rt = InboxRuntime(
        accounts=[account], project="p", topic="t", subscription="s", sa_key_path="x",
        classify_fn=lambda parsed: "FYI", db_path=dbp,
    )
    rt._poll_once()
    assert modify_log and modify_log[0]["addLabelIds"] == ["L2"]  # FYI applied
    assert _read_cursor(dbp, "a@gmail.com") == "200"  # cursor advanced by the poll


def test_poll_once_skips_account_without_cursor(tmp_path) -> None:
    pages, msg = _msg_pages()
    modify_log: list[dict] = []
    dbp = _dbp(tmp_path)
    account = Account(email="a@gmail.com", build_service=lambda: FakeService(pages, msg, modify_log))
    account.label_ids = LABEL_IDS
    rt = InboxRuntime(
        accounts=[account], project="p", topic="t", subscription="s", sa_key_path="x",
        classify_fn=lambda parsed: "FYI", db_path=dbp,
    )
    rt._poll_once()  # no cursor seeded yet (start() seeds from watch()) -> skip
    assert modify_log == []
    assert _read_cursor(dbp, "a@gmail.com") is None


def test_poll_once_auth_failure_does_not_deadlock(tmp_path) -> None:
    # The poll holds self._lock; an auth failure routes through remove_account,
    # which re-acquires it. The reentrant lock lets this complete (a plain Lock
    # would deadlock — and the gateway's _on_message takes the lock the same way).
    flagged: list[str] = []
    dbp = _dbp(tmp_path)
    _seed_cursor(dbp, "a@gmail.com", "150")
    account = Account(email="a@gmail.com", build_service=lambda: _AuthFailService())
    account.label_ids = LABEL_IDS
    rt = InboxRuntime(
        accounts=[account], project="p", topic="t", subscription="s", sa_key_path="x",
        classify_fn=lambda parsed: "FYI", on_auth_failure=lambda e: flagged.append(e), db_path=dbp,
    )
    rt._poll_once()
    assert flagged == ["a@gmail.com"]              # flagged for reconnect
    assert "a@gmail.com" not in rt._by_email        # dropped from routing
    assert _read_cursor(dbp, "a@gmail.com") == "150"  # cursor not advanced
