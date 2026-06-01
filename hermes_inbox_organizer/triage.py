"""Process one new message: classify → apply label → (To Respond) wake to draft.

This is the per-message core of the autonomous loop. The trigger (Pub/Sub pull →
drainer) feeds message ids here. `classify_fn` and `wake_fn` are seams so the
orchestration is unit-tested without an LLM or the agent.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from . import db
from .classifier import classify as _classify
from .gmail import parse_message
from .labels_apply import apply_category

ClassifyFn = Callable[[dict], str]
# wake_fn(account_id, thread_id, subject, sender) -> induce an agent draft turn
WakeFn = Callable[..., Any]


def process_message(
    *,
    message_id: str,
    account_id: str,
    service: Any,
    label_ids: dict[str, str],
    classify_fn: ClassifyFn = _classify,
    wake_fn: Optional[WakeFn] = None,
    conn: Optional[Any] = None,
) -> str:
    """Returns the bare category name applied to the message.

    When ``conn`` (a DB connection) is given, the classification and the thread's
    latest state are persisted (``classified_messages`` / ``thread_state``).
    """
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )
    parsed = parse_message(msg)
    category = classify_fn(parsed)
    thread_id = parsed.get("thread_id", "")
    apply_category(service, message_id, category, label_ids, thread_id=thread_id)
    if conn is not None:
        db.record_classified_message(
            conn,
            account=account_id,
            message_id=message_id,
            thread_id=thread_id,
            category=category,
            from_addr=parsed.get("from", ""),
            subject=parsed.get("subject", ""),
        )
        db.upsert_thread_state(
            conn,
            account=account_id,
            thread_id=thread_id,
            last_message_id=message_id,
            last_category=category,
        )
    if category == "To Respond" and wake_fn is not None:
        wake_fn(
            account_id=account_id,
            thread_id=thread_id,
            subject=parsed.get("subject", ""),
            sender=parsed.get("from", ""),
        )
    return category
