"""backfill_sender_profiles: ranking, sampling, caps, idempotency, failure isolation (AC16/17)."""

from __future__ import annotations

import base64

from hermes_inbox_organizer import backfill, db


class FakeReader:
    """Minimal GmailReader seam: a scripted 'in:sent' scan + per-message To/body."""

    def __init__(self, sent, messages):
        self._sent = sent              # list of {"id": ...} for the in:sent scan
        self._messages = messages      # id -> {"to": ..., "body": ...}
        self.queries = []

    def list_messages(self, query, max_results):
        self.queries.append((query, max_results))
        if query == "in:sent":
            return [{"id": m["id"]} for m in self._sent][:max_results]
        addr = query.split("to:")[-1].strip()  # "in:sent to:<addr>"
        return [
            {"id": mid} for mid, m in self._messages.items()
            if addr in m.get("to", "").lower()
        ][:max_results]

    def get_message(self, message_id, format="full"):
        m = self._messages[message_id]
        payload = {"headers": [{"name": "To", "value": m.get("to", "")}]}
        if format != "metadata":
            payload["mimeType"] = "text/plain"
            payload["body"] = {"data": base64.urlsafe_b64encode(m.get("body", "").encode()).decode()}
        return {"id": message_id, "threadId": "t", "payload": payload}


def _msgs(*specs):  # (id, to, body)
    return {mid: {"to": to, "body": body} for (mid, to, body) in specs}


def test_backfill_ranks_and_profiles_top_senders(tmp_path) -> None:
    conn = db.connect(tmp_path / "state.db")
    messages = _msgs(
        ("1", "bob@y.com", "Hi Bob,\nLunch tomorrow?\n--\nMe"),
        ("2", "bob@y.com", "Bob - yes 1pm works."),
        ("3", "carol@z.com", "Hello Carol, thanks."),
    )
    captured = []

    def fake_summarize(system, user):
        captured.append(user)
        return "writes briefly, signs off casually"

    out = backfill.backfill_sender_profiles(
        reader=FakeReader([{"id": "1"}, {"id": "2"}, {"id": "3"}], messages),
        summarize_fn=fake_summarize, conn=conn, account="a@x.com",
        max_senders=1, sample_per_sender=5,
    )
    assert out["profiled"] == ["bob@y.com"]  # bob (2 msgs) outranks carol (1) under max_senders=1
    p = db.get_sender_profile(conn, "a@x.com", "bob@y.com")
    assert p is not None and p["source"] == "backfill"
    assert p["voice_notes"] == "writes briefly, signs off casually"
    assert "<SENT_" in captured[0]  # owner prose fenced before the LLM (AC16/N2)


def test_backfill_skips_already_profiled_unless_forced(tmp_path) -> None:
    conn = db.connect(tmp_path / "state.db")
    db.upsert_sender_profile(
        conn, account="a@x.com", sender_email="bob@y.com", voice_notes="existing", source="backfill"
    )
    reader = FakeReader([{"id": "1"}], _msgs(("1", "bob@y.com", "Hi Bob.")))
    out = backfill.backfill_sender_profiles(
        reader=reader, summarize_fn=lambda s, u: "new note", conn=conn, account="a@x.com",
        max_senders=5, sample_per_sender=5,
    )
    assert out["skipped"] == ["bob@y.com"] and out["profiled"] == []
    assert db.get_sender_profile(conn, "a@x.com", "bob@y.com")["voice_notes"] == "existing"
    out2 = backfill.backfill_sender_profiles(
        reader=reader, summarize_fn=lambda s, u: "new note", conn=conn, account="a@x.com",
        max_senders=5, sample_per_sender=5, force=True,
    )
    assert out2["profiled"] == ["bob@y.com"]
    assert db.get_sender_profile(conn, "a@x.com", "bob@y.com")["voice_notes"] == "new note"


def test_backfill_isolates_per_sender_failure(tmp_path) -> None:
    conn = db.connect(tmp_path / "state.db")
    reader = FakeReader(
        [{"id": "1"}, {"id": "2"}],
        _msgs(("1", "bob@y.com", "Hi Bob."), ("2", "carol@z.com", "Hi Carol.")),
    )

    def flaky(system, user):
        if "Bob" in user:
            raise RuntimeError("LLM down for bob")
        return "carol note"

    out = backfill.backfill_sender_profiles(
        reader=reader, summarize_fn=flaky, conn=conn, account="a@x.com",
        max_senders=5, sample_per_sender=5,
    )
    assert out["profiled"] == ["carol@z.com"]
    assert any(e["sender"] == "bob@y.com" for e in out["errors"])
    assert db.get_sender_profile(conn, "a@x.com", "carol@z.com") is not None
    assert db.get_sender_profile(conn, "a@x.com", "bob@y.com") is None


def test_backfill_respects_max_senders(tmp_path) -> None:
    conn = db.connect(tmp_path / "state.db")
    reader = FakeReader(
        [{"id": "1"}, {"id": "2"}, {"id": "3"}],
        _msgs(("1", "bob@y.com", "x"), ("2", "bob@y.com", "y"), ("3", "carol@z.com", "z")),
    )
    out = backfill.backfill_sender_profiles(
        reader=reader, summarize_fn=lambda s, u: "note", conn=conn, account="a@x.com",
        max_senders=1, sample_per_sender=5,
    )
    assert out["profiled"] == ["bob@y.com"]  # only the top recipient
