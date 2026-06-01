"""Seed the 8 numbered Gmail labels and apply a category to a thread.

Applying a category adds its label, removes every other category label, and (for
3–8) removes INBOX so the thread archives — only To Respond + FYI stay in the
inbox. Re-labelling happens at the thread level so a new reply can't leave an
older message stranded on a stale category. Idempotent label seeding. Needs a
live Gmail service (mock-tested here).
"""

from __future__ import annotations

from typing import Any

from .labels import CATEGORIES, category_by_name, label_name


def ensure_labels(service: Any) -> dict[str, str]:
    """Idempotently create the 8 labels; return {label_name: gmail_label_id}."""
    existing = {
        lbl["name"]: lbl["id"]
        for lbl in service.users().labels().list(userId="me").execute().get("labels", [])
    }
    out: dict[str, str] = {}
    for c in CATEGORIES:
        name = label_name(c)
        if name in existing:
            out[name] = existing[name]
        else:
            created = (
                service.users()
                .labels()
                .create(
                    userId="me",
                    body={
                        "name": name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
                .execute()
            )
            out[name] = created["id"]
    return out


def apply_category(
    service: Any,
    message_id: str,
    category_name: str,
    label_ids: dict[str, str],
    *,
    thread_id: str | None = None,
) -> None:
    """Add the category label, remove other category labels, archive if skip_inbox.

    Applied at the THREAD level when ``thread_id`` is given, so sibling messages
    can't keep a now-stale category. Without this, a thread the sent-handler had
    moved to "7: Actioned" (thread-level) keeps Actioned on its older messages
    once a new reply arrives and is classified "1: To Respond" — and the thread
    ends up showing both. Re-labelling the whole thread keeps exactly one
    category per thread, matching the sent-handler. Falls back to a message-level
    modify only when the thread id is unknown.
    """
    c = category_by_name(category_name)
    if c is None:
        return
    target = label_name(c)
    add = [label_ids[target]] if target in label_ids else []
    remove = [lid for n, lid in label_ids.items() if n != target]
    if c.skip_inbox:
        remove.append("INBOX")
    body = {"addLabelIds": add, "removeLabelIds": remove}
    users = service.users()
    if thread_id:
        users.threads().modify(userId="me", id=thread_id, body=body).execute()
    else:
        users.messages().modify(userId="me", id=message_id, body=body).execute()
