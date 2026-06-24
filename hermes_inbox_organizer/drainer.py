"""Drain Gmail history since a cursor → process each new INBOX message.

A Pub/Sub notification only carries the new historyId; the actual new messages
come from history.list since the stored cursor (paginated). A 404 means the
cursor is too old → the caller triggers a full resync. INBOX-labelled
messagesAdded route to process_fn; the new max historyId is returned to advance
the cursor (only after processing, so a crash re-drains rather than skips).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

MAX_PAGES = 25


class StaleCursor(Exception):
    """history.list returned 404 — stored cursor too old; full resync needed."""


def _is_404(err: Exception) -> bool:
    resp = getattr(err, "resp", None)
    status = getattr(resp, "status", None) if resp is not None else getattr(err, "status_code", None)
    return str(status) == "404"


def drain_history(
    *,
    service: Any,
    start_history_id: str,
    process_fn: Callable[[str], Any],
    sent_fn: Callable[[str], Any] | None = None,
) -> str:
    """Triage new INBOX messages (process_fn) + new SENT messages (sent_fn) since
    start_history_id; return the new cursor."""
    inbox_ids: list[str] = []
    sent_ids: list[str] = []
    max_hid = start_history_id
    page_token = None
    pages = 0
    while pages < MAX_PAGES:
        try:
            resp = (
                service.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=["messageAdded"],
                    pageToken=page_token,
                )
                .execute()
            )
        except Exception as err:
            if _is_404(err):
                raise StaleCursor() from err
            raise

        if resp.get("historyId"):
            max_hid = resp["historyId"]
        for entry in resp.get("history", []) or []:
            for added in entry.get("messagesAdded", []) or []:
                m = added.get("message", {}) or {}
                mid = m.get("id")
                if not mid:
                    continue
                labels = m.get("labelIds") or []
                if "SENT" in labels:  # your own outbound -> sent-handler
                    sent_ids.append(mid)
                elif "INBOX" in labels:  # inbound -> triage
                    inbox_ids.append(mid)

        page_token = resp.get("nextPageToken")
        pages += 1
        if not page_token:
            break

    for mid in dict.fromkeys(inbox_ids):  # dedupe, preserve order
        try:
            process_fn(mid)
        except Exception:
            logger.exception("drain: triage failed for message %s", mid)
    if sent_fn is not None:
        for mid in dict.fromkeys(sent_ids):
            try:
                sent_fn(mid)
            except Exception:
                logger.exception("drain: sent-handler failed for message %s", mid)
    return max_hid
