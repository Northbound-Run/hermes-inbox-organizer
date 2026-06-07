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


def test_runtime_threads_registry_into_drain(tmp_path) -> None:
    # A registry passed to the runtime must reach process_message in the real
    # drain path: classify via the registry + dispatch on_inbound with the event.
    from hermes_inbox_organizer.modules import InlineExecutor, Module, ModuleRegistry

    class _Rec(Module):
        name = "rec"

        def __init__(self) -> None:
            self.inbound: list = []

        def on_inbound(self, event) -> None:
            self.inbound.append(event)

    pages, msg = _msg_pages()
    dbp = _dbp(tmp_path)
    _seed_cursor(dbp, "a@gmail.com", "150")
    account = Account(email="a@gmail.com", build_service=lambda: FakeService(pages, msg, []))
    account.label_ids = LABEL_IDS

    rec = _Rec()
    reg = ModuleRegistry([rec], classify_fn=lambda parsed: "FYI", executor=InlineExecutor())
    rt = InboxRuntime(
        accounts=[account], project="p", topic="t", subscription="s", sa_key_path="unused",
        db_path=dbp, registry=reg,
    )

    rt.handle_notification(GmailNotification(email_address="a@gmail.com", history_id=200))

    assert len(rec.inbound) == 1
    ev = rec.inbound[0]
    assert (ev.account_id, ev.message_id, ev.thread_id, ev.category) == (
        "a@gmail.com", "m1", "t1", "FYI",
    )


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


def test_dedup_wake_skips_after_draft_recorded(tmp_path) -> None:
    # AC2: once a draft exists (gmail_draft_id set), the thread is never re-dispatched.
    woke: list[str] = []
    dbp = _dbp(tmp_path)
    rt = InboxRuntime(
        accounts=[], project="p", topic="t", subscription="s", sa_key_path="x",
        db_path=dbp, wake_fn=lambda **kw: woke.append(kw["thread_id"]),
    )
    rt._dedup_wake(thread_id="t1", account_id="a", sender="s@x", subject="Q")  # claim + wake
    with contextlib.closing(db.connect(dbp)) as conn:
        db.set_draft_id(conn, "a", "t1", "draft-1")  # fulfilled
    rt._dedup_wake(thread_id="t1", account_id="a", sender="s@x", subject="Q")  # fulfilled -> skip
    assert woke == ["t1"]


def test_retry_redispatches_unfulfilled_after_ttl(tmp_path) -> None:
    # AC4: an unfulfilled draft past the TTL is re-dispatched via the KEYWORD-ONLY
    # wake_fn contract (DB column from_addr -> kwarg sender) — proves the B1 fix.
    import hermes_inbox_organizer.runtime as rt_mod

    woke: list[dict] = []
    dbp = _dbp(tmp_path)
    rt = InboxRuntime(
        accounts=[], project="p", topic="t", subscription="s", sa_key_path="x",
        db_path=dbp, wake_fn=lambda **kw: woke.append(kw),
    )
    with contextlib.closing(db.connect(dbp)) as conn:
        db.claim_draft(conn, "a@x.com", "t1", from_addr="al@x.com", subject="Q",
                       ttl_ms=rt_mod.RETRY_TTL_MS, max_attempts=rt_mod.MAX_DRAFT_ATTEMPTS, now_ms=1)
    rt._retry_unfulfilled_drafts()  # real now >> 1 + TTL -> eligible
    assert len(woke) == 1
    w = woke[0]
    assert (w["account_id"], w["thread_id"], w["sender"], w["subject"]) == ("a@x.com", "t1", "al@x.com", "Q")
    assert isinstance(w["instruction"], str) and "t1" in w["instruction"]  # brief built + passed (Phase 2)


def test_retry_skips_exhausted_and_fulfilled(tmp_path) -> None:
    # AC5: exhausted (max attempts) and fulfilled drafts are never retried.
    woke: list[dict] = []
    dbp = _dbp(tmp_path)
    rt = InboxRuntime(
        accounts=[], project="p", topic="t", subscription="s", sa_key_path="x",
        db_path=dbp, wake_fn=lambda **kw: woke.append(kw),
    )
    with contextlib.closing(db.connect(dbp)) as conn:
        for now in (1, 1000, 2000):  # exhaust attempts (3), all in the past
            db.claim_draft(conn, "a@x.com", "t1", ttl_ms=1, max_attempts=3, now_ms=now)
        db.claim_draft(conn, "a@x.com", "t2", ttl_ms=1, max_attempts=3, now_ms=1)
        db.set_draft_id(conn, "a@x.com", "t2", "d-2")  # fulfilled
    rt._retry_unfulfilled_drafts()
    assert woke == []


def test_dedup_wake_falls_back_to_minimal_when_brief_build_fails(tmp_path, monkeypatch) -> None:
    # A brief-build exception must NOT strand or un-restrict the wake: _build_brief
    # returns None, so _dedup_wake dispatches instruction=None and wake_draft rebuilds the
    # sentinel-bearing (restricted) fallback. Here we pin that None is passed through.
    import hermes_inbox_organizer.brief as brief_mod

    def _boom(*a, **k):
        raise RuntimeError("brief build failed")

    monkeypatch.setattr(brief_mod, "build_draft_brief", _boom)
    woke: list = []
    rt = InboxRuntime(
        accounts=[], project="p", topic="t", subscription="s", sa_key_path="x",
        db_path=_dbp(tmp_path), wake_fn=lambda **kw: woke.append(kw),
    )
    rt._dedup_wake(thread_id="t1", account_id="a", sender="s@x", subject="j")
    assert len(woke) == 1
    assert woke[0]["instruction"] is None  # fallback signal -> wake_draft rebuilds w/ sentinel
