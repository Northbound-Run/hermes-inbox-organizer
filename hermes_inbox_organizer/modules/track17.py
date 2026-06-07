"""17track API client + normalized package stages (Phase 4).

17track is a universal carrier-tracking aggregator. We POLL it (no webhooks — no
public ingress, the same constraint that makes Gmail use Pub/Sub pull). Two
calls: ``register`` a tracking number (costs 1 quota on success; free tier =
100/month), then ``gettrackinfo`` to read its latest status.

The client returns a NORMALIZED view so the shipping module never depends on
17track's exact JSON: ``register`` -> :class:`RegisterResult`; ``get_statuses``
-> ``list[TrackStatus]``. Stages are 17track v2.2's ``latest_status.status``
strings. Behind a seam (:class:`FakeTrack17Client`) so the module is unit-tested
without network.

API (v2.2; base verified 2026-06-01): ``https://api.17track.net/track/v2.2/``,
header ``17token: <key>``, POST a JSON array of ``{"number","carrier"?}``,
<=40 numbers/call, 3 req/s. The live client uses stdlib ``urllib`` (no new dep)
and is untested in units (like the live Gmail client). The exact ``track_info``
field path + quota error codes are flagged for live confirmation on first poll.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass, field
from typing import Optional, Protocol

logger = logging.getLogger(__name__)

_BASE = "https://api.17track.net/track/v2.2"
MAX_PER_CALL = 40

# Normalized stages == 17track v2.2 ``latest_status.status`` values.
STAGE_NOT_FOUND = "NotFound"
STAGE_INFO_RECEIVED = "InfoReceived"
STAGE_IN_TRANSIT = "InTransit"
STAGE_OUT_FOR_DELIVERY = "OutForDelivery"
STAGE_AVAILABLE_FOR_PICKUP = "AvailableForPickup"
STAGE_DELIVERY_FAILURE = "DeliveryFailure"
STAGE_DELIVERED = "Delivered"
STAGE_EXCEPTION = "Exception"
STAGE_EXPIRED = "Expired"

# Reaching one of these stops polling the parcel (it won't progress further).
TERMINAL_STAGES = frozenset({STAGE_DELIVERED, STAGE_EXCEPTION, STAGE_EXPIRED})


@dataclass(frozen=True)
class TrackStatus:
    number: str
    stage: Optional[str]  # a normalized stage, or None when not yet known / not found
    sub_status: str = ""
    carrier: Optional[int] = None


@dataclass(frozen=True)
class RegisterResult:
    accepted: list = field(default_factory=list)  # tracking numbers 17track accepted
    rejected: dict = field(default_factory=dict)  # number -> reason
    quota_exhausted: bool = False


class Track17Client(Protocol):
    def register(self, numbers: list) -> RegisterResult: ...
    def get_statuses(self, numbers: list) -> list: ...


class FakeTrack17Client:
    """Scripted client for tests — no network.

    ``statuses`` maps number -> :class:`TrackStatus` (or a bare stage string).
    ``register`` accepts everything not in ``reject`` and records its calls.
    """

    def __init__(self, statuses=None, reject=None, quota_exhausted=False) -> None:
        self._statuses = dict(statuses or {})
        self._reject = set(reject or ())
        self._quota = quota_exhausted
        self.registered: list = []
        self.status_calls: list = []

    def register(self, numbers: list) -> RegisterResult:
        if self._quota:
            return RegisterResult(quota_exhausted=True)
        accepted, rejected = [], {}
        for n in numbers:
            if n in self._reject:
                rejected[n] = "rejected"
            else:
                accepted.append(n)
                self.registered.append(n)
        return RegisterResult(accepted=accepted, rejected=rejected)

    def set_status(self, number, stage, sub_status="", carrier=None) -> None:
        self._statuses[number] = TrackStatus(number, stage, sub_status, carrier)

    def get_statuses(self, numbers: list) -> list:
        self.status_calls.append(list(numbers))
        out = []
        for n in numbers:
            s = self._statuses.get(n)
            if isinstance(s, TrackStatus):
                out.append(s)
            elif isinstance(s, str):
                out.append(TrackStatus(n, s))
            else:
                out.append(TrackStatus(n, None))
        return out


class HttpTrack17Client:
    """Live client over the 17track v2.2 HTTP API (stdlib urllib). Not unit-tested."""

    def __init__(self, api_key: str, *, base: str = _BASE, timeout: float = 30.0) -> None:
        self._key = api_key
        self._base = base.rstrip("/")
        self._timeout = timeout

    def _post(self, path: str, payload: list) -> dict:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self._base}/{path}",
            data=data,
            headers={"Content-Type": "application/json", "17token": self._key},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310 - fixed host
            return json.loads(resp.read().decode() or "{}")

    def register(self, numbers: list) -> RegisterResult:
        if not numbers:
            return RegisterResult()
        resp = self._post("register", [{"number": n} for n in numbers[:MAX_PER_CALL]])
        data = resp.get("data") or {}
        accepted = [a.get("number") for a in (data.get("accepted") or []) if a.get("number")]
        rejected: dict = {}
        quota = False
        for r in data.get("rejected") or []:
            num = r.get("number", "")
            err = r.get("error") or {}
            msg = str(err.get("message", "rejected"))
            if "quota" in msg.lower() or "limit" in msg.lower():
                quota = True
            if num:
                rejected[num] = msg
        return RegisterResult(accepted=accepted, rejected=rejected, quota_exhausted=quota)

    def get_statuses(self, numbers: list) -> list:
        if not numbers:
            return []
        resp = self._post("gettrackinfo", [{"number": n} for n in numbers[:MAX_PER_CALL]])
        data = resp.get("data") or {}
        out = []
        for a in data.get("accepted") or []:
            info = a.get("track_info") or {}
            latest = info.get("latest_status") or {}
            out.append(
                TrackStatus(
                    number=a.get("number", ""),
                    stage=latest.get("status"),
                    sub_status=str(latest.get("sub_status", "") or ""),
                    carrier=a.get("carrier"),
                )
            )
        for r in data.get("rejected") or []:
            if r.get("number"):
                out.append(TrackStatus(number=r["number"], stage=None))
        return out
