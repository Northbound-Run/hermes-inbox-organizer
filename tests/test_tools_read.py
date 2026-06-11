"""Read tools: message parsing + handler behavior against a fake reader."""

from __future__ import annotations

import base64
import json

from hermes_inbox_organizer.gmail import parse_message
from hermes_inbox_organizer.tools_read import (
    make_inbox_get_email_handler,
    make_inbox_get_thread_handler,
    make_inbox_list_emails_handler,
)


def _b64url(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _msg(mid: str, frm: str, subj: str, body: str) -> dict:
    return {
        "id": mid,
        "snippet": body[:20],
        "payload": {
            "headers": [
                {"name": "From", "value": frm},
                {"name": "Subject", "value": subj},
            ],
            "mimeType": "text/plain",
            "body": {"data": _b64url(body)},
        },
    }


class FakeReader:
    def __init__(self, thread=None, messages=None) -> None:
        self._thread = thread
        self._messages = messages or {}

    def get_thread(self, thread_id: str) -> dict:
        return self._thread

    def get_message(self, message_id: str, format: str = "full") -> dict:
        return self._messages[message_id]

    def list_messages(self, query: str, max_results: int) -> list[dict]:
        return [{"id": k} for k in self._messages]


def test_parse_message_decodes_headers_and_body() -> None:
    parsed = parse_message(_msg("m1", "alice@x.com", "Lunch?", "Let's meet Tuesday."))
    assert parsed["from"] == "alice@x.com"
    assert parsed["subject"] == "Lunch?"
    assert parsed["body"] == "Let's meet Tuesday."


def test_parse_message_surfaces_bulk_mail_signals() -> None:
    # Header shape from unsubscribe.eml (a real ESP newsletter).
    msg = _msg("m1", "team@dataforseo.com", "New updates!", "... Unsubscribe here")
    msg["payload"]["headers"] += [
        {"name": "List-Unsubscribe", "value": "<mailto:u@x.com>, <https://x.com/u>"},
        {"name": "List-Unsubscribe-Post", "value": "List-Unsubscribe=One-Click"},
        {"name": "Precedence", "value": "Bulk"},
    ]
    parsed = parse_message(msg)
    assert parsed["list_unsubscribe"] is True
    assert parsed["one_click_unsubscribe"] is True
    assert parsed["precedence"] == "bulk"  # normalized


def test_parse_message_bulk_mail_signals_default_benign() -> None:
    parsed = parse_message(_msg("m1", "alice@x.com", "Lunch?", "Tuesday?"))
    assert parsed["list_unsubscribe"] is False
    assert parsed["one_click_unsubscribe"] is False
    assert parsed["precedence"] == ""


def test_get_thread_returns_parsed_messages() -> None:
    thread = {"messages": [_msg("m1", "a@x.com", "Hi", "first"), _msg("m2", "b@x.com", "Re: Hi", "second")]}
    handler = make_inbox_get_thread_handler(lambda aid: FakeReader(thread=thread))
    out = json.loads(handler({"account_id": "a1", "thread_id": "t1"}))
    assert out["thread_id"] == "t1"
    assert [m["body"] for m in out["messages"]] == ["first", "second"]


def test_get_thread_uses_deeper_body_limit_for_drafting() -> None:
    # N3: inbox_get_thread reads 8000 chars/message (vs the shared 4000 default) so deep
    # threads aren't over-truncated when drafting.
    long_body = "x" * 6000  # > 4000 default, < 8000 draft limit
    thread = {"messages": [_msg("m1", "a@x.com", "Hi", long_body)]}
    handler = make_inbox_get_thread_handler(lambda aid: FakeReader(thread=thread))
    out = json.loads(handler({"account_id": "a1", "thread_id": "t1"}))
    assert len(out["messages"][0]["body"]) == 6000  # not truncated at 4000


def test_get_thread_reports_not_connected() -> None:
    handler = make_inbox_get_thread_handler(lambda aid: None)
    out = json.loads(handler({"account_id": "a1", "thread_id": "t1"}))
    assert "not connected" in out["error"]


def test_get_thread_validates_args() -> None:
    handler = make_inbox_get_thread_handler(lambda aid: FakeReader())
    assert "error" in json.loads(handler({"account_id": "a1"}))


def test_list_emails_searches_named_account() -> None:
    messages = {"m1": _msg("m1", "a@x.com", "One", "body one"), "m2": _msg("m2", "b@x.com", "Two", "body two")}
    handler = make_inbox_list_emails_handler(
        lambda aid: FakeReader(messages=messages), lambda: ["a1@x.com", "b2@x.com"]
    )
    out = json.loads(handler({"account_id": "a1@x.com", "query": "subject:One"}))
    assert out["count"] == 2  # fake returns all regardless of query
    assert out["accounts_searched"] == ["a1@x.com"]
    assert all(m["account"] == "a1@x.com" for m in out["messages"])


def test_list_emails_defaults_to_all_connected_accounts() -> None:
    messages = {"m1": _msg("m1", "a@x.com", "One", "body one")}
    seen: list[str] = []

    def resolve(aid: str) -> FakeReader:
        seen.append(aid)
        return FakeReader(messages=messages)

    handler = make_inbox_list_emails_handler(resolve, lambda: ["a1@x.com", "b2@x.com"])
    out = json.loads(handler({"query": "in:inbox"}))  # no account_id → all accounts
    assert out["accounts_searched"] == ["a1@x.com", "b2@x.com"]
    assert out["count"] == 2  # one msg per account
    assert {m["account"] for m in out["messages"]} == {"a1@x.com", "b2@x.com"}
    assert seen == ["a1@x.com", "b2@x.com"]


def test_list_emails_isolates_per_account_failure() -> None:
    class Boom:
        def list_messages(self, q: str, n: int):
            raise RuntimeError("api down")

        def get_message(self, mid: str, format: str = "full"):
            raise RuntimeError("api down")

    good = {"m1": _msg("m1", "a@x.com", "Ok", "fine")}

    def resolve(aid: str):
        return Boom() if aid == "bad@x.com" else FakeReader(messages=good)

    handler = make_inbox_list_emails_handler(resolve, lambda: ["good@x.com", "bad@x.com"])
    out = json.loads(handler({}))  # all accounts, default query
    assert out["count"] == 1
    assert any(e["account"] == "bad@x.com" for e in out.get("errors", []))


def test_list_emails_no_accounts_connected() -> None:
    handler = make_inbox_list_emails_handler(lambda aid: None, lambda: [])
    out = json.loads(handler({"query": "in:inbox"}))
    assert "error" in out and "no accounts connected" in out["error"]


def test_get_email_parses_single() -> None:
    messages = {"m9": _msg("m9", "c@x.com", "Solo", "just me")}
    handler = make_inbox_get_email_handler(lambda aid: FakeReader(messages=messages))
    out = json.loads(handler({"account_id": "a1", "message_id": "m9"}))
    assert out["subject"] == "Solo"
    assert out["body"] == "just me"


def test_handlers_never_raise_on_reader_failure() -> None:
    class Boom:
        def get_message(self, mid):
            raise RuntimeError("api down")

    handler = make_inbox_get_email_handler(lambda aid: Boom())
    out = json.loads(handler({"account_id": "a1", "message_id": "m1"}))
    assert "error" in out
