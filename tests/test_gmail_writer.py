"""GmailDraftWriter: reply-header derivation + MIME draft creation (fake service)."""

from __future__ import annotations

import base64

from hermes_inbox_organizer.gmail import GmailDraftWriter, GoogleGmailReader, build_reply_headers


def _thread(messages: list[dict]) -> dict:
    return {"messages": messages}


def _m(frm: str, subject: str, msg_id: str = "", references: str = "") -> dict:
    hdrs = [{"name": "From", "value": frm}, {"name": "Subject", "value": subject}]
    if msg_id:
        hdrs.append({"name": "Message-ID", "value": msg_id})
    if references:
        hdrs.append({"name": "References", "value": references})
    return {"payload": {"headers": hdrs}}


def test_reply_headers_target_last_message_not_from_us() -> None:
    thread = _thread([
        _m("me@x.com", "Re: Hi", "<a@x>"),
        _m("alice@y.com", "Hi", "<b@y>"),       # last inbound -> reply target
        _m("me@x.com", "Re: Hi again", "<c@x>"),
    ])
    h = build_reply_headers(thread, "me@x.com")
    assert h["to"] == "alice@y.com"
    assert h["subject"] == "Re: Hi"
    assert h["in_reply_to"] == "<b@y>"


def test_reply_headers_dedupe_re_prefix() -> None:
    h = build_reply_headers(_thread([_m("a@y.com", "Re: lunch", "<x@y>")]), "me@x.com")
    assert h["subject"] == "Re: lunch"  # not "Re: Re: lunch"


# --- fake googleapis service chain ---
class _Exec:
    def __init__(self, val):
        self._val = val

    def execute(self):
        return self._val


class _Drafts:
    def __init__(self, rec):
        self._rec = rec

    def create(self, userId, body):
        self._rec["create_body"] = body
        return _Exec({"id": "draft-xyz"})

    def update(self, userId, id, body):
        self._rec["update_id"] = id
        self._rec["update_body"] = body
        return _Exec({"id": id})


class _Threads:
    def __init__(self, thread):
        self._thread = thread

    def get(self, **kwargs):
        return _Exec(self._thread)


class _Users:
    def __init__(self, thread, rec):
        self._thread = thread
        self._rec = rec

    def threads(self):
        return _Threads(self._thread)

    def drafts(self):
        return _Drafts(self._rec)


class _Service:
    def __init__(self, thread, rec):
        self._thread = thread
        self._rec = rec

    def users(self):
        return _Users(self._thread, self._rec)


def test_reader_metadata_fetch_includes_to_header() -> None:
    # Regression (Phase 4 live finding): the sent-mail backfill ranks recipients by the
    # To header, so metadata fetches MUST request it — Gmail omits unlisted headers.
    captured: dict = {}

    class _Msgs:
        def get(self, **kw):
            captured.update(kw)
            return _Exec({"id": kw.get("id")})

    class _U:
        def messages(self):
            return _Msgs()

    class _Svc:
        def users(self):
            return _U()

    GoogleGmailReader(_Svc()).get_message("m1", format="metadata")
    assert "To" in captured["metadataHeaders"] and "From" in captured["metadataHeaders"]


def test_create_draft_builds_mime_and_posts_to_thread() -> None:
    rec: dict = {}
    thread = _thread([_m("alice@y.com", "Project update", "<msg-1@y>")])
    writer = GmailDraftWriter(_Service(thread, rec), "me@gmail.com")

    draft_id = writer.create_draft(
        account_id="acct", thread_id="thread-42", body="Thanks Alice — looks great."
    )

    assert draft_id == "draft-xyz"
    msg = rec["create_body"]["message"]
    assert msg["threadId"] == "thread-42"

    import email as emaillib

    parsed = emaillib.message_from_bytes(base64.urlsafe_b64decode(msg["raw"]))
    assert parsed["To"] == "alice@y.com"
    assert parsed["Subject"] == "Re: Project update"
    assert parsed["In-Reply-To"] == "<msg-1@y>"
    assert "Thanks Alice" in parsed.get_payload(decode=True).decode("utf-8")


def test_update_draft_uses_drafts_update_not_create() -> None:
    # AC6: updating an existing draft hits drafts.update (no second draft created).
    rec: dict = {}
    thread = _thread([_m("alice@y.com", "Project update", "<msg-1@y>")])
    writer = GmailDraftWriter(_Service(thread, rec), "me@gmail.com")

    out = writer.update_draft(
        account_id="acct", thread_id="thread-42", body="Revised reply.", draft_id="draft-9"
    )

    assert out == "draft-9"
    assert "create_body" not in rec        # did NOT create a duplicate
    assert rec["update_id"] == "draft-9"
    msg = rec["update_body"]["message"]
    assert msg["threadId"] == "thread-42"

    import email as emaillib

    parsed = emaillib.message_from_bytes(base64.urlsafe_b64decode(msg["raw"]))
    assert parsed["To"] == "alice@y.com"
    assert parsed["Subject"] == "Re: Project update"
    assert "Revised reply." in parsed.get_payload(decode=True).decode("utf-8")
