"""Autonomous runtime: Gmail watch() + Pub/Sub streaming pull → drain → triage.

Ties the components into the live loop. On a notification we drain Gmail history
since the stored cursor, classify+label each new INBOX message, and wake a draft
for "To Respond" — then advance the cursor (only after processing, so a crash
re-drains rather than skips). Live google calls (watch, SubscriberClient) are
lazy-imported; ``handle_notification`` + cursor mgmt are seamed for tests.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import db
from .config import get_config
from .drainer import StaleCursor, drain_history
from .pubsub import decode_gmail_notification
from .triage import process_message

logger = logging.getLogger(__name__)


# Gmail watch() expires in ~7 days; re-arm well before then.
RENEWAL_BUFFER_MS = 24 * 60 * 60 * 1000  # renew when within 24h of expiry
RENEWAL_CHECK_INTERVAL_S = 6 * 60 * 60  # check every 6h

# Pub/Sub pull is best-effort (Gmail may drop/delay notifications), so a polling
# reconciler re-drains each account from its stored cursor on this interval to
# catch anything the push path missed. Cursor-based, so it never reprocesses.
POLL_INTERVAL_S = 5 * 60

# Draft dispatch dedup + retry. A dispatched draft is "in flight" for RETRY_TTL_MS;
# an unfulfilled draft past that window is retried, up to MAX_DRAFT_ATTEMPTS. All
# three dispatch paths (notify, poll reconciler, retry loop) funnel through the
# atomic db.claim_draft, so exactly one wins per window. RETRY_TTL_MS MUST be
# >= POLL_INTERVAL_S so the reconciler never re-dispatches a still-running turn.
RETRY_TTL_MS = 15 * 60 * 1000        # 15 min (>= POLL_INTERVAL_S; >> wake-turn p99)
MAX_DRAFT_ATTEMPTS = 3
DRAFT_RETRY_INTERVAL_S = 5 * 60      # re-scan unfulfilled drafts every 5 min


def arm_watch(service: Any, topic: str) -> tuple[str, int]:
    """Arm Gmail watch on INBOX+SENT → topic; return (start historyId, expiration_ms)."""
    resp = (
        service.users()
        .watch(userId="me", body={"topicName": topic, "labelIds": ["INBOX", "SENT"]})
        .execute()
    )
    return str(resp.get("historyId", "")), int(resp.get("expiration", 0))


def should_renew(now_ms: int, expiration_ms: int, buffer_ms: int = RENEWAL_BUFFER_MS) -> bool:
    """True if the watch is missing/expired or within the renewal buffer of expiry."""
    return expiration_ms <= 0 or now_ms >= (expiration_ms - buffer_ms)


def build_subscriber(sa_key_path: str):
    from google.cloud import pubsub_v1

    return pubsub_v1.SubscriberClient.from_service_account_file(sa_key_path)


def _is_auth_error(exc: Exception) -> bool:
    """True for a dead/revoked OAuth credential (vs a transient error).

    Detected without importing google libs: RefreshError by class name, the
    ``invalid_grant`` marker, or a 401/403 HttpError status.
    """
    if type(exc).__name__ == "RefreshError" or "invalid_grant" in str(exc):
        return True
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    try:
        return int(status) in (401, 403)
    except (TypeError, ValueError):
        return False


@dataclass
class Account:
    """Per-account runtime state.

    ``build_service`` mints an auto-refreshing Gmail service for *this* account.
    The history cursor lives in the DB keyed by ``email`` (so drains never cross
    mailboxes). ``label_ids`` + ``watch_expiration`` are filled in by
    :meth:`InboxRuntime.start`.
    """

    email: str
    build_service: Callable[[], Any]
    label_ids: dict[str, str] = field(default_factory=dict)
    watch_expiration: int = 0  # ms; 0 until watch armed


class InboxRuntime:
    def __init__(
        self,
        *,
        accounts: list[Account],
        project: str,
        topic: str,
        subscription: str,
        sa_key_path: str,
        classify_fn: Optional[Callable[[dict], str]] = None,
        wake_fn: Optional[Callable[..., Any]] = None,
        db_path: Optional[str] = None,
        on_auth_failure: Optional[Callable[[str], None]] = None,
        registry: Optional[Any] = None,
    ) -> None:
        self._accounts = list(accounts)
        self._by_email = {a.email: a for a in self._accounts}
        self._project = project
        self._topic = topic
        self._subscription = subscription
        self._sa_key_path = sa_key_path
        self._classify_fn = classify_fn
        self._wake_fn = wake_fn
        self._db_path = db_path or get_config().db_path
        self._on_auth_failure = on_auth_failure
        self._registry = registry  # module registry; None => legacy single-classifier path
        self._auth_failed: set[str] = set()  # accounts already flagged for reconnect
        self._future = None
        # Reentrant: a drain under the lock can call remove_account (auth failure)
        # or the poller can drain under the lock — both re-acquire on the same thread.
        self._lock = threading.RLock()  # serialize drains (one at a time across threads)

    def _db(self) -> sqlite3.Connection:
        """Open a short-lived DB connection (one per op → safe across daemon threads)."""
        return db.connect(self._db_path)

    def _dedup_wake(self, **kw: Any) -> None:
        """Atomically claim + dispatch a draft once per (account, thread).

        The single ``db.claim_draft`` gate is shared by all three dispatch paths
        (this notify path, the poll reconciler — also routed through here — and the
        retry loop), so a fulfilled, in-flight, or exhausted thread is skipped
        without a second agent turn, with no reliance on the RLock for correctness.
        The claim records the attempt BEFORE dispatch, so a turn that dies mid-flight
        is retried by ``_draft_retry_loop`` after the TTL.
        """
        if self._wake_fn is None:
            return
        tid = kw.get("thread_id", "")
        acct = kw.get("account_id", "")
        if not (tid and acct):
            # No thread/account => nothing to draft against; skip rather than
            # dispatch an un-deduped, un-retryable wake (drafting needs a thread id).
            logger.warning(
                "inbox runtime: draft wake missing account/thread (%r/%r); skipping", acct, tid
            )
            return
        with contextlib.closing(self._db()) as conn:
            won = db.claim_draft(
                conn, acct, tid,
                from_addr=kw.get("sender", ""), subject=kw.get("subject", ""),
                ttl_ms=RETRY_TTL_MS, max_attempts=MAX_DRAFT_ATTEMPTS, now_ms=db.now_ms(),
            )
            if not won:
                self._log_draft_skip(conn, acct, tid)
                return
            instruction = self._build_brief(conn, acct, tid, kw.get("sender", ""), kw.get("subject", ""))
        # Claim won. wake_fn (wake_draft) is fire-and-forget; a failure inside it is
        # recovered by _draft_retry_loop after the TTL (the claim already recorded the
        # attempt), so the return value is intentionally not consulted here.
        self._wake_fn(instruction=instruction, **kw)

    def _build_brief(self, conn: sqlite3.Connection, acct: str, tid: str, sender: str, subject: str):
        """Context-rich wake instruction; falls back to the minimal one (None) on failure."""
        try:
            from .brief import build_draft_brief
            from .config import get_config

            return build_draft_brief(
                conn, account_id=acct, thread_id=tid, sender=sender, subject=subject,
                research=get_config().draft_research_enabled,
            )
        except Exception:
            logger.exception("inbox runtime: brief build failed for thread %s; using minimal wake", tid)
            return None

    def _log_draft_skip(self, conn: sqlite3.Connection, acct: str, tid: str) -> None:
        """Explain a lost claim (fulfilled / in-flight / exhausted); exhausted at WARNING."""
        row = db.get_draft_request(conn, acct, tid)
        if row is None:
            return
        if row["gmail_draft_id"]:
            logger.info("inbox runtime: thread %s already drafted (%s); skipping", tid, row["gmail_draft_id"])
        elif row["attempts"] >= MAX_DRAFT_ATTEMPTS:
            logger.warning(
                "inbox runtime: thread %s exhausted draft retries (%d attempts, no draft)",
                tid, row["attempts"],
            )
        else:
            logger.info("inbox runtime: thread %s draft in flight; skipping", tid)

    def start(self) -> None:
        from .labels_apply import ensure_labels

        if not get_config().labels_enabled:
            logger.info(
                "inbox runtime: label system disabled (INBOX_LABELS_ENABLED) — "
                "classify/record/draft only, no mailbox mutations"
            )

        # Arm each account independently: seed its labels (unless the label system
        # is disabled), watch INBOX+SENT, and set its cursor. A broken account is
        # dropped from routing rather than taking down the others (one shared
        # subscription still feeds the rest).
        for account in list(self._accounts):
            try:
                service = account.build_service()
                account.label_ids = ensure_labels(service) if get_config().labels_enabled else {}
                hid, account.watch_expiration = arm_watch(service, self._topic)
                with contextlib.closing(self._db()) as conn:
                    if db.get_cursor(conn, account.email) is None:
                        db.set_cursor(conn, account.email, hid)
                logger.info("inbox runtime: armed watch for %s", account.email)
            except Exception:
                logger.exception("inbox runtime: failed to arm %s; skipping", account.email)
                self._by_email.pop(account.email, None)

        if not self._by_email:
            logger.warning("inbox runtime: no accounts armed; not subscribing")
            return

        from google.cloud import pubsub_v1

        sub = build_subscriber(self._sa_key_path)
        sub_path = sub.subscription_path(self._project, self._subscription)
        # max_messages=1: one callback at a time (with self._lock) so concurrent
        # notifications can't double-drain before a cursor advances.
        flow = pubsub_v1.types.FlowControl(max_messages=1)
        self._future = sub.subscribe(sub_path, callback=self._on_message, flow_control=flow)
        threading.Thread(target=self._renewal_loop, name="inbox-watch-renewal", daemon=True).start()
        threading.Thread(target=self._poll_loop, name="inbox-poll-reconciler", daemon=True).start()
        threading.Thread(target=self._draft_retry_loop, name="inbox-draft-retry", daemon=True).start()
        self._start_periodic_jobs()
        logger.info("inbox runtime: watching %d account(s), pulling %s", len(self._by_email), sub_path)

    def add_account(self, account: Account) -> bool:
        """Hot-add an account to the running runtime (no restart needed).

        Ensures its labels, arms its watch, seeds its cursor, and registers it for
        routing — the shared subscription already delivers its notifications.
        Idempotent on email; returns True if added, False if already managed.
        """
        from .labels_apply import ensure_labels

        with self._lock:
            if account.email in self._by_email:
                return False
            service = account.build_service()
            account.label_ids = ensure_labels(service) if get_config().labels_enabled else {}
            hid, account.watch_expiration = arm_watch(service, self._topic)
            with contextlib.closing(self._db()) as conn:
                if db.get_cursor(conn, account.email) is None:
                    db.set_cursor(conn, account.email, hid)
            self._accounts.append(account)
            self._by_email[account.email] = account
        logger.info("inbox runtime: hot-added account %s", account.email)
        return True

    def remove_account(self, email: str) -> bool:
        """Stop managing an account: drop from routing + best-effort stop its watch.

        Idempotent; returns True if it was managed. Used by disconnect and by the
        auth-failure path (a dead token can no longer receive notifications).
        """
        with self._lock:
            account = self._by_email.pop(email, None)
            if account is None:
                return False
            self._accounts = [a for a in self._accounts if a.email != email]
        try:  # best-effort, outside the lock (network); fine if the token is dead
            account.build_service().users().stop(userId="me").execute()
        except Exception:
            logger.info("inbox runtime: could not stop watch for %s (token may be revoked)", email)
        logger.info("inbox runtime: removed account %s", email)
        return True

    def _note_auth_failure(self, email: str) -> None:
        """Flag a dead-credential account for reconnect (once) and drop it from routing."""
        first = email not in self._auth_failed
        self._auth_failed.add(email)
        self.remove_account(email)
        if first and self._on_auth_failure is not None:
            try:
                self._on_auth_failure(email)
            except Exception:
                logger.exception("inbox runtime: on_auth_failure callback failed for %s", email)

    def _renewal_loop(self) -> None:
        """Re-arm each account's watch() before its ~7-day expiry so sync never stops."""
        while True:
            time.sleep(RENEWAL_CHECK_INTERVAL_S)
            for account in list(self._by_email.values()):
                try:
                    if should_renew(int(time.time() * 1000), account.watch_expiration):
                        _, account.watch_expiration = arm_watch(account.build_service(), self._topic)
                        logger.info(
                            "inbox runtime: watch renewed for %s (expires %s)",
                            account.email,
                            account.watch_expiration,
                        )
                except Exception:
                    logger.exception("inbox runtime: watch renewal failed for %s", account.email)

    def _poll_loop(self) -> None:
        """Reconcile each account on a timer in case Pub/Sub dropped a notification."""
        while True:
            time.sleep(POLL_INTERVAL_S)
            self._poll_once()

    def _poll_once(self) -> None:
        """One reconciliation pass: drain each account from its stored cursor.

        Skips an account with no cursor yet (``start`` seeds it from watch()).
        Holds ``self._lock`` per account so a poll and a live notification can't
        double-drain the same mailbox.
        """
        for account in list(self._by_email.values()):
            with self._lock:
                if account.email not in self._by_email:
                    continue  # removed (disconnect / auth failure) mid-pass
                with contextlib.closing(self._db()) as conn:
                    cursor = db.get_cursor(conn, account.email)
                if cursor is None:
                    continue  # not armed yet; nothing to reconcile from
                try:
                    self._drain_account(account, fallback_history_id=cursor)
                except Exception:
                    logger.exception("inbox runtime: poll drain failed for %s", account.email)

    def _draft_retry_loop(self) -> None:
        """Re-dispatch unfulfilled drafts whose agent turns failed or were dropped.

        Required because the history cursor advances after each drain, so a failed
        To-Respond draft is never re-emitted by a redrain — only this loop retries
        it (until fulfilled or MAX_DRAFT_ATTEMPTS).
        """
        while True:
            time.sleep(DRAFT_RETRY_INTERVAL_S)
            try:
                self._retry_unfulfilled_drafts()
            except Exception:
                logger.exception("inbox runtime: draft retry pass failed")

    def _retry_unfulfilled_drafts(self) -> None:
        """One retry pass: re-claim + re-dispatch each eligible unfulfilled draft.

        Re-dispatch uses the keyword-only ``wake_fn`` contract with the stored
        ``from_addr`` mapped to the ``sender`` kwarg (``wake_draft`` takes no
        positional args). The atomic ``claim_draft`` re-check means a draft the
        notify/poll path just handled is skipped here.
        """
        if self._wake_fn is None:
            return
        with contextlib.closing(self._db()) as conn:
            rows = db.unfulfilled_drafts(
                conn, ttl_ms=RETRY_TTL_MS, max_attempts=MAX_DRAFT_ATTEMPTS, now_ms=db.now_ms()
            )
        for row in rows:
            acct, tid = row["account"], row["thread_id"]
            sender, subject = row["from_addr"] or "", row["subject"] or ""
            instruction = None
            with self._lock:
                with contextlib.closing(self._db()) as conn:
                    won = db.claim_draft(
                        conn, acct, tid, from_addr=sender, subject=subject,
                        ttl_ms=RETRY_TTL_MS, max_attempts=MAX_DRAFT_ATTEMPTS, now_ms=db.now_ms(),
                    )
                    if not won:
                        continue  # another dispatch path just handled it
                    instruction = self._build_brief(conn, acct, tid, sender, subject)
            self._wake_fn(
                account_id=acct, thread_id=tid, sender=sender, subject=subject, instruction=instruction
            )
            logger.info("inbox runtime: retried draft dispatch for thread %s", tid)

    def _start_periodic_jobs(self) -> None:
        """Start each module-contributed timer job on its own daemon thread.

        Empty until modules contribute ``periodic()`` jobs (e.g. the shipping
        poller). A job's ``run_once`` is responsible for skipping accounts no
        longer managed (the same pattern ``_poll_once`` uses)."""
        if self._registry is None:
            return
        for job in self._registry.periodic():
            threading.Thread(
                target=self._run_periodic_job, args=(job,), name=f"inbox-job-{job.name}", daemon=True
            ).start()
            logger.info("inbox runtime: started periodic job %s (every %ss)", job.name, job.interval_s)

    def _run_periodic_job(self, job) -> None:
        while True:
            time.sleep(job.interval_s)
            try:
                job.run_once()
            except Exception:
                logger.exception("inbox runtime: periodic job %s failed", job.name)

    def _on_message(self, message) -> None:
        try:
            data = message.data
            if isinstance(data, bytes):
                data = data.decode()
            with self._lock:  # serialize: no concurrent drains for one account
                self.handle_notification(decode_gmail_notification(data))
            message.ack()
        except Exception:
            logger.exception("inbox runtime: notification failed; nacking")
            message.nack()

    def handle_notification(self, notif) -> None:
        """Route to the notified account, drain its history, triage, advance its cursor."""
        account = self._by_email.get(notif.email_address)
        if account is None:
            logger.warning(
                "inbox runtime: notification for unknown account %s; ignoring",
                notif.email_address,
            )
            return
        self._drain_account(account, fallback_history_id=str(notif.history_id))

    def _drain_account(self, account: Account, *, fallback_history_id: str) -> None:
        """Drain one account from its stored cursor → triage each new message.

        Shared by Pub/Sub notifications and the polling reconciler.
        ``fallback_history_id`` seeds the cursor when none is stored and is the
        reset point if the stored cursor is too old (StaleCursor).
        """
        service = account.build_service()
        extra: dict[str, Any] = {}
        if self._classify_fn is not None:
            extra["classify_fn"] = self._classify_fn

        # One connection for the whole drain (single-threaded under self._lock):
        # cursor read, per-message classification persistence, cursor advance.
        with contextlib.closing(self._db()) as conn:
            cursor = db.get_cursor(conn, account.email) or fallback_history_id

            def _process(message_id: str) -> None:
                category = process_message(
                    message_id=message_id,
                    account_id=account.email,
                    service=service,
                    label_ids=account.label_ids,
                    wake_fn=self._dedup_wake,
                    conn=conn,
                    registry=self._registry,
                    **extra,
                )
                logger.info("inbox runtime: [%s] %s -> %s", account.email, message_id, category)

            def _sent(message_id: str) -> None:
                from .sent_handler import handle_sent

                target = handle_sent(
                    message_id=message_id,
                    account_id=account.email,
                    service=service,
                    label_ids=account.label_ids,
                    registry=self._registry,
                )
                logger.info("inbox runtime: [%s] SENT %s -> %s", account.email, message_id, target)

            try:
                new_cursor = drain_history(
                    service=service,
                    start_history_id=cursor,
                    process_fn=_process,
                    sent_fn=_sent,
                )
            except StaleCursor:
                new_cursor = fallback_history_id  # cursor too old; reset forward
            except Exception as exc:
                if _is_auth_error(exc):
                    logger.warning(
                        "inbox runtime: auth failure for %s — flagging for reconnect", account.email
                    )
                    self._note_auth_failure(account.email)
                    return  # ack: a dead token must not trigger a redelivery storm
                raise  # transient error → let _on_message nack so it retries
            db.set_cursor(conn, account.email, new_cursor)
