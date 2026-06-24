"""2FA / login-code notifier module (Phase 3).

Observes inbound mail; when a message looks like a login/verification-code email
it extracts the code and pushes it to the owner immediately via the Notifier, so
the code lands in chat without opening the email. Wired as an OBSERVER (not a
``classify_override``): 2FA mail is already labelled Notification by the
classifier — the value here is the push, not the label.

Security posture:

* Detection is **deterministic regex (NO LLM in the path)** over length-bounded,
  linear-time patterns — an attacker-crafted subject/body can't ReDoS the worker
  (S3).
* The push contains ONLY the extracted code + the bare sender *address* — never
  the attacker-controlled display name or surrounding body (S1). The owner is
  told to use it only if they just signed in.
* The code is **NEVER logged or persisted**. Dedup keys on ``message_id``
  (``db.note_once``), never the code value (S2), so a Pub/Sub redelivery or the
  poll reconciler re-draining the same message can't double-push.
* Optional sender allowlist (``INBOX_2FA_SENDER_ALLOWLIST``); empty = push all.

``on_inbound`` runs offloaded on a registry worker thread, so it opens its OWN
short-lived DB connection (the drain's connection is single-threaded).
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Callable
from typing import Any

from .. import db
from .base import InboundEvent, Module

logger = logging.getLogger(__name__)

_MAX_SUBJECT = 200
_MAX_BODY = 2000

# Ordered, precision-first patterns. All linear-time + bounded (no ReDoS): the
# code must sit next to a code cue, or be a distinctive Google ``G-`` code.
# The code's trailing edge uses ``(?!\d)`` rather than ``\b`` so a code glued to
# the next word — common when HTML->text strips whitespace, e.g. IKEA's
# "one-time code:123456Please" — still matches, while still refusing to slice a
# longer digit run. The leading edge uses ``(?<!\d)`` for the same reason.
_CODE_PATTERNS = (
    # "...code is 123456", "code: 123456", "OTP 4821", "passcode (1234)",
    # "one-time code:123456Please", "code: G-123456"
    re.compile(
        r"\b(?:code|passcode|otp|pin|one[\s-]?time password)\b(?:\s+is)?\s*[:=]?\s*\(?"
        r"(G-\d{4,8}|\d{3}[\s-]\d{3}|\d{4,8})(?!\d)",
        re.IGNORECASE,
    ),
    # "123456 is your verification code"
    re.compile(
        r"(?<!\d)(G-\d{4,8}|\d{4,8})(?!\d)\s+is\s+your\b[^.\n]{0,30}?\b(?:code|passcode|otp|pin)\b",
        re.IGNORECASE,
    ),
    # Distinctive Google style anywhere, even without an adjacent cue word.
    re.compile(r"\b(G-\d{4,8})(?!\d)"),
)

_ANGLE_ADDR_RE = re.compile(r"<([^>]+)>")


def detect_code(parsed: dict) -> str | None:
    """Return the login/verification code in this email, or None.

    Precision-first: requires the code to sit next to a code cue (or be a
    distinctive Google ``G-`` code). Input is length-bounded so a crafted message
    can't blow up the regex.
    """
    subject = (parsed.get("subject") or "")[:_MAX_SUBJECT]
    body = (parsed.get("body") or parsed.get("snippet") or "")[:_MAX_BODY]
    haystack = f"{subject}\n{body}"
    for pat in _CODE_PATTERNS:
        m = pat.search(haystack)
        if m:
            return m.group(1)
    return None


def _sender_email(from_header: str) -> str:
    """Bare email address from a From header (drops the attacker-set display name).

    Picks the LAST ``<...>`` that contains an ``@`` — a crafted display name may
    embed decoy angle brackets (e.g. ``"Evil <x>" <attacker@bad.com>``), and the
    real address is the trailing one.
    """
    raw = (from_header or "").strip()
    angle = [a.strip() for a in _ANGLE_ADDR_RE.findall(raw) if "@" in a]
    if angle:
        return angle[-1][:120]
    return raw[:120]


class TwoFactorModule(Module):
    """Observer that pushes detected login codes to the owner."""

    name = "twofa"

    def __init__(
        self,
        *,
        notifier: Any,
        enabled: bool = True,
        sender_allowlist: frozenset | None = None,
        db_connect: Callable[[], Any] | None = None,
    ) -> None:
        self._notifier = notifier
        self._enabled = enabled
        self._allowlist = frozenset(sender_allowlist or ())
        self._db_connect = db_connect or db.connect

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _sender_allowed(self, sender: str) -> bool:
        if not self._allowlist:
            return True  # empty allowlist = push all (the low-friction default)
        low = sender.lower()
        return any(
            low == a or low.endswith("@" + a) or low.endswith("." + a) for a in self._allowlist
        )

    def on_inbound(self, event: InboundEvent) -> None:
        code = detect_code(event.parsed)
        if code is None:
            return
        sender = _sender_email(event.parsed.get("from", ""))
        if not self._sender_allowed(sender):
            logger.info(
                "inbox 2fa: code from %s not on allowlist; labelled only (message %s)",
                sender,
                event.message_id,
            )
            return
        # Atomic dedup GATE before the push (redelivery/poll re-drain safe):
        # keyed on message_id, NEVER the code (S2). Best-effort — a failed push is
        # not retried (a stale code is noise; B3), so the gate runs first.
        with contextlib.closing(self._db_connect()) as conn:
            if not db.note_once(conn, self.name, event.account_id, event.message_id):
                return
        ok = self._notifier.send(
            f"\U0001f511 Login code {code} — from {sender}. Only use it if you just signed in.",
            urgent=True,
        )
        # NEVER log the code itself — only the message id.
        if ok:
            logger.info("inbox 2fa: pushed a login code for message %s", event.message_id)
        else:
            logger.warning("inbox 2fa: push failed for message %s", event.message_id)
