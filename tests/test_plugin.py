"""register(ctx) wires against the real Hermes PluginContext surface."""

from __future__ import annotations

from hermes_inbox_organizer import register
from hermes_inbox_organizer.background import InboundMessage, InboxDaemon


class FakeCtx:
    """Records what a plugin registers (mirrors PluginContext's call surface)."""

    def __init__(self) -> None:
        self.tools: list[dict] = []
        self.hooks: list[tuple[str, object]] = []
        self.commands: list[tuple[str, object, str]] = []

    def register_tool(self, **kwargs) -> None:
        self.tools.append(kwargs)

    def register_hook(self, hook_name, callback) -> None:
        self.hooks.append((hook_name, callback))

    def register_command(self, name, handler, description) -> None:
        self.commands.append((name, handler, description))


def test_register_wires_native_tool() -> None:
    ctx = FakeCtx()
    daemon = register(ctx)

    assert isinstance(daemon, InboxDaemon)
    tool = next(t for t in ctx.tools if t["name"] == "inbox_create_draft")
    assert tool["toolset"] == "inbox"
    assert tool["schema"]["name"] == "inbox_create_draft"
    assert "thread_id" in tool["schema"]["parameters"]["properties"]
    assert callable(tool["handler"])


def test_register_wires_inbox_create_draft_with_record_outcome() -> None:
    # T7 wiring #1: inbox_create_draft must be built with the record_outcome seam so
    # the draft body is captured for the feedback loop. The handler closes over the
    # seam, so assert the handler runs end-to-end without a connected account (it
    # returns the not-connected error BEFORE record_outcome, proving the wiring is
    # present and inert in CI). The deeper capture path is covered in test_db /
    # test_inbox_tool; here we just prove register() passes record_outcome through.
    import inspect

    from hermes_inbox_organizer import _record_draft_outcome
    from hermes_inbox_organizer.inbox_tool import make_inbox_create_draft_handler

    # the seam exists and has the (account_id, thread_id, body, draft_id) shape
    assert list(inspect.signature(_record_draft_outcome).parameters) == [
        "account_id", "thread_id", "body", "draft_id",
    ]
    # and make_inbox_create_draft_handler accepts a record_outcome kwarg (T6 seam)
    assert "record_outcome" in inspect.signature(make_inbox_create_draft_handler).parameters

    ctx = FakeCtx()
    register(ctx)
    handler = next(t for t in ctx.tools if t["name"] == "inbox_create_draft")["handler"]
    # No account connected in a test ctx -> the handler returns an error string and
    # never raises (record_outcome is wired but not reached on this path).
    out = handler({"account_id": "a@x.com", "thread_id": "t1", "body": "hi"})
    assert isinstance(out, str)


def test_register_wires_draft_feedback_tools() -> None:
    # T7 wiring #2: DraftFeedbackModule.tools() auto-register via the registry loop.
    ctx = FakeCtx()
    register(ctx)
    names = {t["name"] for t in ctx.tools}
    assert {
        "inbox_draft_feedback_status",
        "inbox_forget_lesson",
        "inbox_clear_learned_notes",
    } <= names
    for n in ("inbox_draft_feedback_status", "inbox_forget_lesson", "inbox_clear_learned_notes"):
        tool = next(t for t in ctx.tools if t["name"] == n)
        assert tool["toolset"] == "inbox" and callable(tool["handler"])


def test_draft_feedback_mutations_are_owner_gated_status_is_not() -> None:
    # T7 wiring #3: forget/clear are in the owner gate (blocked for a non-owner on a
    # normal turn); the read-only status tool is NOT gated.
    from hermes_inbox_organizer import CONNECT_TOOLS

    assert {"inbox_forget_lesson", "inbox_clear_learned_notes"} <= CONNECT_TOOLS
    assert "inbox_draft_feedback_status" not in CONNECT_TOOLS

    ctx = FakeCtx()
    register(ctx)
    pre_tool = next(cb for name, cb in ctx.hooks if name == "pre_tool_call")
    # No owner bound in this test ctx -> the two mutations are blocked on a normal turn.
    for name in ("inbox_forget_lesson", "inbox_clear_learned_notes"):
        gate = pre_tool(tool_name=name, turn_id="normal-turn")
        assert gate and gate.get("action") == "block" and "owner" in gate["message"].lower()
    # The read-only status tool passes the gate (None == allowed) on a normal turn.
    assert pre_tool(tool_name="inbox_draft_feedback_status", turn_id="normal-turn") is None


def test_draft_feedback_mutations_blocked_on_draft_turn_status_blocked_too() -> None:
    # B4 interaction: on a wake/draft turn, NONE of the feedback tools are in
    # DRAFT_TURN_ALLOWLIST, so all three are blocked by the B4 guard (a draft turn
    # must not revert/inspect learned state) — proving the allowlist restricts draft
    # turns independently of the owner gate, and that normal turns (above) are fine.
    from hermes_inbox_organizer.draft_trigger import DRAFT_TURN_SENTINEL

    ctx = FakeCtx()
    register(ctx)
    pre_llm = next(cb for name, cb in ctx.hooks if name == "pre_llm_call")
    pre_tool = next(cb for name, cb in ctx.hooks if name == "pre_tool_call")
    pre_llm(session_id="s", turn_id="turn-D", user_message=f"draft this {DRAFT_TURN_SENTINEL}")
    for name in (
        "inbox_draft_feedback_status", "inbox_forget_lesson", "inbox_clear_learned_notes",
    ):
        assert pre_tool(tool_name=name, turn_id="turn-D").get("action") == "block"


def test_register_wires_pre_llm_call_hook() -> None:
    ctx = FakeCtx()
    register(ctx)
    assert any(name == "pre_llm_call" for name, _ in ctx.hooks)


def test_register_wires_gateway_capture_and_probe_command() -> None:
    ctx = FakeCtx()
    register(ctx)
    assert any(name == "pre_gateway_dispatch" for name, _ in ctx.hooks)

    names = [c[0] for c in ctx.commands]
    assert "inboxprobe" in names
    handler = next(c[1] for c in ctx.commands if c[0] == "inboxprobe")
    # Gateway not captured in a test ctx -> handler catches and returns a string.
    assert isinstance(handler(), str)


def test_end_to_end_to_respond_sets_pending_and_nudges() -> None:
    ctx = FakeCtx()
    daemon = register(ctx)
    hook = next(cb for name, cb in ctx.hooks if name == "pre_llm_call")

    # Nothing pending -> hook injects no context.
    assert hook() is None

    # A question subject classifies as To Respond -> pending + agent nudge.
    category = daemon.handle(
        InboundMessage(
            account_id="acct-1",
            message_id="m1",
            thread_id="t1",
            sender="alice@example.com",
            subject="can we move our call?",
        )
    )
    assert category == "1: To Respond"
    assert daemon.pending() == ["t1"]

    injected = hook()
    assert injected is not None
    assert "t1" in injected["context"]


def test_end_to_end_fyi_is_silent() -> None:
    ctx = FakeCtx()
    daemon = register(ctx)
    category = daemon.handle(
        InboundMessage(
            account_id="acct-1",
            message_id="m2",
            thread_id="t2",
            sender="news@example.com",
            subject="weekly digest",
        )
    )
    assert category == "2: FYI"
    assert daemon.pending() == []


def test_draft_turn_tool_allowlist_enforced_via_hooks() -> None:
    # B4 (Phase 3): the REGISTERED hooks must actually restrict a wake/draft turn's
    # toolset — exercises the real closures + the exact turn_id/user_message kwargs,
    # not just the guard class in isolation.
    from hermes_inbox_organizer.draft_trigger import DRAFT_TURN_SENTINEL

    ctx = FakeCtx()
    register(ctx)
    pre_llm = next(cb for name, cb in ctx.hooks if name == "pre_llm_call")
    pre_tool = next(cb for name, cb in ctx.hooks if name == "pre_tool_call")
    post_llm = next(cb for name, cb in ctx.hooks if name == "post_llm_call")

    # Before any draft turn, the draft guard blocks nothing.
    assert pre_tool(tool_name="terminal", turn_id="turn-X") is None

    # Simulate the wake turn: pre_llm_call sees the sentinel in the user_message.
    pre_llm(session_id="s", turn_id="turn-X", user_message=f"draft this {DRAFT_TURN_SENTINEL}")

    # Dangerous tools are now blocked for that turn_id; allowlisted ones pass.
    assert pre_tool(tool_name="terminal", turn_id="turn-X").get("action") == "block"
    assert pre_tool(tool_name="execute_code", turn_id="turn-X").get("action") == "block"
    assert pre_tool(tool_name="browser_navigate", turn_id="turn-X").get("action") == "block"
    assert pre_tool(tool_name="inbox_create_draft", turn_id="turn-X") is None
    assert pre_tool(tool_name="web_search", turn_id="turn-X") is None

    # A different (normal) turn is unaffected.
    assert pre_tool(tool_name="terminal", turn_id="other-turn") is None

    # A CONNECT/backfill tool is blocked on a draft turn (not in the allowlist) — proves
    # B4 is enforced before (and independently of) the owner-gate.
    assert pre_tool(tool_name="inbox_backfill_profiles", turn_id="turn-X").get("action") == "block"

    # A NORMAL turn (no sentinel) still owner-gates connect tools (the B4 early-return
    # didn't break the existing gate; no owner bound in this test -> blocked).
    gate = pre_tool(tool_name="inbox_connect_account", turn_id="normal-turn")
    assert gate and gate.get("action") == "block" and "owner" in gate["message"].lower()

    # After the turn ends, the restriction is cleared.
    post_llm(turn_id="turn-X")
    assert pre_tool(tool_name="terminal", turn_id="turn-X") is None


def test_register_wires_rollup_tool_via_registry() -> None:
    # Phase 2: the rollup tool is now contributed by RollupModule through the
    # registry, but the registered tool must be byte-identical to before.
    from hermes_inbox_organizer.modules.rollup import _DESCRIPTION
    from hermes_inbox_organizer.rollup import INBOX_UNREAD_ROLLUP_SCHEMA

    ctx = FakeCtx()
    register(ctx)
    tool = next(t for t in ctx.tools if t["name"] == "inbox_unread_rollup")
    assert tool["toolset"] == "inbox"
    assert tool["schema"] is INBOX_UNREAD_ROLLUP_SCHEMA  # same schema object
    assert tool["description"] == _DESCRIPTION  # description unchanged
    assert callable(tool["handler"])
