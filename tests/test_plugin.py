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
