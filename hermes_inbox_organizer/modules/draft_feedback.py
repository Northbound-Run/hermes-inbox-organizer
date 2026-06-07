"""Draft-feedback module — pair draft→sent, distil the correction, sweep no-replies.

The wiring half of the reinforcement loop (the distillation logic lives in
:mod:`hermes_inbox_organizer.draft_learn`). Three hooks:

* ``on_sent`` (observer, offloaded): when the owner sends on a thread we drafted,
  pair our draft against what actually went out, score + bucket it
  (``draft_learn.classify_outcome``), persist the outcome, and distil it. When the
  owner sends on a thread we did NOT draft, optionally capture the reply as a gold
  example for that correspondent (``capture_all_sent``).
* ``periodic`` (timer job): a sweep that (1) marks long-pending drafted threads
  ``no_reply`` — but only when the mailbox was demonstrably live through the window
  (M1), so a token outage never false-marks; (2) retries any distillation that
  failed; (3) prunes lessons + old learned outcomes to bound growth.
* ``tools`` (on-demand): owner inspect/revert — a read-only status plus
  ``forget_lesson`` / ``clear_learned_notes`` (the auditable-apply revert affordances;
  the two mutations are added to the owner gate by the plugin wiring).

The account resolver + reconnect predicate are INJECTED (they depend on the
plugin's token store + runtime in ``__init__``) so the M1 liveness gate is testable
with fakes. ``on_sent``/the sweep/the tool handlers each open their own short-lived
``db.connect()`` (worker threads — never the drain's connection) and never raise
(observer + tool contract). Bodies are never logged; distillation re-fences them.
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
from typing import Any, Callable, Optional

from .. import db, draft_learn, llm
from ..backfill import _recipients
from ..config import get_config
from ..sent_handler import _new_text
from .base import Module, PeriodicJob, SentEvent, ToolSpec

logger = logging.getLogger(__name__)

# How many active global lessons to retain before the sweep soft-evicts the
# lowest-value ones (documented module constant per the plan — the brief's own
# injection cap is the separate, smaller ``draft_feedback_max_lessons`` knob).
MAX_LESSONS_STORE = 20


def _now_ms() -> int:
    return int(time.time() * 1000)


class DraftFeedbackModule(Module):
    """Learn from draft→sent deltas; sweep no-replies. Gated by ``draft_feedback_enabled``."""

    name = "draft_feedback"

    def __init__(
        self,
        *,
        resolve_accounts: Callable[[], set],
        needs_reconnect: Callable[[], set],
        config: Any = None,
        classify_fn: Callable[[str, str], dict] = llm.classify_json,
        db_connect: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._cfg = config or get_config()
        self._resolve_accounts = resolve_accounts      # () -> managed account emails (M1)
        self._needs_reconnect = needs_reconnect        # () -> reconnect set (M1)
        self._classify_fn = classify_fn
        self._db_connect = db_connect or db.connect

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.draft_feedback_enabled)

    # -- pair + distil (observer, offloaded) ------------------------------------
    def on_sent(self, event: SentEvent) -> None:
        """Pair a SENT message against our draft (or capture it as a gold example).

        Never raises — the registry offloads this and isolates failures, but we also
        guard here so a malformed event can't escape the worker.
        """
        try:
            account = event.account_id
            thread_id = event.thread_id
            sent_body = _new_text((event.parsed or {}).get("body", "")).strip()[: draft_learn.BODY_CAP]

            with contextlib.closing(self._db_connect()) as conn:
                row = db.get_draft_outcome(conn, account, thread_id)
                if row is not None and row["draft_body"]:
                    # Only pair a drafted row that's still UNPAIRED. A 2nd send on a
                    # thread we already paired must NOT re-distill against the original
                    # draft — that would perturb learned voice from a stale baseline.
                    if row["outcome"] == "pending":
                        self._pair_drafted(conn, account, thread_id, row, sent_body, event)
                    else:
                        logger.info(
                            "draft-feedback: thread already paired account=%s thread=%s outcome=%s; skipping",
                            account, thread_id, row["outcome"],
                        )
                else:
                    self._capture_non_drafted(conn, account, thread_id, sent_body, event)
        except Exception:
            logger.exception(
                "draft-feedback: on_sent failed for message %s",
                getattr(event, "message_id", "?"),
            )

    def _pair_drafted(
        self, conn: Any, account: str, thread_id: str, row: Any, sent_body: str, event: SentEvent
    ) -> None:
        """A thread we drafted: score draft↔sent, persist the outcome, then distil."""
        outcome, similarity = draft_learn.classify_outcome(
            row["draft_body"] or "",
            sent_body,
            verbatim_t=self._cfg.draft_feedback_verbatim_threshold,
            edit_t=self._cfg.draft_feedback_edit_threshold,
        )
        # sender_email stays the inbound correspondent captured at draft time.
        sender_email = (row["sender_email"] or "")
        db.record_draft_outcome_sent(
            conn,
            account=account,
            thread_id=thread_id,
            sender_email=sender_email,
            sent_message_id=event.message_id,
            sent_body=sent_body,
            similarity=similarity,
            outcome=outcome,
        )
        logger.info(
            "draft-feedback: paired account=%s thread=%s outcome=%s similarity=%s",
            account, thread_id, outcome, similarity,
        )
        # Re-read so the distiller sees the just-persisted outcome/similarity/sent_body.
        fresh = db.get_draft_outcome(conn, account, thread_id)
        if fresh is not None:
            draft_learn.distill_and_apply(
                conn, account=account, outcome_row=fresh, classify_fn=self._classify_fn
            )

    def _capture_non_drafted(
        self, conn: Any, account: str, thread_id: str, sent_body: str, event: SentEvent
    ) -> None:
        """A thread we did NOT draft: optionally capture the reply as a gold example.

        C1: the correspondent is the RECIPIENT (``parsed["to"]``), never the owner
        (``parsed["from"]``). Skip when the To has more than one recipient (ambiguous
        ownership → pool noise) or when there's no usable recipient/body.
        """
        if not self._cfg.draft_feedback_capture_all_sent:
            return
        if not sent_body:
            return
        recipients = _recipients((event.parsed or {}).get("to", ""))
        if len(recipients) != 1:  # 0 = no usable recipient; >1 = ambiguous → skip
            return
        db.record_draft_outcome_sent(
            conn,
            account=account,
            thread_id=thread_id,
            sender_email=recipients[0],
            sent_message_id=event.message_id,
            sent_body=sent_body,
            similarity=None,
            outcome="sent_no_draft",
        )
        # No LLM — a gold example only. The distill queue treats sent_no_draft as
        # nothing-to-distil (just marks it learned on the next sweep).
        logger.info(
            "draft-feedback: captured gold example account=%s thread=%s", account, thread_id
        )

    # -- sweep (periodic) -------------------------------------------------------
    def periodic(self) -> list:
        return [
            PeriodicJob(
                name="draft-feedback-sweep",
                interval_s=self._cfg.draft_feedback_sweep_interval_s,
                run_once=self.sweep_once,
            )
        ]

    def sweep_once(self) -> None:
        """One bounded pass: liveness-gated no_reply marking, distill retry, prune."""
        try:
            with contextlib.closing(self._db_connect()) as conn:
                self._mark_no_replies(conn)
                self._retry_distillation(conn)
                self._prune(conn)
        except Exception:
            logger.exception("draft-feedback: sweep failed")

    def _mark_no_replies(self, conn: Any) -> None:
        """Mark long-pending drafted rows ``no_reply`` — only when the mailbox was live.

        M1: a row is only credible evidence of a non-send if its account is currently
        managed, NOT awaiting reconnect, AND drained successfully *after* the draft was
        created (``accounts.updated_at_ms > draft_created_ms`` — bumped by ``set_cursor``
        on every successful drain). Otherwise (e.g. the recurring 7-day token outage)
        the mailbox wasn't watched through the window, so we leave the row ``pending``
        for the next sweep rather than degrade drafting with a false ``no_reply``.
        """
        window_ms = self._cfg.draft_feedback_no_reply_hours * 3600 * 1000
        before_ms = _now_ms() - window_ms
        rows = db.pending_outcomes_older_than(conn, before_ms=before_ms)
        if not rows:
            return
        managed = set(self._resolve_accounts() or set())
        reconnect = set(self._needs_reconnect() or set())
        marked = 0
        for r in rows:
            account = r["account"]
            if account not in managed or account in reconnect:
                continue  # unmanaged / awaiting reconnect → not credible (M1)
            drained_ms = db.get_account_updated_ms(conn, account)
            draft_ms = r["draft_created_ms"] or 0
            if drained_ms is None or drained_ms <= draft_ms:
                continue  # mailbox wasn't drained through the window → leave pending (M1)
            # Conditional flip (G1): db.mark_outcome_no_reply only updates a row still
            # 'pending', so a send on_sent recorded between the read above and now wins.
            if db.mark_outcome_no_reply(conn, account, r["thread_id"]):
                marked += 1
        if marked:
            logger.info("draft-feedback: sweep marked %d no_reply", marked)

    def _retry_distillation(self, conn: Any) -> None:
        """Re-process any outcome that's classified but not yet learned (failed on_sent
        distill, or a freshly-marked no_reply). Bounded; each row isolated."""
        rows = db.unlearned_outcomes(conn, limit=200)
        for r in rows:
            try:
                draft_learn.distill_and_apply(
                    conn, account=r["account"], outcome_row=r, classify_fn=self._classify_fn
                )
            except Exception:
                # distill_and_apply already catches LLM failures; this guards anything
                # else so one bad row can't abort the sweep. No body in the log.
                logger.exception(
                    "draft-feedback: distill retry failed account=%s thread=%s",
                    r["account"], r["thread_id"],
                )

    def _prune(self, conn: Any) -> None:
        """Bound storage: soft-evict low-value lessons, delete old learned outcomes."""
        for account in set(self._resolve_accounts() or set()):
            try:
                db.prune_lessons(conn, account, keep=MAX_LESSONS_STORE)
            except Exception:
                logger.exception("draft-feedback: prune_lessons failed account=%s", account)
        retention_days = self._cfg.draft_feedback_retention_days
        if retention_days and retention_days > 0:
            before_ms = _now_ms() - retention_days * 24 * 3600 * 1000
            deleted = db.delete_learned_outcomes_older_than(conn, before_ms=before_ms)
            if deleted:
                logger.info("draft-feedback: pruned %d learned outcomes", deleted)

    # -- owner inspect / revert tools -------------------------------------------
    def tools(self) -> list:
        return [
            ToolSpec(
                name=INBOX_DRAFT_FEEDBACK_STATUS_SCHEMA["name"],
                schema=INBOX_DRAFT_FEEDBACK_STATUS_SCHEMA,
                handler=self._status_handler,
                description=INBOX_DRAFT_FEEDBACK_STATUS_SCHEMA["description"],
                toolset="inbox",
            ),
            ToolSpec(
                name=INBOX_FORGET_LESSON_SCHEMA["name"],
                schema=INBOX_FORGET_LESSON_SCHEMA,
                handler=self._forget_lesson_handler,
                description=INBOX_FORGET_LESSON_SCHEMA["description"],
                toolset="inbox",
            ),
            ToolSpec(
                name=INBOX_CLEAR_LEARNED_NOTES_SCHEMA["name"],
                schema=INBOX_CLEAR_LEARNED_NOTES_SCHEMA,
                handler=self._clear_learned_notes_handler,
                description=INBOX_CLEAR_LEARNED_NOTES_SCHEMA["description"],
                toolset="inbox",
            ),
        ]

    def _status_handler(self, args: dict, **_kwargs: Any) -> str:
        """READ-ONLY: outcome histogram + active lessons + per-sender learned notes.

        Returns the *presence/length* of learned notes, never the note text — the
        owner can read the full note via the sender-profile tool; surfacing prose here
        risks echoing fenced/untrusted content into a chat surface.
        """
        a = args or {}
        try:
            account = a.get("account_id") or None
            with contextlib.closing(self._db_connect()) as conn:
                histogram = db.outcome_histogram(conn, account)
                lesson_rows = (
                    db.top_lessons(conn, account, limit=self._cfg.draft_feedback_max_lessons)
                    if account
                    else db.all_active_lessons(conn, limit=self._cfg.draft_feedback_max_lessons)
                )
                lessons = [
                    {
                        "lesson_id": r["lesson_id"],
                        "polarity": r["polarity"],
                        "rule": r["rule"],
                        "evidence_count": r["evidence_count"],
                    }
                    for r in lesson_rows
                ]
                learned_senders = db.learned_note_summaries(conn, account, limit=100)
            return json.dumps(
                {
                    "outcomes": histogram,
                    "active_lessons": lessons,
                    "learned_senders": learned_senders,
                }
            )
        except Exception as exc:  # contract: never raise out of a tool handler
            return json.dumps({"error": f"inbox_draft_feedback_status failed: {exc}"})

    def _forget_lesson_handler(self, args: dict, **_kwargs: Any) -> str:
        """MUTATION (owner-gated by the plugin wiring): soft-disable a lesson by id."""
        a = args or {}
        try:
            lesson_id = a.get("lesson_id")
            if lesson_id is None:
                return json.dumps({"error": "lesson_id is required"})
            with contextlib.closing(self._db_connect()) as conn:
                db.set_lesson_active(conn, int(lesson_id), 0)
            return json.dumps({"ok": True, "lesson_id": int(lesson_id), "active": False})
        except Exception as exc:
            return json.dumps({"error": f"inbox_forget_lesson failed: {exc}"})

    def _clear_learned_notes_handler(self, args: dict, **_kwargs: Any) -> str:
        """MUTATION (owner-gated by the plugin wiring): drop a sender's learned note."""
        a = args or {}
        try:
            account = a.get("account_id")
            sender = (a.get("sender_email") or "").strip().lower()
            if not account or not sender:
                return json.dumps({"error": "account_id and sender_email are required"})
            with contextlib.closing(self._db_connect()) as conn:
                db.clear_learned_notes(conn, account, sender)
            return json.dumps({"ok": True, "account_id": account, "sender_email": sender})
        except Exception as exc:
            return json.dumps({"error": f"inbox_clear_learned_notes failed: {exc}"})


INBOX_DRAFT_FEEDBACK_STATUS_SCHEMA: dict = {
    "name": "inbox_draft_feedback_status",
    "description": (
        "Inspect what the draft-reinforcement loop has learned from your edits: the "
        "outcome histogram (how often drafts were sent verbatim / edited / rewritten / "
        "not sent), the active global do/don't lessons (with their ids + evidence "
        "counts), and which correspondents have a learned voice note. READ-ONLY — it "
        "reports counts and lesson text only, never your email bodies. Pass account_id "
        "to scope to one mailbox."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "account_id": {"type": "string", "description": "Optional connected account id (email)."},
        },
    },
}

INBOX_FORGET_LESSON_SCHEMA: dict = {
    "name": "inbox_forget_lesson",
    "description": (
        "Revert a learned drafting lesson by id (soft-disable, so it stops influencing "
        "drafts). Use inbox_draft_feedback_status to find the lesson_id. This changes "
        "how future replies are drafted — confirm with the owner before calling."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "lesson_id": {"type": "integer", "description": "The lesson id to forget (from the status tool)."},
        },
        "required": ["lesson_id"],
    },
}

INBOX_CLEAR_LEARNED_NOTES_SCHEMA: dict = {
    "name": "inbox_clear_learned_notes",
    "description": (
        "Clear the learned-from-edits voice note for one correspondent (leaves the "
        "original backfill voice profile intact). Use this to undo a bad/poisoned "
        "learned note. This changes how future replies to that person are drafted — "
        "confirm with the owner before calling."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "account_id": {"type": "string", "description": "The connected account id (email)."},
            "sender_email": {"type": "string", "description": "The correspondent whose learned note to clear."},
        },
        "required": ["account_id", "sender_email"],
    },
}
