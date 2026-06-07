"""_DraftTurnGuard: the B4 pre_tool_call allowlist for autonomous draft turns (AC20/AC21)."""

from __future__ import annotations

from hermes_inbox_organizer import DRAFT_TURN_ALLOWLIST, _DraftTurnGuard
from hermes_inbox_organizer.draft_trigger import DRAFT_TURN_SENTINEL


def _guard() -> _DraftTurnGuard:
    return _DraftTurnGuard(DRAFT_TURN_ALLOWLIST)


def test_blocks_dangerous_tools_during_draft_turn() -> None:
    g = _guard()
    g.note("turn-1", f"draft this email... {DRAFT_TURN_SENTINEL}")
    for tool in ("terminal", "execute_code", "browser_navigate", "read_file",
                 "write_file", "delegate_task", "cronjob", "send_message", "memory"):
        assert g.blocked("turn-1", tool) is True, tool


def test_allows_inbox_and_research_tools_during_draft_turn() -> None:
    g = _guard()
    g.note("turn-1", DRAFT_TURN_SENTINEL)
    for tool in ("inbox_get_thread", "inbox_list_emails", "inbox_get_sender_profile",
                 "inbox_save_sender_profile", "inbox_create_draft",
                 "web_search", "web_extract", "session_search"):
        assert g.blocked("turn-1", tool) is False, tool


def test_no_restriction_outside_a_draft_turn() -> None:
    g = _guard()
    # a turn whose user_message lacks the sentinel is a normal turn — unrestricted
    g.note("turn-normal", "hey, run a terminal command for me please")
    assert g.blocked("turn-normal", "terminal") is False
    # an unknown turn id is unrestricted
    assert g.blocked("turn-unknown", "terminal") is False


def test_clear_ends_the_restriction() -> None:
    g = _guard()
    g.note("turn-1", DRAFT_TURN_SENTINEL)
    assert g.blocked("turn-1", "terminal") is True
    g.clear("turn-1")  # post_llm_call fired
    assert g.blocked("turn-1", "terminal") is False


def test_note_ignores_missing_turn_message_or_sentinel() -> None:
    g = _guard()
    g.note(None, DRAFT_TURN_SENTINEL)   # no turn_id
    g.note("t", "no sentinel here")     # no sentinel
    g.note("t", None)                   # no message
    assert g.blocked("t", "terminal") is False


def test_fifo_cap_bounds_memory_without_false_blocks() -> None:
    g = _guard()
    for i in range(_DraftTurnGuard._MAX + 50):
        g.note(f"t{i}", DRAFT_TURN_SENTINEL)
    # oldest entries evicted (no longer restricted); the most recent still restricted.
    # turn_ids are unique, so an evicted (already-completed) id never causes a false block.
    assert g.blocked("t0", "terminal") is False
    assert g.blocked(f"t{_DraftTurnGuard._MAX + 49}", "terminal") is True
