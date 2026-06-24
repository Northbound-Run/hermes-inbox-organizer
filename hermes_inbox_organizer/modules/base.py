"""The module contract — how feature modules hook into the email flow.

A *module* is a self-contained, optionally-enabled unit (e.g. the unread rollup,
the 2FA notifier, the shipping tracker) that plugs into the autonomous triage
flow without the core having to know about it. The contract is **hybrid**:

* **observe** by default — ``on_inbound`` / ``on_sent`` fire AFTER the core has
  classified, labelled, and persisted a message. They are side-effect only
  (notify / track / persist) and are dispatched OFF the runtime lock on a worker
  pool, so a slow or failing module can't stall or break triage.
* **influence** by opt-in — ``classify_override`` runs in the DECISION phase,
  *under* the runtime lock, and may return a category to override the default
  classifier. It MUST be cheap and pure (regex/parse only, no I/O). Returning
  ``None`` defers to the default classifier; an invalid/unknown category is
  rejected by the registry and also defers.

Modules may also contribute agent **tools** and **periodic** timer jobs.

Events carry plain DATA (the already-parsed message dict), never the live Gmail
``service`` or the drain's DB connection — so a module that does I/O opens its
own short-lived ``db.connect()`` on its worker thread (the drain's connection is
single-threaded), and unit tests build events as plain dataclasses with no
Gmail/Hermes mocking.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InboundEvent:
    """One inbound message that was classified + labelled (notification phase)."""

    account_id: str
    message_id: str
    thread_id: str
    parsed: dict  # parse_message() output — headers + body/snippet + label_ids
    category: str  # the bare category actually applied (e.g. "To Respond", "Notification")


@dataclass(frozen=True)
class SentEvent:
    """One of the owner's SENT messages, after the sent-handler moved its thread."""

    account_id: str
    message_id: str
    thread_id: str
    parsed: dict
    target_category: str  # "Actioned" | "Awaiting Reply"


@dataclass(frozen=True)
class ToolSpec:
    """An agent tool a module contributes (registered via ``ctx.register_tool``)."""

    name: str
    schema: dict
    handler: Callable[..., Any]
    description: str = ""
    toolset: str = "inbox"
    emoji: str | None = None


@dataclass(frozen=True)
class PeriodicJob:
    """A timer job a module contributes; the runtime loops ``run_once`` every
    ``interval_s`` on a daemon thread. ``run_once`` must do one bounded pass and
    skip accounts no longer managed (mirrors the runtime's own reconciler)."""

    name: str
    interval_s: float
    run_once: Callable[[], None]


class Module:
    """Base class for inbox modules — subclass and override only what you need.

    Class attributes:
      * ``name``     — stable identifier (dedup keys, logs). Override it.
      * ``priority`` — lower runs earlier for ``classify_override`` (default 100).

    Instance contract (all optional; defaults are inert):
      * ``enabled``           — config gate; a disabled module is skipped entirely.
      * ``classify_override`` — DECISION phase, under the lock, pure/no-I/O.
      * ``on_inbound``/``on_sent`` — NOTIFICATION phase, offloaded, may do I/O.
      * ``tools``/``periodic``     — contributions wired at registration/start.
    """

    name: str = "module"
    priority: int = 100

    @property
    def enabled(self) -> bool:
        return True

    # -- decision phase (under the runtime lock — keep it cheap, no I/O) --------
    def classify_override(self, parsed: dict) -> str | None:
        """Return a bare category to override the default classifier, or None."""
        return None

    # -- notification phase (offloaded off the lock — I/O allowed) --------------
    def on_inbound(self, event: InboundEvent) -> None:
        return None

    def on_sent(self, event: SentEvent) -> None:
        return None

    # -- contributions ----------------------------------------------------------
    def tools(self) -> list[ToolSpec]:
        return []

    def periodic(self) -> list[PeriodicJob]:
        return []
