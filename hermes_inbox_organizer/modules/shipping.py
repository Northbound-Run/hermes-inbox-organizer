"""Shipping tracker module (Phase 4) — detect parcels, poll 17track, push updates.

Flow:
* ``on_inbound`` (observer, offloaded): detect tracking number(s) in a shipping
  email, record them, and register new ones with 17track (1 quota each; capped
  by ``max_active``; quota/errors degrade to label-only, never crash).
* ``periodic`` (timer job): poll the active set via 17track ``gettrackinfo``
  (batched <=40/call — no webhooks, no public ingress), and push a one-line
  update on each MEANINGFUL stage change (in transit → out for delivery →
  delivered / exception). ``last_notified_stage`` advances only after a
  successful push, so a failed notify re-fires next poll (B3). Terminal stages
  (delivered / exception / expired) drop the parcel from the poll set.
* ``tools`` (on-demand): ``inbox_track_packages`` lists what's being tracked.

Detection is precision-first deterministic regex (no LLM); 17track itself
rejects malformed numbers (rejected = no quota cost). The 17track client is a
seam (:class:`~hermes_inbox_organizer.modules.track17.FakeTrack17Client`) so the
module is unit-tested without network; ``on_inbound``/poll run on worker threads
so each opens its own short-lived DB connection.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from typing import Any, Callable, Optional

from .. import db
from .base import InboundEvent, Module, PeriodicJob, ToolSpec
from .track17 import MAX_PER_CALL, TERMINAL_STAGES, Track17Client, TrackStatus

logger = logging.getLogger(__name__)

_MAX_SUBJECT = 200
_MAX_BODY = 4000

# A message is shipping-related only if it mentions shipping/tracking context —
# the gate that keeps non-shipping mail from registering random numbers.
_SHIP_CUE_RE = re.compile(
    r"\b(track(?:ing)?|shipped|shipment|out for delivery|on its way|delivered|"
    r"delivery|package|parcel|courier|carrier|usps|ups|fedex|dhl|ontrac|"
    r"royal mail|canada post)\b",
    re.IGNORECASE,
)

# Tracking-number shapes (precision-first; all linear/bounded). Order = priority.
_TRACKING_PATTERNS = (
    re.compile(r"\b(1Z[0-9A-Z]{16})\b"),  # UPS — very distinctive
    re.compile(  # explicit "tracking number: XXXX"
        r"tracking\s*(?:number|no\.?|#|id)?\s*[:#]?\s*([A-Za-z0-9]{10,35})\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(\d{12,22})\b"),  # USPS / FedEx long numeric
)

# Friendly labels for the normalized 17track stages.
_STAGE_LABELS = {
    "InfoReceived": "label created",
    "InTransit": "in transit",
    "OutForDelivery": "out for delivery",
    "AvailableForPickup": "ready for pickup",
    "DeliveryFailure": "delivery attempt failed",
    "Delivered": "delivered ✅",
    "Exception": "exception — needs attention",
    "Expired": "tracking expired",
    "NotFound": "not found yet",
}


def _plausible(num: str) -> bool:
    """Bound + sanity-check a candidate tracking number before registering it."""
    return (
        10 <= len(num) <= 35
        and num.isalnum()
        and any(c.isdigit() for c in num)
        and not num.isalpha()
    )


def detect_tracking_numbers(parsed: dict) -> list:
    """Return unique candidate tracking numbers in a shipping email (≤5), or []."""
    subject = (parsed.get("subject") or "")[:_MAX_SUBJECT]
    body = (parsed.get("body") or parsed.get("snippet") or "")[:_MAX_BODY]
    haystack = f"{subject}\n{body}"
    if not _SHIP_CUE_RE.search(haystack):
        return []
    found: list = []
    for pat in _TRACKING_PATTERNS:
        for m in pat.finditer(haystack):
            num = m.group(1).upper()
            if _plausible(num) and num not in found:
                found.append(num)
                if len(found) >= 5:
                    return found
    return found


def _format_update(number: str, st: TrackStatus) -> str:
    label = _STAGE_LABELS.get(st.stage or "", st.stage or "update")
    return f"\U0001f4e6 Package {number}: {label}"


class ShippingModule(Module):
    name = "shipping"

    def __init__(
        self,
        *,
        notifier: Any,
        client: Optional[Track17Client],
        enabled: bool = True,
        max_active: int = 50,
        poll_interval_s: int = 4 * 3600,
        db_connect: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._notifier = notifier
        self._client = client
        self._enabled = enabled and client is not None
        self._max_active = max_active
        self._poll_interval_s = poll_interval_s
        self._db_connect = db_connect or db.connect

    @property
    def enabled(self) -> bool:
        return self._enabled

    # -- detect + register (observer) -------------------------------------------
    def on_inbound(self, event: InboundEvent) -> None:
        numbers = detect_tracking_numbers(event.parsed)
        if not numbers:
            return
        for num in numbers:
            with contextlib.closing(self._db_connect()) as conn:
                if not db.add_tracked_package(conn, event.account_id, num):
                    continue  # repeat email about a parcel we already track
                if db.count_active_packages(conn) >= self._max_active:
                    logger.warning(
                        "inbox shipping: at max active (%d); not tracking %s (label only)",
                        self._max_active,
                        num,
                    )
                    continue
            self._register(event.account_id, num)

    def _register(self, account: str, number: str) -> None:
        try:
            result = self._client.register([number])
        except Exception:
            logger.exception("inbox shipping: register call failed for %s", number)
            return
        if result.quota_exhausted:
            logger.warning("inbox shipping: 17track quota exhausted; %s not tracked (label only)", number)
            return
        if number in result.accepted:
            with contextlib.closing(self._db_connect()) as conn:
                db.mark_package_registered(conn, account, number)
            logger.info("inbox shipping: registered %s for %s", number, account)
        else:
            logger.info("inbox shipping: 17track rejected %s (%s)", number, result.rejected.get(number, "?"))

    # -- poll + notify (periodic) -----------------------------------------------
    def poll_once(self) -> None:
        with contextlib.closing(self._db_connect()) as conn:
            rows = db.get_active_packages(conn)
        if not rows:
            return
        by_number = {r["tracking_number"]: r for r in rows}
        numbers = list(by_number)
        for i in range(0, len(numbers), MAX_PER_CALL):
            batch = numbers[i : i + MAX_PER_CALL]
            try:
                statuses = self._client.get_statuses(batch)
            except Exception:
                logger.exception("inbox shipping: poll batch failed")
                continue
            for st in statuses:
                self._handle_status(by_number.get(st.number), st)

    def _handle_status(self, row: Any, st: TrackStatus) -> None:
        if row is None or not st.stage:
            return
        account, number = row["account"], row["tracking_number"]
        terminal = st.stage in TERMINAL_STAGES
        with contextlib.closing(self._db_connect()) as conn:
            db.update_package_stage(conn, account, number, st.stage, terminal=terminal)
        if st.stage == row["last_notified_stage"]:
            return  # no new stage → stay quiet (minor scans don't change the stage)
        # Notify, then advance the marker ONLY on success so a failed push re-fires.
        if self._notifier.send(_format_update(number, st)):
            with contextlib.closing(self._db_connect()) as conn:
                db.set_package_notified_stage(conn, account, number, st.stage)

    def periodic(self) -> list:
        return [
            PeriodicJob(
                name="shipping-poll", interval_s=self._poll_interval_s, run_once=self.poll_once
            )
        ]

    # -- on-demand tool ---------------------------------------------------------
    def tools(self) -> list:
        return [
            ToolSpec(
                name=INBOX_TRACK_PACKAGES_SCHEMA["name"],
                schema=INBOX_TRACK_PACKAGES_SCHEMA,
                handler=self._track_packages_handler,
                description=INBOX_TRACK_PACKAGES_SCHEMA["description"],
                toolset="inbox",
            )
        ]

    def _track_packages_handler(self, args: dict, **_kwargs: Any) -> str:
        a = args or {}
        try:
            account = a.get("account_id") or None
            include = bool(a.get("include_delivered"))
            with contextlib.closing(self._db_connect()) as conn:
                rows = db.list_tracked_packages(conn, account, include_terminal=include)
            packages = [
                {
                    "account": r["account"],
                    "tracking_number": r["tracking_number"],
                    "carrier": r["carrier"],
                    "stage": _STAGE_LABELS.get(r["last_stage"] or "", r["last_stage"]),
                    "delivered": bool(r["terminal"]),
                }
                for r in rows
            ]
            return json.dumps({"count": len(packages), "packages": packages})
        except Exception as exc:  # contract: never raise out of a tool handler
            return json.dumps({"error": f"inbox_track_packages failed: {exc}"})


INBOX_TRACK_PACKAGES_SCHEMA: dict = {
    "name": "inbox_track_packages",
    "description": (
        "List the packages the organizer is currently tracking (via 17track), each "
        "with its latest delivery stage. READ-ONLY. By default shows in-flight "
        "parcels across all connected accounts; pass include_delivered=true to also "
        "show recently delivered ones, or account_id to scope to one mailbox. The "
        "organizer also pushes updates automatically as parcels progress."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "account_id": {"type": "string", "description": "Optional connected account id (email)."},
            "include_delivered": {
                "type": "boolean",
                "description": "Include delivered/terminal parcels (default false).",
                "default": False,
            },
        },
    },
}
