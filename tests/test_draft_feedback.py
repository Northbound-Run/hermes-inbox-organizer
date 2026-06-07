"""DraftFeedbackModule: on_sent pairing/capture, the liveness-gated sweep, and tools.

All seamed — a temp DB (``db_connect``), injected ``resolve_accounts`` /
``needs_reconnect`` (the M1 liveness gate), a fake ``classify_fn``, and a tiny fake
config. No Gmail, no LLM, no runtime. Covers: outcome bucketing, capture-all
recipient-keying + multi-recipient skip (C1), the M2 quote-inflation route, the
enabled gate, the no_reply sweep gated on liveness (M1) with the G1 no-clobber
UPDATE, retention/lesson prune, and the three owner tools.
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace

from hermes_inbox_organizer import db, draft_learn
from hermes_inbox_organizer.modules import SentEvent
from hermes_inbox_organizer.modules.draft_feedback import DraftFeedbackModule


# ── fakes / factories ────────────────────────────────────────────────────────────

def _cfg(**over):
    """A minimal Config stand-in carrying only the fields the module reads."""
    base = dict(
        draft_feedback_enabled=True,
        draft_feedback_capture_all_sent=True,
        draft_feedback_no_reply_hours=72,
        draft_feedback_sweep_interval_s=6 * 3600,
        draft_feedback_max_examples=3,
        draft_feedback_max_lessons=8,
        draft_feedback_retention_days=90,
        draft_feedback_verbatim_threshold=92,
        draft_feedback_edit_threshold=45,
    )
    base.update(over)
    return SimpleNamespace(**base)


class FakeClassifier:
    def __init__(self, result=None):
        self._result = result or {"voice_update": None, "lessons": []}
        self.calls = []

    def __call__(self, system, user):
        self.calls.append((system, user))
        return dict(self._result)


def _mod(tmp_path, *, managed=("a@x.com",), reconnect=(), classify=None, **cfg_over):
    return DraftFeedbackModule(
        resolve_accounts=lambda: set(managed),
        needs_reconnect=lambda: set(reconnect),
        config=_cfg(**cfg_over),
        classify_fn=classify or FakeClassifier(),
        db_connect=lambda: db.connect(tmp_path / "state.db"),
    )


def _sent_event(*, account="a@x.com", thread_id="t1", message_id="m1", body="", to="", frm="me@x.com"):
    return SentEvent(
        account_id=account,
        message_id=message_id,
        thread_id=thread_id,
        parsed={"from": frm, "to": to, "body": body},
        target_category="Awaiting Reply",
    )


def _conn(tmp_path):
    return contextlib.closing(db.connect(tmp_path / "state.db"))


def _seed_draft(tmp_path, *, account="a@x.com", thread_id="t1", sender="bob@y.com", draft_body="hi"):
    """Capture a draft side (what inbox_create_draft would persist)."""
    with _conn(tmp_path) as conn:
        db.upsert_draft_outcome_draft(
            conn, account=account, thread_id=thread_id, sender_email=sender,
            gmail_draft_id="d1", draft_body=draft_body,
        )


def _outcome(tmp_path, account="a@x.com", thread_id="t1"):
    with _conn(tmp_path) as conn:
        return db.get_draft_outcome(conn, account, thread_id)


# ── enabled gate ──────────────────────────────────────────────────────────────────

def test_enabled_gate(tmp_path) -> None:
    assert _mod(tmp_path).enabled is True
    assert _mod(tmp_path, draft_feedback_enabled=False).enabled is False


# ── on_sent: pairing a drafted thread (bucketing) ─────────────────────────────────

def test_on_sent_verbatim_buckets_and_no_llm(tmp_path) -> None:
    _seed_draft(tmp_path, draft_body="Sounds good, see you at noon.")
    clf = FakeClassifier()
    m = _mod(tmp_path, classify=clf)
    m.on_sent(_sent_event(body="Sounds good, see you at noon.", to="bob@y.com"))

    row = _outcome(tmp_path)
    assert row["outcome"] == "sent_verbatim"
    assert row["sent_body"] == "Sounds good, see you at noon."
    assert row["similarity"] == 100
    assert row["learned"] == 1            # verbatim distil marks learned
    assert clf.calls == []                # verbatim never calls the LLM
    assert row["sender_email"] == "bob@y.com"  # stays the inbound correspondent


def test_on_sent_edited_buckets_and_distills(tmp_path) -> None:
    # A real edit: same structure, owner tweaked the greeting/wording (similarity in
    # the [edit_t, verbatim_t) band → sent_edited, routes to the LLM).
    _seed_draft(tmp_path, draft_body="Hi Bob, the proposal is attached — let me know if you have any questions about it.")
    clf = FakeClassifier({
        "voice_update": "Prefers a casual greeting and a brief sign-off.",
        "lessons": [{"polarity": "dont", "rule": "Avoid long-winded preambles"}],
    })
    m = _mod(tmp_path, classify=clf)
    m.on_sent(_sent_event(
        body="Hey Bob, proposal attached. Let me know if you have questions about it, thanks.",
        to="bob@y.com",
    ))

    row = _outcome(tmp_path)
    assert row["outcome"] == "sent_edited"
    assert len(clf.calls) == 1            # edited routes to the LLM
    with _conn(tmp_path) as conn:
        prof = db.get_sender_profile(conn, "a@x.com", "bob@y.com")
        assert "casual" in (prof["learned_notes"] or "")
        assert any("long-winded" in (r["rule"] or "") for r in db.top_lessons(conn, "a@x.com", limit=10))


def test_on_sent_ignored_bucket(tmp_path) -> None:
    _seed_draft(tmp_path, draft_body="Thanks, that works for me — see you at noon on Friday.")
    m = _mod(tmp_path, classify=FakeClassifier())
    m.on_sent(_sent_event(body="Sorry, I have to push this to next week instead.", to="bob@y.com"))
    assert _outcome(tmp_path)["outcome"] == "sent_ignored"


def test_on_sent_does_not_repair_already_paired_thread(tmp_path) -> None:
    # A drafted row already paired by a first send. A SECOND send on the same thread
    # must NOT re-pair / re-distill against the original draft (stale baseline) and
    # must not clobber the stored outcome/sent_body.
    with _conn(tmp_path) as conn:
        conn.execute(
            """INSERT INTO draft_outcomes (account, thread_id, sender_email, draft_body,
                   draft_created_ms, sent_body, sent_message_id, outcome, similarity,
                   learned, updated_at_ms)
                   VALUES ('a@x.com','t1','bob@y.com','original draft', ?, 'first real reply',
                           'm-first','sent_edited',60,1,?)""",
            (db.now_ms(), db.now_ms()),
        )
    clf = FakeClassifier({"voice_update": "should not run again", "lessons": []})
    m = _mod(tmp_path, classify=clf)
    m.on_sent(_sent_event(message_id="m-second", body="A different later reply.", to="bob@y.com"))

    assert clf.calls == []  # no second distill against the original draft
    row = _outcome(tmp_path)
    assert row["outcome"] == "sent_edited"          # untouched
    assert row["sent_body"] == "first real reply"   # not clobbered
    assert row["sent_message_id"] == "m-first"      # still the first send


def test_on_sent_m2_inflation_routes_to_llm_not_verbatim(tmp_path) -> None:
    # A short draft; the owner's "sent" body has a huge quote tail _new_text won't
    # strip (simulating a non-Gmail client). Must NOT bucket verbatim.
    draft = "Sounds good, see you then."
    _seed_draft(tmp_path, draft_body=draft)
    # No '>' lines so _new_text keeps it all → inflates the ratio's denominator.
    inflated = draft + " " + ("also adding a lot of extra unrelated context here " * 20)
    clf = FakeClassifier()
    m = _mod(tmp_path, classify=clf, draft_feedback_verbatim_threshold=50)
    m.on_sent(_sent_event(body=inflated, to="bob@y.com"))

    assert _outcome(tmp_path)["outcome"] == "sent_edited"  # forced off verbatim (M2)
    assert len(clf.calls) == 1


# ── on_sent: capture-all (non-drafted threads) ────────────────────────────────────

def test_capture_all_keys_on_recipient_not_owner(tmp_path) -> None:
    m = _mod(tmp_path)  # capture_all default on; no draft row exists for this thread
    m.on_sent(_sent_event(
        thread_id="tnew", body="Fresh reply I wrote myself.",
        to="carol@z.com", frm="owner@x.com",
    ))
    row = _outcome(tmp_path, thread_id="tnew")
    assert row is not None
    assert row["outcome"] == "sent_no_draft"
    assert row["sender_email"] == "carol@z.com"   # C1: RECIPIENT, never the owner
    assert row["sender_email"] != "owner@x.com"
    assert row["draft_body"] is None
    assert row["sent_body"] == "Fresh reply I wrote myself."


def test_capture_all_skips_multi_recipient(tmp_path) -> None:
    m = _mod(tmp_path)
    m.on_sent(_sent_event(
        thread_id="tmulti", body="Reply to a group.",
        to="carol@z.com, dave@z.com", frm="owner@x.com",
    ))
    assert _outcome(tmp_path, thread_id="tmulti") is None  # C1: ambiguous → skip


def test_capture_all_off_is_noop(tmp_path) -> None:
    m = _mod(tmp_path, draft_feedback_capture_all_sent=False)
    m.on_sent(_sent_event(thread_id="tnew", body="Fresh reply.", to="carol@z.com"))
    assert _outcome(tmp_path, thread_id="tnew") is None


def test_on_sent_never_raises_on_bad_event(tmp_path) -> None:
    m = _mod(tmp_path)
    # parsed missing keys / None body — must be swallowed (observer contract).
    m.on_sent(SentEvent(account_id="a@x.com", message_id="m", thread_id="t",
                        parsed={}, target_category="Awaiting Reply"))
    assert _outcome(tmp_path, thread_id="t") is None  # nothing captured, no crash


# ── sweep: liveness-gated no_reply (M1) ───────────────────────────────────────────

def _seed_pending(tmp_path, *, account, thread_id, draft_created_ms, account_updated_ms):
    """A pending drafted row + an accounts row with a chosen updated_at_ms."""
    with _conn(tmp_path) as conn:
        conn.execute(
            """INSERT INTO draft_outcomes (account, thread_id, sender_email, draft_body,
                   draft_created_ms, outcome, learned, updated_at_ms)
                   VALUES (?, ?, 'bob@y.com', 'hi', ?, 'pending', 0, ?)""",
            (account, thread_id, draft_created_ms, draft_created_ms),
        )
        conn.execute(
            """INSERT INTO accounts (account, updated_at_ms) VALUES (?, ?)
               ON CONFLICT(account) DO UPDATE SET updated_at_ms = excluded.updated_at_ms""",
            (account, account_updated_ms),
        )


def test_sweep_marks_no_reply_when_live(tmp_path) -> None:
    old = db.now_ms() - 100 * 3600 * 1000  # 100h ago (> 72h window)
    # drained AFTER the draft → mailbox was watched through the window.
    _seed_pending(tmp_path, account="a@x.com", thread_id="t1",
                  draft_created_ms=old, account_updated_ms=old + 5000)
    _mod(tmp_path, managed=("a@x.com",)).sweep_once()
    assert _outcome(tmp_path, thread_id="t1")["outcome"] == "no_reply"


def test_sweep_skips_unmanaged_account(tmp_path) -> None:
    old = db.now_ms() - 100 * 3600 * 1000
    _seed_pending(tmp_path, account="gone@x.com", thread_id="t1",
                  draft_created_ms=old, account_updated_ms=old + 5000)
    _mod(tmp_path, managed=("a@x.com",)).sweep_once()  # gone@x.com not managed
    assert _outcome(tmp_path, account="gone@x.com", thread_id="t1")["outcome"] == "pending"


def test_sweep_skips_needs_reconnect_account(tmp_path) -> None:
    old = db.now_ms() - 100 * 3600 * 1000
    _seed_pending(tmp_path, account="a@x.com", thread_id="t1",
                  draft_created_ms=old, account_updated_ms=old + 5000)
    _mod(tmp_path, managed=("a@x.com",), reconnect=("a@x.com",)).sweep_once()
    assert _outcome(tmp_path, thread_id="t1")["outcome"] == "pending"  # M1: awaiting reconnect


def test_sweep_skips_stale_drain(tmp_path) -> None:
    old = db.now_ms() - 100 * 3600 * 1000
    # drained BEFORE the draft was created → window not actually watched.
    _seed_pending(tmp_path, account="a@x.com", thread_id="t1",
                  draft_created_ms=old, account_updated_ms=old - 5000)
    _mod(tmp_path, managed=("a@x.com",)).sweep_once()
    assert _outcome(tmp_path, thread_id="t1")["outcome"] == "pending"  # M1: stale updated_at_ms


def test_sweep_leaves_recent_pending(tmp_path) -> None:
    recent = db.now_ms() - 1 * 3600 * 1000  # 1h ago (< 72h window)
    _seed_pending(tmp_path, account="a@x.com", thread_id="t1",
                  draft_created_ms=recent, account_updated_ms=recent + 5000)
    _mod(tmp_path, managed=("a@x.com",)).sweep_once()
    assert _outcome(tmp_path, thread_id="t1")["outcome"] == "pending"  # too new to mark


def test_sweep_no_clobber_of_recorded_send(tmp_path) -> None:
    # A row old enough to mark, BUT a send already landed (outcome != pending). The
    # conditional UPDATE (WHERE outcome='pending', G1) must not touch it.
    old = db.now_ms() - 100 * 3600 * 1000
    with _conn(tmp_path) as conn:
        conn.execute(
            """INSERT INTO draft_outcomes (account, thread_id, sender_email, draft_body,
                   draft_created_ms, sent_body, outcome, similarity, learned, updated_at_ms)
                   VALUES ('a@x.com', 't1', 'bob@y.com', 'hi', ?, 'the real reply', 'sent_edited', 60, 0, ?)""",
            (old, old),
        )
        conn.execute(
            "INSERT INTO accounts (account, updated_at_ms) VALUES ('a@x.com', ?)",
            (old + 5000,),
        )
    # sweep would also retry distillation on this unlearned row → give it a classifier.
    _mod(tmp_path, managed=("a@x.com",), classify=FakeClassifier()).sweep_once()
    row = _outcome(tmp_path, thread_id="t1")
    assert row["outcome"] == "sent_edited"        # NOT overwritten to no_reply (G1)
    assert row["sent_body"] == "the real reply"   # gold example survived


# ── sweep: distill retry + prune ──────────────────────────────────────────────────

def test_sweep_retries_unlearned_distillation(tmp_path) -> None:
    # An outcome that was classified but left unlearned (a prior on_sent distill
    # failed). The sweep's retry should distil + mark it learned.
    with _conn(tmp_path) as conn:
        conn.execute(
            """INSERT INTO draft_outcomes (account, thread_id, sender_email, draft_body,
                   sent_body, outcome, similarity, learned, updated_at_ms)
                   VALUES ('a@x.com', 't1', 'bob@y.com', 'long draft text here', 'short', 'sent_ignored', 20, 0, ?)""",
            (db.now_ms(),),
        )
    clf = FakeClassifier({"voice_update": "terser", "lessons": []})
    _mod(tmp_path, managed=("a@x.com",), classify=clf).sweep_once()
    assert _outcome(tmp_path, thread_id="t1")["learned"] == 1
    assert len(clf.calls) == 1


def test_sweep_prunes_lessons_and_old_outcomes(tmp_path) -> None:
    from hermes_inbox_organizer.modules.draft_feedback import MAX_LESSONS_STORE

    very_old = db.now_ms() - 200 * 24 * 3600 * 1000  # 200 days ago
    with _conn(tmp_path) as conn:
        # MAX_LESSONS_STORE + 3 active lessons → 3 should be soft-evicted.
        for i in range(MAX_LESSONS_STORE + 3):
            db.upsert_lesson(conn, account="a@x.com", scope="global", polarity="do",
                             rule=f"rule number {i}")
        # An old learned outcome → pruned by retention (default 90 days).
        conn.execute(
            """INSERT INTO draft_outcomes (account, thread_id, sender_email, outcome,
                   learned, updated_at_ms) VALUES ('a@x.com', 'told', 'bob@y.com',
                   'sent_no_draft', 1, ?)""",
            (very_old,),
        )
    _mod(tmp_path, managed=("a@x.com",)).sweep_once()
    with _conn(tmp_path) as conn:
        active = conn.execute(
            "SELECT count(*) AS n FROM draft_lessons WHERE account='a@x.com' AND active=1"
        ).fetchone()["n"]
        assert active == MAX_LESSONS_STORE     # capped to the store size
        assert db.get_draft_outcome(conn, "a@x.com", "told") is None  # old outcome pruned


# ── owner tools ───────────────────────────────────────────────────────────────────

def _tool(m, name):
    return next(t.handler for t in m.tools() if t.name == name)


def test_status_tool_reports_histogram_lessons_and_learned_senders(tmp_path) -> None:
    with _conn(tmp_path) as conn:
        conn.execute(
            """INSERT INTO draft_outcomes (account, thread_id, sender_email, outcome,
                   learned, updated_at_ms) VALUES ('a@x.com','t1','bob@y.com','sent_verbatim',1,?)""",
            (db.now_ms(),),
        )
        db.upsert_lesson(conn, account="a@x.com", scope="global", polarity="dont", rule="Avoid jargon")
        db.upsert_learned_notes(conn, "a@x.com", "bob@y.com", "terse and casual")
    m = _mod(tmp_path)
    out = json.loads(_tool(m, "inbox_draft_feedback_status")({"account_id": "a@x.com"}))
    assert out["outcomes"] == {"sent_verbatim": 1}
    assert any(l["rule"] == "Avoid jargon" for l in out["active_lessons"])
    senders = {s["sender_email"]: s for s in out["learned_senders"]}
    assert "bob@y.com" in senders
    # body text is NOT echoed — only the char count (avoid surfacing fenced content).
    assert senders["bob@y.com"]["note_chars"] == len("terse and casual")
    assert "terse and casual" not in json.dumps(out)


def test_forget_lesson_tool_soft_disables(tmp_path) -> None:
    with _conn(tmp_path) as conn:
        db.upsert_lesson(conn, account="a@x.com", scope="global", polarity="do", rule="Be brief")
        lid = conn.execute("SELECT lesson_id FROM draft_lessons").fetchone()["lesson_id"]
    m = _mod(tmp_path)
    out = json.loads(_tool(m, "inbox_forget_lesson")({"lesson_id": lid}))
    assert out["ok"] is True and out["active"] is False
    with _conn(tmp_path) as conn:
        assert db.top_lessons(conn, "a@x.com", limit=10) == []  # no longer active


def test_forget_lesson_tool_requires_id(tmp_path) -> None:
    out = json.loads(_tool(_mod(tmp_path), "inbox_forget_lesson")({}))
    assert "error" in out


def test_clear_learned_notes_tool(tmp_path) -> None:
    with _conn(tmp_path) as conn:
        db.upsert_sender_profile(conn, account="a@x.com", sender_email="bob@y.com",
                                 voice_notes="BACKFILL", source="backfill")
        db.upsert_learned_notes(conn, "a@x.com", "bob@y.com", "learned thing")
    m = _mod(tmp_path)
    out = json.loads(_tool(m, "inbox_clear_learned_notes")(
        {"account_id": "a@x.com", "sender_email": "bob@y.com"}))
    assert out["ok"] is True
    with _conn(tmp_path) as conn:
        prof = db.get_sender_profile(conn, "a@x.com", "bob@y.com")
        assert prof["learned_notes"] is None        # learned layer cleared
        assert prof["voice_notes"] == "BACKFILL"     # backfill layer intact


def test_clear_learned_notes_tool_validates_args(tmp_path) -> None:
    out = json.loads(_tool(_mod(tmp_path), "inbox_clear_learned_notes")({"account_id": "a@x.com"}))
    assert "error" in out


def test_tools_exposes_the_three_owner_tools(tmp_path) -> None:
    names = {t.name for t in _mod(tmp_path).tools()}
    assert names == {
        "inbox_draft_feedback_status",
        "inbox_forget_lesson",
        "inbox_clear_learned_notes",
    }
