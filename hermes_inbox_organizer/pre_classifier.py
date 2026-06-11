"""Deterministic fast-path before the LLM — cheap, no API call.

Catches obvious automated/bulk mail so we skip the LLM on clear-cut cases.
Bulk-marketing detection keys off the list headers ``parse_message`` surfaces
(List-Unsubscribe / List-Unsubscribe-Post / Precedence); the noreply From rule
remains the generic automated-mail fallback.
"""

from __future__ import annotations

import re

_NOREPLY_RE = re.compile(r"no[-_.]?reply|donotreply|do-not-reply|mailer-daemon", re.I)

# Unsubscribe affordances in visible text — the footer of most bulk mail (also
# matches inside unsubscribe URLs).
_UNSUB_BODY_RE = re.compile(
    r"unsubscribe|opt[ -]out|email preferences|manage (?:your )?preferences", re.I
)

_BULK_PRECEDENCE = {"bulk", "junk"}


def is_bulk_marketing(parsed: dict) -> bool:
    """True on deterministic bulk-sender header fingerprints.

    RFC 8058 one-click unsubscribe (List-Unsubscribe-Post) is required of bulk
    senders and absent from transactional mail, so it alone is decisive — as is
    List-Unsubscribe combined with a bulk/junk Precedence. A bare
    List-Unsubscribe is NOT: mailing lists and many notification senders (e.g.
    GitHub) set it on non-marketing mail, so that case only becomes an LLM hint
    (see :func:`has_unsubscribe_signal`).
    """
    if parsed.get("one_click_unsubscribe"):
        return True
    return bool(parsed.get("list_unsubscribe")) and (
        str(parsed.get("precedence") or "").lower() in _BULK_PRECEDENCE
    )


def has_unsubscribe_signal(parsed: dict) -> bool:
    """Weak bulk signal — an unsubscribe header, or unsubscribe text/link in the
    body. Only ever an LLM hint: body text alone must never hard-classify, since
    a human asking "how do I unsubscribe…?" is still To Respond.
    """
    if parsed.get("list_unsubscribe") or parsed.get("one_click_unsubscribe"):
        return True
    return bool(_UNSUB_BODY_RE.search(parsed.get("body") or parsed.get("snippet") or ""))


def pre_classify(parsed: dict) -> "str | None":
    """Return a bare category name if a rule matches, else None (→ LLM)."""
    # Before the noreply rule: a no-reply marketing blast is Marketing, not a
    # generic Notification.
    if is_bulk_marketing(parsed):
        return "Marketing"
    frm = (parsed.get("from") or "").lower()
    if _NOREPLY_RE.search(frm):
        return "Notification"
    return None
