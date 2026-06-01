"""GmailDraftWriter: reply-header derivation + MIME draft creation (fake service)."""

from __future__ import annotations

import base64

from hermes_inbox_organizer.gmail import GmailDraftWriter, build_reply_headers


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
