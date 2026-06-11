"""Process one new message: classify → apply label → (To Respond) wake to draft.

This is the per-message core of the autonomous loop. The trigger (Pub/Sub pull →
drainer) feeds message ids here. `classify_fn` and `wake_fn` are seams so the
orchestration is unit-tested without an LLM or the agent.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from . import db
from .classifier import classify as _classify
from .config import get_config
from .gmail import parse_message
from .labels_apply import apply_category
from .modules.base import InboundEvent

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
    registry: Optional[Any] = None,
) -> str:
    """Returns the bare category name applied to the message.

    When ``conn`` (a DB connection) is given, the classification and the thread's
    latest state are persisted (``classified_messages`` / ``thread_state``).

    When a module ``registry`` is given, it drives the DECISION phase
    (``registry.classify`` — module overrides, else the default classifier) and,
    AFTER the label is applied + persisted, the NOTIFICATION phase
    (``registry.dispatch_inbound`` — observers, offloaded). With ``registry=None``
    the behavior is exactly the legacy path (``classify_fn``, no dispatch), so
    the routing is unit-tested without any modules.

    With the label system disabled (``INBOX_LABELS_ENABLED=0``) the Gmail label
    mutation is skipped; classification, persistence, module dispatch, and the
    To-Respond wake all still run.
    """
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="full")
        .execute()
    )
    parsed = parse_message(msg)
    category = registry.classify(parsed) if registry is not None else classify_fn(parsed)
    thread_id = parsed.get("thread_id", "")
    if get_config().labels_enabled:
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
    # Notification phase — only after a successful apply + persist, so a failed
    # mutation never fires observers (e.g. a 2FA push) for mail that wasn't labelled.
    # (With labels disabled the apply is skipped by config, not failed — dispatch runs.)
    if registry is not None:
        registry.dispatch_inbound(
            InboundEvent(
                account_id=account_id,
                message_id=message_id,
                thread_id=thread_id,
                parsed=parsed,
                category=category,
            )
        )
    if category == "To Respond" and wake_fn is not None:
        wake_fn(
            account_id=account_id,
            thread_id=thread_id,
            subject=parsed.get("subject", ""),
            sender=parsed.get("from", ""),
        )
    return category
