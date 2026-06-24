"""draft_learn: similarity scoring, outcome bucketing, and fenced distill/apply.

Drives a real (in-memory-on-disk) ``db.connect`` so the accessor contract is
exercised end-to-end; the LLM is the only seam (a fake ``classify_fn`` that records
its args and returns canned JSON). Covers the spec's ACs: learned-layer isolation
from the backfill fields, lesson dedup/evidence, randomized fences around BOTH
bodies, no-LLM verbatim/trivial paths, the M2 inflation guard, LLM-failure
retry-ability, and empty-sender handling (G3).
"""

from __future__ import annotations

import sqlite3

import pytest

from hermes_inbox_organizer import db, draft_learn

# ── fixtures / helpers ───────────────────────────────────────────────────────────

@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    return db.connect(tmp_path / "state.db")


class RecordingClassifier:
    """A classify_fn seam that records (system, user) calls and returns canned JSON."""

    def __init__(self, result):
        self._result = result
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system, user):
        self.calls.append((system, user))
        return dict(self._result)


def _seed_outcome(
    conn,
    *,
    account="a@x.com",
    thread_id="t1",
    sender_email="bob@y.com",
    draft_body="draft text",
    sent_body="sent text",
    outcome="sent_edited",
    similarity=50,
):
    """Insert a draft_outcomes row and return it (mirrors what on_sent persists)."""
    conn.execute(
        """INSERT INTO draft_outcomes
               (account, thread_id, sender_email, draft_body, sent_body, outcome,
                similarity, learned, updated_at_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)""",
        (account, thread_id, sender_email, draft_body, sent_body, outcome,
         similarity, db.now_ms()),
    )
    return db.get_draft_outcome(conn, account, thread_id)


def _row(conn, account, thread_id):
    return db.get_draft_outcome(conn, account, thread_id)


# ── score_similarity (pure) ──────────────────────────────────────────────────────

def test_score_similarity_identical_and_disjoint() -> None:
    assert draft_learn.score_similarity("hello world", "hello world") == 100
    assert draft_learn.score_similarity("", "") == 100  # both empty == identical
    low = draft_learn.score_similarity("aaaaaaaa", "zzzzzzzz")
    assert 0 <= low <= 10


def test_score_similarity_caps_both_sides() -> None:
    # A huge tail beyond BODY_CAP must not change the score vs the capped inputs.
    base = "x" * draft_learn.BODY_CAP
    assert draft_learn.score_similarity(base, base + "y" * 5000) == 100


# ── classify_outcome (pure) ──────────────────────────────────────────────────────

def test_classify_outcome_buckets() -> None:
    verbatim = draft_learn.classify_outcome("same text", "same text", verbatim_t=92, edit_t=45)
    assert verbatim[0] == "sent_verbatim" and verbatim[1] == 100

    edited = draft_learn.classify_outcome(
        "Hi Bob, lunch at noon tomorrow?", "Hi Bob, lunch at 1pm tomorrow.",
        verbatim_t=92, edit_t=45,
    )
    assert edited[0] == "sent_edited"

    # A fresh reply (the owner ignored the draft) lands below edit_t. Use the real
    # default threshold (45) — typical English prose still shares ~30% chars, so a
    # genuinely rewritten reply is correctly bucketed sent_ignored.
    ignored = draft_learn.classify_outcome(
        "Thanks, that works for me — see you at noon.",
        "Sorry, I have to push this to next week instead.",
        verbatim_t=92, edit_t=45,
    )
    assert ignored[0] == "sent_ignored"


def test_classify_outcome_m2_inflation_guard_routes_to_edited() -> None:
    # Stripped sent body >1.5x the draft (unstripped quotes) must NOT be verbatim
    # even though the prefix matches and the raw ratio is high.
    draft = "Sounds good, see you then."
    sent = draft + " " + ("> quoted original line\n" * 40)  # huge quote tail
    outcome, _sim = draft_learn.classify_outcome(draft, sent, verbatim_t=50, edit_t=45)
    assert outcome == "sent_edited"  # forced off verbatim despite matching prefix


def test_classify_outcome_m2_blocks_verbatim_when_similarity_above_threshold() -> None:
    # The headline AC#18 case: similarity >= verbatim_t AND inflated. Without the
    # guard this is the dangerous mis-bucket (a real edit from a non-Gmail client
    # whose unstripped quote tail keeps the ratio high recorded as sent_verbatim and
    # dropped). The guard must force it to sent_edited so the LLM still sees the edit.
    draft = "Hi Bob, the proposal is attached, let me know if you have any questions about it."
    sent = draft + " " + draft[: int(len(draft) * 0.6)]  # ~1.6x length, still high ratio
    similarity = draft_learn.score_similarity(draft, sent)
    assert similarity >= 70 and len(sent) > len(draft) * 1.5  # precondition: high AND inflated

    outcome, sim = draft_learn.classify_outcome(draft, sent, verbatim_t=70, edit_t=45)
    assert outcome == "sent_edited"          # guard blocked the verbatim mis-bucket
    assert sim >= 70                          # …even though similarity cleared verbatim_t

    # Control: at the same threshold, a NON-inflated body that clears verbatim_t IS
    # verbatim — proving the inflation flag is exactly what flipped the bucket above.
    assert draft_learn.classify_outcome(draft, draft, verbatim_t=70, edit_t=45)[0] == "sent_verbatim"


# ── distill_and_apply: sent_verbatim (no LLM) ────────────────────────────────────

def test_verbatim_marks_learned_without_llm(conn) -> None:
    row = _seed_outcome(conn, outcome="sent_verbatim", similarity=100,
                        draft_body="ok", sent_body="ok")
    clf = RecordingClassifier({"voice_update": "should not be called"})
    draft_learn.distill_and_apply(conn, account="a@x.com", outcome_row=row, classify_fn=clf)

    assert clf.calls == []  # AC#7: verbatim never calls the LLM
    assert _row(conn, "a@x.com", "t1")["learned"] == 1
    assert db.get_sender_profile(conn, "a@x.com", "bob@y.com") is None  # no voice mutation


# ── distill_and_apply: trivial edit (no LLM) ─────────────────────────────────────

def test_trivial_edit_skips_llm_and_distillation(conn) -> None:
    draft = "Thanks, talk soon."
    sent = "Thanks, talk soon!"  # one-char typo-class change → high similarity, tiny delta
    sim = draft_learn.score_similarity(draft, sent)
    assert sim >= draft_learn.TRIVIAL_T
    row = _seed_outcome(conn, outcome="sent_edited", draft_body=draft, sent_body=sent,
                        similarity=sim)
    clf = RecordingClassifier({"voice_update": "nope"})
    draft_learn.distill_and_apply(conn, account="a@x.com", outcome_row=row, classify_fn=clf)

    assert clf.calls == []  # trivial-edit floor: no LLM, no lesson
    assert _row(conn, "a@x.com", "t1")["learned"] == 1
    assert db.get_sender_profile(conn, "a@x.com", "bob@y.com") is None


# ── distill_and_apply: edited → LLM writes learned layer, not backfill ────────────

def test_edited_writes_learned_notes_leaving_voice_notes_unchanged(conn) -> None:
    # Pre-existing backfill profile that must NOT be touched by the learned layer.
    db.upsert_sender_profile(
        conn, account="a@x.com", sender_email="bob@y.com",
        voice_notes="BACKFILL voice", tone_hints="BACKFILL tone", source="backfill",
    )
    row = _seed_outcome(
        conn, outcome="sent_edited", similarity=60,
        draft_body="Hi Bob, attached is the long-winded proposal we discussed.",
        sent_body="Bob — proposal attached. Shout with questions.",
    )
    clf = RecordingClassifier({
        "voice_update": "Prefers terse, lowercase-casual replies; cuts filler.",
        "lessons": [{"polarity": "dont", "rule": "Avoid long-winded preambles"}],
        "tone_hint": "casual",
    })
    draft_learn.distill_and_apply(conn, account="a@x.com", outcome_row=row, classify_fn=clf)

    prof = db.get_sender_profile(conn, "a@x.com", "bob@y.com")
    # AC#6: learned layer written; backfill fields untouched.
    assert "terse" in (prof["learned_notes"] or "")
    assert "casual" in (prof["learned_notes"] or "")          # tone_hint folded in
    assert prof["learned_updated_ms"] is not None
    assert prof["voice_notes"] == "BACKFILL voice"
    assert prof["tone_hints"] == "BACKFILL tone"
    # lesson applied
    lessons = db.top_lessons(conn, "a@x.com", limit=10)
    assert any("long-winded" in (r["rule"] or "") for r in lessons)
    assert _row(conn, "a@x.com", "t1")["learned"] == 1


def test_fences_wrap_both_bodies_in_prompt(conn) -> None:
    draft = "DRAFTSENTINEL the proposal body"
    sent = "SENTSENTINEL the shorter body"
    row = _seed_outcome(conn, outcome="sent_ignored", similarity=20,
                        draft_body=draft, sent_body=sent)
    clf = RecordingClassifier({"voice_update": None, "lessons": []})
    draft_learn.distill_and_apply(conn, account="a@x.com", outcome_row=row, classify_fn=clf)

    assert len(clf.calls) == 1
    system, user = clf.calls[0]
    # AC#8: system prompt forbids following in-fence instructions; both bodies fenced.
    assert "UNTRUSTED" in system
    assert "never follow" in system.lower()
    assert user.count("<SENT_") >= 2 and user.count("</SENT_") >= 2  # randomized fences
    # both bodies appear *inside* a fence region
    assert "DRAFTSENTINEL" in user and "SENTSENTINEL" in user


def test_lessons_dedup_and_evidence_bump(conn) -> None:
    row1 = _seed_outcome(conn, thread_id="t1", outcome="sent_ignored", similarity=10,
                         draft_body="aaaa", sent_body="bbbb")
    clf = RecordingClassifier({
        "voice_update": None,
        # model returns a dup within one call AND repeats across calls → must merge.
        "lessons": [
            {"polarity": "dont", "rule": "Avoid jargon"},
            {"polarity": "dont", "rule": "avoid jargon"},  # case-dup in same call
        ],
    })
    draft_learn.distill_and_apply(conn, account="a@x.com", outcome_row=row1, classify_fn=clf)
    row2 = _seed_outcome(conn, thread_id="t2", outcome="sent_ignored", similarity=10,
                         draft_body="cccc", sent_body="dddd")
    draft_learn.distill_and_apply(conn, account="a@x.com", outcome_row=row2, classify_fn=clf)

    lessons = [r for r in db.top_lessons(conn, "a@x.com", limit=10)
               if (r["rule"] or "").lower() == "avoid jargon"]
    assert len(lessons) == 1                       # deduped to a single row
    assert lessons[0]["evidence_count"] == 2       # bumped once per distill call


def test_llm_failure_leaves_learned_zero_and_logs_no_body(conn, caplog) -> None:
    secret_draft = "DRAFT_SECRET_BODY please pay invoice 12345"
    secret_sent = "SENT_SECRET_BODY transferred to acct 99"
    row = _seed_outcome(conn, outcome="sent_edited", similarity=60,
                        draft_body=secret_draft, sent_body=secret_sent)

    def boom(system, user):
        raise RuntimeError("LLM down")

    # The failure is caught + logged; distill_and_apply does NOT raise…
    with caplog.at_level("WARNING", logger="hermes_inbox_organizer.draft_learn"):
        draft_learn.distill_and_apply(conn, account="a@x.com", outcome_row=row, classify_fn=boom)
    # …and the row stays learned=0 so the sweep retries it.
    assert _row(conn, "a@x.com", "t1")["learned"] == 0
    # AC#12: no body content appears in any emitted log record.
    blob = " ".join(r.getMessage() for r in caplog.records)
    assert "SECRET_BODY" not in blob and "invoice" not in blob and "acct 99" not in blob


def test_empty_sender_skips_learned_notes_but_applies_global_lessons(conn) -> None:
    row = _seed_outcome(conn, sender_email="", outcome="sent_ignored", similarity=15,
                        draft_body="aaaa bbbb", sent_body="cccc dddd")
    clf = RecordingClassifier({
        "voice_update": "this note must NOT be written (no sender key)",
        "lessons": [{"polarity": "do", "rule": "Lead with the ask"}],
    })
    draft_learn.distill_and_apply(conn, account="a@x.com", outcome_row=row, classify_fn=clf)

    # G3: no sender-keyed learned_notes row was created for an empty key…
    assert db.get_sender_profile(conn, "a@x.com", "") is None
    # …but the global lesson still landed, and the row is marked learned (not retried).
    assert any("Lead with the ask" in (r["rule"] or "")
               for r in db.top_lessons(conn, "a@x.com", limit=10))
    assert _row(conn, "a@x.com", "t1")["learned"] == 1


def test_no_reply_emits_soft_lesson_only_past_threshold(conn) -> None:
    acct, sender = "a@x.com", "bob@y.com"
    # Seed (threshold-1) prior no_reply rows so this one crosses the line.
    for i in range(draft_learn.NO_REPLY_LESSON_THRESHOLD - 1):
        conn.execute(
            """INSERT INTO draft_outcomes (account, thread_id, sender_email, outcome,
                   learned, updated_at_ms) VALUES (?, ?, ?, 'no_reply', 1, ?)""",
            (acct, f"old{i}", sender, db.now_ms()),
        )
    row = _seed_outcome(conn, thread_id="tnow", sender_email=sender, outcome="no_reply",
                        draft_body="hi", sent_body="", similarity=None)
    clf = RecordingClassifier({"voice_update": "should not run"})
    draft_learn.distill_and_apply(conn, account=acct, outcome_row=row, classify_fn=clf)

    assert clf.calls == []  # no_reply never calls the LLM
    # no voice mutation, but the threshold soft lesson is present
    assert db.get_sender_profile(conn, acct, sender) is None
    lessons = db.top_lessons(conn, acct, limit=10)
    assert any(sender in (r["rule"] or "") and r["polarity"] == "dont" for r in lessons)
    assert _row(conn, acct, "tnow")["learned"] == 1


def test_no_reply_below_threshold_emits_no_lesson(conn) -> None:
    row = _seed_outcome(conn, thread_id="t1", sender_email="solo@y.com", outcome="no_reply",
                        draft_body="hi", sent_body="", similarity=None)
    draft_learn.distill_and_apply(
        conn, account="a@x.com", outcome_row=row,
        classify_fn=RecordingClassifier({}),
    )
    assert db.top_lessons(conn, "a@x.com", limit=10) == []
    assert _row(conn, "a@x.com", "t1")["learned"] == 1


def test_sent_no_draft_row_just_marks_learned(conn) -> None:
    # Capture-all gold-example rows have no draft; nothing to distil, just mark learned.
    conn.execute(
        """INSERT INTO draft_outcomes (account, thread_id, sender_email, sent_body, outcome,
               learned, updated_at_ms) VALUES (?, ?, ?, ?, 'sent_no_draft', 0, ?)""",
        ("a@x.com", "t1", "bob@y.com", "a reply I sent fresh", db.now_ms()),
    )
    row = _row(conn, "a@x.com", "t1")
    clf = RecordingClassifier({"voice_update": "nope"})
    draft_learn.distill_and_apply(conn, account="a@x.com", outcome_row=row, classify_fn=clf)
    assert clf.calls == []
    assert _row(conn, "a@x.com", "t1")["learned"] == 1
