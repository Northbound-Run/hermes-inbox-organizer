"""The continual inbox daemon — runs in-process inside Hermes.

Proven viable by ``hermes-chat-recorder`` (``_background_loop.py``: a persistent
asyncio loop on a ``daemon=True`` thread) and the in-tree ``plugins/google_meet``
(process managers + realtime connections). A plugin may own long-lived work
started from ``register()``.

The daemon consumes Gmail change notifications via Pub/Sub **streaming pull** —
an *outbound* long-lived connection, so there is NO public webhook, no Cloudflare
tunnel, and no OIDC verification of Google's POSTs (all required by push). It
classifies each new message with the cheap local classifier, applies a label,
and for "To Respond" messages asks the agent to draft (via the trigger callback).

``MessageSource`` is a seam: the real source is ``PubSubMessageSource`` (streaming
pull), but tests drive a fake source so the routing logic runs without live GCP.
The Pub/Sub subscriber owns its own background thread, mirroring how the real
``google.cloud.pubsub_v1`` client delivers callbacks.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

TO_RESPOND = "1: To Respond"


@dataclass(frozen=True)
class InboundMessage:
    account_id: str
    message_id: str
    thread_id: str
    sender: str
    subject: str
    label_ids: tuple[str, ...] = ()


# A source pushes inbound messages to a callback (mirrors pubsub_v1.subscribe).
OnMessage = Callable[[InboundMessage], object]  # return (a label, or None) is ignored by the source


class MessageSource(Protocol):
    def start(self, on_message: OnMessage) -> None: ...
    def stop(self) -> None: ...


class NullSource:
    """Inert source so ``daemon.start()`` is safe when unconfigured (tests/CI)."""

    def start(self, on_message: OnMessage) -> None:
        return None

    def stop(self) -> None:
        return None


Classifier = Callable[[InboundMessage], str]  # returns a category label name
ApplyLabel = Callable[[InboundMessage, str], None]
OnToRespond = Callable[[InboundMessage], None]


class InboxDaemon:
    """Wires a message source to the classify -> label -> maybe-draft routing.

    ``handle`` is pure routing (no I/O of its own beyond the injected callbacks)
    and is the unit-tested core. ``start``/``stop`` delegate to the source, whose
    real implementation owns the continual connection/thread.
    """

    def __init__(
        self,
        *,
        source: MessageSource,
        classifier: Classifier,
        apply_label: ApplyLabel,
        on_to_respond: OnToRespond,
    ) -> None:
        self._source = source
        self._classifier = classifier
        self._apply_label = apply_label
        self._on_to_respond = on_to_respond
        self._pending: list[str] = []

    def handle(self, msg: InboundMessage) -> str:
        category = self._classifier(msg)
        self._apply_label(msg, category)
        if category == TO_RESPOND:
            if msg.thread_id not in self._pending:
                self._pending.append(msg.thread_id)
            # A draft-trigger failure (e.g. gateway not captured yet) must not
            # crash classification/labeling — it stays pending and is retried.
            try:
                self._on_to_respond(msg)
            except Exception:
                logger.exception(
                    "inbox daemon: draft trigger failed for thread %s", msg.thread_id
                )
        return category

    def pending(self) -> list[str]:
        """Thread ids awaiting a draft (drives the pre_llm_call nudge)."""
        return list(self._pending)

    def clear_pending(self, thread_id: str) -> None:
        if thread_id in self._pending:
            self._pending.remove(thread_id)

    def start(self) -> None:
        self._source.start(self.handle)
        logger.info("inbox daemon started")

    def stop(self) -> None:
        self._source.stop()


class PubSubMessageSource:
    """Real source: Gmail watch() -> Pub/Sub topic, consumed via streaming pull.

    Streaming pull is an outbound connection; the google client runs callbacks on
    its own background thread (``StreamingPullFuture``). Decoding a notification
    into ``InboundMessage``s (history.list since the stored historyId, routed by
    label) needs live creds, so it lives here behind ``start()`` and is not
    exercised by unit tests.
    """

    def __init__(self, project_id: str, subscription_id: str) -> None:
        self._project_id = project_id
        self._subscription_id = subscription_id
        self._future = None

    def start(self, on_message: OnMessage) -> None:
        from google.cloud import pubsub_v1  # lazy: no dep needed for unit tests

        subscriber = pubsub_v1.SubscriberClient()
        path = subscriber.subscription_path(self._project_id, self._subscription_id)

        def _callback(pubsub_msg) -> None:
            try:
                for inbound in self._decode(pubsub_msg):
                    on_message(inbound)
                pubsub_msg.ack()
            except Exception:
                logger.exception("pubsub: decode/handle failed; nacking")
                pubsub_msg.nack()

        self._future = subscriber.subscribe(path, callback=_callback)
        logger.info("pubsub streaming pull started on %s", path)

    def _decode(self, pubsub_msg):  # pragma: no cover - needs live Gmail
        raise NotImplementedError(
            "history.list decode needs live creds"
        )

    def stop(self) -> None:
        if self._future is not None:
            self._future.cancel()
            self._future = None
