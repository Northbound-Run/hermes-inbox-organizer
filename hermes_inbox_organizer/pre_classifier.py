"""Deterministic fast-path before the LLM — cheap, no API call.

Catches obvious automated/bulk mail so we skip the LLM on clear-cut cases.
List-header detection would need parse_message to surface those headers; for now
we key off the From address.
"""

from __future__ import annotations

import re

_NOREPLY_RE = re.compile(r"no[-_.]?reply|donotreply|do-not-reply|mailer-daemon", re.I)


def pre_classify(parsed: dict) -> "str | None":
    """Return a bare category name if a rule matches, else None (→ LLM)."""
    frm = (parsed.get("from") or "").lower()
    if _NOREPLY_RE.search(frm):
        return "Notification"
    return None
