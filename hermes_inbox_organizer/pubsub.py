"""Pub/Sub streaming-pull notification handling.

Gmail ``watch()`` publishes a base64 JSON ``{"emailAddress", "historyId"}`` to the
topic. We consume it via **streaming pull** — an outbound connection, so there is
no public webhook / tunnel / OIDC.

Ack contract: the callback must durably persist the notification
in one step, THEN ack. On persist failure it nacks so Pub/Sub redelivers
(durability before ack). An unknown account is acked + audited rather than
nacked forever. The actual ``history.list`` traversal is owned by a separate
drainer, not the pull callback.

``decode_gmail_notification`` and ``handle_pubsub_message`` are pure/seamed and
unit-tested here; only ``PubSubSubscriber.start`` touches the live google client.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GmailNotification:
    email_address: str
    history_id: int


class NotificationDecodeError(ValueError):
    pass


def decode_gmail_notification(data: str | bytes) -> GmailNotification:
    """Decode a Gmail Pub/Sub notification into a GmailNotification.

    The streaming-pull client delivers the already-wire-decoded payload (raw JSON:
    ``{"emailAddress","historyId"}``); push/wire delivers base64(JSON). Handle
    both — parse as JSON; if that fails, base64-decode then parse.
    """
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        try:
            obj = json.loads(base64.b64decode(data, validate=True))
        except (binascii.Error, json.JSONDecodeError, ValueError) as exc:
            raise NotificationDecodeError(str(exc)) from exc
    try:
        return GmailNotification(
            email_address=str(obj["emailAddress"]),
            history_id=int(obj["historyId"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise NotificationDecodeError(str(exc)) from exc


class Ack(Enum):
    ACK = "ack"
    NACK = "nack"


# Seams (live impls supplied at wiring time):
ResolveAccount = Callable[[str], str | None]  # email -> account_id | None
PersistNotification = Callable[[str, GmailNotification], None]  # raises on failure
Audit = Callable[[str, str], None]  # (event, detail)


def handle_pubsub_message(
    data_b64: str,
    *,
    resolve_account: ResolveAccount,
    persist: PersistNotification,
    audit: Audit,
) -> Ack:
    """Decide ack/nack for one Pub/Sub message per the durability contract."""
    try:
        notif = decode_gmail_notification(data_b64)
    except NotificationDecodeError:
        # Malformed payload will never decode — ack so it doesn't redeliver forever.
        audit("pubsub_decode_failed", data_b64[:64])
        return Ack.ACK

    account_id = resolve_account(notif.email_address)
    if account_id is None:
        # Not one of our connected accounts — ack + audit, no infinite redelivery.
        audit("pubsub_unknown_account", notif.email_address)
        return Ack.ACK

    try:
        persist(account_id, notif)  # durable insert in ONE step
    except Exception:
        # Persist failed — nack so Pub/Sub redelivers (we never ack un-persisted work).
        logger.exception("pubsub: persist failed for %s; nacking", account_id)
        return Ack.NACK

    return Ack.ACK


class PubSubSubscriber:
    """Live streaming-pull wiring. Not exercised by unit tests (needs GCP)."""

    def __init__(
        self,
        project_id: str,
        subscription_id: str,
        *,
        resolve_account: ResolveAccount,
        persist: PersistNotification,
        audit: Audit,
    ) -> None:
        self._project_id = project_id
        self._subscription_id = subscription_id
        self._resolve_account = resolve_account
        self._persist = persist
        self._audit = audit
        self._future = None

    def start(self) -> None:
        from google.cloud import pubsub_v1  # lazy: no dep needed for unit tests

        subscriber = pubsub_v1.SubscriberClient()
        path = subscriber.subscription_path(self._project_id, self._subscription_id)

        def _callback(message) -> None:  # google's StreamingPull message
            decision = handle_pubsub_message(
                message.data.decode() if isinstance(message.data, bytes) else message.data,
                resolve_account=self._resolve_account,
                persist=self._persist,
                audit=self._audit,
            )
            if decision is Ack.ACK:
                message.ack()
            else:
                message.nack()

        self._future = subscriber.subscribe(path, callback=_callback)
        logger.info("pubsub streaming pull started on %s", path)

    def stop(self) -> None:
        if self._future is not None:
            self._future.cancel()
            self._future = None
