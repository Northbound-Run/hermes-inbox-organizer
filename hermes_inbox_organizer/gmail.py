"""Gmail read client — the agent's eyes on the inbox.

The read tools fetch threads/messages so the agent can compose a reply from the
actual email content. ``GoogleGmailReader`` wraps the Gmail API
(google-api-python-client) and needs live OAuth creds. The
``GmailReader`` protocol + the ``account_id -> reader`` resolver are seams, so
the read tools are unit-tested with a fake reader — no creds required.

``parse_message`` decodes a Gmail message resource into a compact, agent-friendly
dict (headers + plaintext body).
"""

from __future__ import annotations

import base64
from typing import Any, Callable, Optional, Protocol


def _b64url_decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
        "utf-8", "replace"
    )


def _header(headers: list[dict], name: str) -> str:
    for h in headers or []:
        if str(h.get("name", "")).lower() == name.lower():
            return str(h.get("value", ""))
    return ""


def _extract_plain(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain":
        data = (payload.get("body") or {}).get("data")
        if data:
            return _b64url_decode(data)
    for part in payload.get("parts") or []:
        found = _extract_plain(part)
        if found:
            return found
    return ""


def parse_message(msg: dict, *, body_limit: int = 4000) -> dict:
    """Gmail message resource -> compact dict for the agent.

    ``internal_date`` (Gmail's authoritative receive time, epoch **ms**) and
    ``label_ids`` are surfaced for the rollup's exact-window filter and
    label->category mapping; both default to a benign empty value so existing
    callers (full-body parses without ``internalDate``/``labelIds``) are
    unaffected.
    """
    payload = msg.get("payload") or {}
    headers = payload.get("headers") or []
    try:
        internal_date = int(msg.get("internalDate") or 0)
    except (TypeError, ValueError):
        internal_date = 0
    return {
        "id": msg.get("id", ""),
        "thread_id": msg.get("threadId", ""),
        "from": _header(headers, "from"),
        "to": _header(headers, "to"),
        "cc": _header(headers, "cc"),
        "subject": _header(headers, "subject"),
        "date": _header(headers, "date"),
        "snippet": msg.get("snippet", ""),
        "body": _extract_plain(payload)[:body_limit],
        "internal_date": internal_date,
        "label_ids": list(msg.get("labelIds") or []),
    }


class GmailReader(Protocol):
    def get_thread(self, thread_id: str) -> dict: ...
    def list_messages(self, query: str, max_results: int) -> list[dict]: ...
    def list_messages_page(
        self, query: str, max_results: int, page_token: Optional[str] = None
    ) -> dict: ...
    def get_message(self, message_id: str, format: str = "full") -> dict: ...
    def list_labels(self) -> list[dict]: ...


# Seam: account_id -> reader, or None when the account isn't connected yet.
ResolveReader = Callable[[str], Optional[GmailReader]]


def reader_from_token(tok: Any) -> "GoogleGmailReader":
    """Build a live reader from a stored AccountToken (auto-refreshing creds).

    Lazy google imports; needs the [live] deps installed. Not unit-tested.
    """
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        None,
        refresh_token=tok.refresh_token,
        client_id=tok.client_id,
        client_secret=tok.client_secret,
        token_uri=tok.token_uri,
        scopes=list(tok.scopes),
    )
    return GoogleGmailReader(build("gmail", "v1", credentials=creds))


class GoogleGmailReader:
    """Live reader over the Gmail API. Needs OAuth creds; not unit-tested."""

    def __init__(self, service: Any) -> None:
        self._svc = service  # googleapis gmail "users" resource

    def get_thread(self, thread_id: str) -> dict:
        return (
            self._svc.users()
            .threads()
            .get(userId="me", id=thread_id, format="full")
            .execute()
        )

    def list_messages(self, query: str, max_results: int) -> list[dict]:
        resp = (
            self._svc.users()
            .messages()
            .list(userId="me", q=query, maxResults=max_results)
            .execute()
        )
        return resp.get("messages", []) or []

    def list_messages_page(
        self, query: str, max_results: int, page_token: Optional[str] = None
    ) -> dict:
        """One page of a messages.list, raw envelope.

        Returns ``{messages, nextPageToken, resultSizeEstimate}`` so the caller
        can page + compute a truncation signal (the list-returning
        ``list_messages`` drops the token/estimate).
        """
        return (
            self._svc.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=max_results,
                pageToken=page_token,
            )
            .execute()
        )

    def get_message(self, message_id: str, format: str = "full") -> dict:
        kwargs: dict[str, Any] = {"userId": "me", "id": message_id, "format": format}
        if format == "metadata":
            # Headers we actually use; trims the payload (no body) for the rollup.
            kwargs["metadataHeaders"] = ["From", "Subject", "Date"]
        return self._svc.users().messages().get(**kwargs).execute()

    def list_labels(self) -> list[dict]:
        """All labels for the mailbox (read-only). Used to map label_ids -> category."""
        return (
            self._svc.users()
            .labels()
            .list(userId="me")
            .execute()
            .get("labels", [])
            or []
        )


def service_from_token(tok: Any) -> Any:
    """Build an auto-refreshing Gmail API service from a stored AccountToken."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    creds = Credentials(
        None,
        refresh_token=tok.refresh_token,
        client_id=tok.client_id,
        client_secret=tok.client_secret,
        token_uri=tok.token_uri,
        scopes=list(tok.scopes),
    )
    return build("gmail", "v1", credentials=creds)


def build_reply_headers(thread: dict, account_email: str) -> dict:
    """Derive reply headers from a thread: target = last message not from us."""
    msgs = thread.get("messages", []) or []
    target = None
    for m in reversed(msgs):
        hdrs = (m.get("payload") or {}).get("headers") or []
        if account_email.lower() not in _header(hdrs, "from").lower():
            target = m
            break
    if target is None and msgs:
        target = msgs[-1]
    hdrs = (target.get("payload") or {}).get("headers") or [] if target else []
    subject = _header(hdrs, "subject")
    msg_id = _header(hdrs, "message-id")
    references = _header(hdrs, "references")
    return {
        "to": _header(hdrs, "from"),
        "subject": subject if subject.lower().startswith("re:") else f"Re: {subject}",
        "in_reply_to": msg_id,
        "references": (f"{references} {msg_id}".strip() if references else msg_id),
    }


class GmailDraftWriter:
    """Creates a real Gmail draft reply on a thread (the 'hands'). Needs live creds.

    Creates a draft per call; idempotency is enforced upstream by the draft
    ledger (the ``draft_requests`` table — see ``db.py`` / ``draft_trigger``).
    """

    def __init__(self, service: Any, account_email: str) -> None:
        self._svc = service
        self._email = account_email

    def create_draft(self, *, account_id: str, thread_id: str, body: str) -> str:
        import base64
        from email.mime.text import MIMEText

        thread = (
            self._svc.users()
            .threads()
            .get(
                userId="me",
                id=thread_id,
                format="metadata",
                metadataHeaders=["From", "To", "Subject", "Message-ID", "References"],
            )
            .execute()
        )
        h = build_reply_headers(thread, self._email)
        mime = MIMEText(body, "plain", "utf-8")
        mime["From"] = self._email
        mime["To"] = h["to"]
        mime["Subject"] = h["subject"]
        if h["in_reply_to"]:
            mime["In-Reply-To"] = h["in_reply_to"]
            mime["References"] = h["references"]
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        resp = (
            self._svc.users()
            .drafts()
            .create(userId="me", body={"message": {"raw": raw, "threadId": thread_id}})
            .execute()
        )
        return resp.get("id", "")


def writer_from_token(tok: Any) -> "GmailDraftWriter":
    return GmailDraftWriter(service_from_token(tok), tok.email)
