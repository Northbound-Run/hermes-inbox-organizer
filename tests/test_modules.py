"""Unit tests for the module contract + registry (no Hermes / no creds).

Dispatch is exercised both deterministically (InlineExecutor) and with the real
thread pool (offload + isolation), and classify-override precedence/validation
mirrors ``classifier.classify``'s rules.
"""

from __future__ import annotations

import threading
import time

from hermes_inbox_organizer.modules import (
    InboundEvent,
    InlineExecutor,
    Module,
    ModuleRegistry,
    PeriodicJob,
    SentEvent,
    ToolSpec,
)


def _inbound(**kw) -> InboundEvent:
    base = dict(
        account_id="a@x.com",
        message_id="m1",
        thread_id="t1",
        parsed={"from": "bob@x.com", "subject": "hi"},
        category="FYI",
    )
    base.update(kw)
    return InboundEvent(**base)


def _sent(**kw) -> SentEvent:
    base = dict(
        account_id="a@x.com",
        message_id="m1",
        thread_id="t1",
        parsed={"from": "me@x.com", "body": "thanks"},
        target_category="Actioned",
    )
    base.update(kw)
    return SentEvent(**base)


# ── classify (decision phase) ────────────────────────────────────────────────


def test_empty_registry_uses_default_classifier():
    reg = ModuleRegistry([], classify_fn=lambda parsed: "FYI", executor=InlineExecutor())
    assert reg.classify({"subject": "x"}) == "FYI"


def test_override_wins_over_default():
    class M(Module):
        name = "m"

        def classify_override(self, parsed):
            return "To Respond"

    reg = ModuleRegistry([M()], classify_fn=lambda parsed: "FYI", executor=InlineExecutor())
    assert reg.classify({}) == "To Respond"


def test_override_precedence_lowest_priority_number_first():
    class Early(Module):
        name = "early"
        priority = 10

        def classify_override(self, parsed):
            return "Marketing"

    class Late(Module):
        name = "late"
        priority = 20

        def classify_override(self, parsed):
            return "Notification"

    reg = ModuleRegistry(
        [Late(), Early()], classify_fn=lambda parsed: "FYI", executor=InlineExecutor()
    )
    assert reg.classify({}) == "Marketing"  # Early (priority 10) wins despite list order


def test_override_none_defers_to_next_then_default():
    class Abstain(Module):
        name = "abstain"
        priority = 1

        def classify_override(self, parsed):
            return None

    class Decide(Module):
        name = "decide"
        priority = 2

        def classify_override(self, parsed):
            return "Comment"

    reg = ModuleRegistry(
        [Abstain(), Decide()], classify_fn=lambda parsed: "FYI", executor=InlineExecutor()
    )
    assert reg.classify({}) == "Comment"


def test_override_unknown_category_rejected():
    class M(Module):
        name = "m"

        def classify_override(self, parsed):
            return "Totally Not A Category"

    reg = ModuleRegistry([M()], classify_fn=lambda parsed: "FYI", executor=InlineExecutor())
    assert reg.classify({}) == "FYI"  # invalid -> deferred to default


def test_override_sent_only_category_rejected():
    # Awaiting Reply / Actioned are sent-only — the classifier path must never
    # apply them to inbound mail, so an override returning one is rejected.
    class M(Module):
        name = "m"

        def classify_override(self, parsed):
            return "Actioned"

    reg = ModuleRegistry([M()], classify_fn=lambda parsed: "FYI", executor=InlineExecutor())
    assert reg.classify({}) == "FYI"


def test_override_numbered_name_resolves():
    class M(Module):
        name = "m"

        def classify_override(self, parsed):
            return "1: To Respond"  # numbered label form

    reg = ModuleRegistry([M()], classify_fn=lambda parsed: "FYI", executor=InlineExecutor())
    assert reg.classify({}) == "To Respond"  # normalized to the bare name


def test_override_exception_is_isolated_and_defers():
    class Boom(Module):
        name = "boom"
        priority = 1

        def classify_override(self, parsed):
            raise RuntimeError("kaboom")

    reg = ModuleRegistry([Boom()], classify_fn=lambda parsed: "FYI", executor=InlineExecutor())
    assert reg.classify({}) == "FYI"  # crash swallowed -> default


# ── dispatch (notification phase) ─────────────────────────────────────────────


def test_dispatch_inbound_fires_each_enabled_module_once():
    calls: list[str] = []

    class A(Module):
        name = "a"

        def on_inbound(self, event):
            calls.append(f"a:{event.message_id}")

    class B(Module):
        name = "b"

        def on_inbound(self, event):
            calls.append(f"b:{event.message_id}")

    reg = ModuleRegistry([A(), B()], executor=InlineExecutor())
    reg.dispatch_inbound(_inbound(message_id="m9"))
    assert sorted(calls) == ["a:m9", "b:m9"]


def test_dispatch_sent_fires_observers():
    seen: list[str] = []

    class M(Module):
        name = "m"

        def on_sent(self, event):
            seen.append(event.target_category)

    reg = ModuleRegistry([M()], executor=InlineExecutor())
    reg.dispatch_sent(_sent(target_category="Awaiting Reply"))
    assert seen == ["Awaiting Reply"]


def test_dispatch_isolates_module_exceptions():
    seen: list[str] = []

    class Boom(Module):
        name = "boom"

        def on_inbound(self, event):
            raise RuntimeError("nope")

    class Good(Module):
        name = "good"

        def on_inbound(self, event):
            seen.append("ran")

    reg = ModuleRegistry([Boom(), Good()], executor=InlineExecutor())
    reg.dispatch_inbound(_inbound())  # must not raise
    assert seen == ["ran"]  # Good still ran despite Boom raising


def test_dispatch_offloads_slow_module_without_blocking():
    started = threading.Event()
    finished = threading.Event()

    class Slow(Module):
        name = "slow"

        def on_inbound(self, event):
            started.set()
            time.sleep(0.6)
            finished.set()

    reg = ModuleRegistry([Slow()])  # real ThreadPoolExecutor
    try:
        t0 = time.monotonic()
        reg.dispatch_inbound(_inbound())
        elapsed = time.monotonic() - t0
        assert elapsed < 0.2  # returned without waiting for the 0.6s module
        assert started.wait(timeout=2)
        assert finished.wait(timeout=2)  # but it did run to completion off-thread
    finally:
        reg.shutdown()


def test_disabled_module_is_skipped():
    calls: list[str] = []

    class Off(Module):
        name = "off"

        @property
        def enabled(self):
            return False

        def classify_override(self, parsed):
            return "To Respond"

        def on_inbound(self, event):
            calls.append("ran")

    reg = ModuleRegistry([Off()], classify_fn=lambda parsed: "FYI", executor=InlineExecutor())
    assert reg.modules == []
    assert reg.classify({}) == "FYI"  # disabled override ignored
    reg.dispatch_inbound(_inbound())
    assert calls == []  # disabled observer not called


# ── contributions ─────────────────────────────────────────────────────────────


def test_tools_and_periodic_aggregate():
    spec = ToolSpec(name="t", schema={"name": "t"}, handler=lambda **kw: "ok")
    job = PeriodicJob(name="j", interval_s=60.0, run_once=lambda: None)

    class M(Module):
        name = "m"

        def tools(self):
            return [spec]

        def periodic(self):
            return [job]

    reg = ModuleRegistry([M(), Module()], executor=InlineExecutor())
    assert reg.tools() == [spec]  # bare Module() contributes nothing
    assert reg.periodic() == [job]
