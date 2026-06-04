"""Unit tests for the Notifier seam (Phase 0 de-risk of DeliveryRouter).

No Hermes / no creds: the live ``gateway`` runner, its async ``delivery_router``,
and its event loop are all behind seams (``get_gateway``, ``resolve_target``,
``run_on_loop``). For ``_resolve_target`` we install minimal fake ``gateway.*``
modules so the Matrix-id/parse-format collision logic is exercised directly.
"""

from __future__ import annotations

import enum
import sys
import types
from dataclasses import dataclass
from typing import Any, Optional

from hermes_inbox_organizer.notifier import (
    DeliveryNotifier,
    FakeNotifier,
    _delivery_ok,
    _resolve_home_target,
    _resolve_target,
)


# ── Fakes for the captured GatewayRunner + its DeliveryRouter ────────────────


class _FakeRouter:
    def __init__(self, result: Optional[dict] = None) -> None:
        self.calls: list[tuple[str, list]] = []
        self._result = result if result is not None else {"x": {"success": True}}

    def deliver(self, content: str, targets: list) -> dict:
        # Plain (non-async) in the fake: the injected run_on_loop passes its
        # return value straight through, so no event loop is involved.
        self.calls.append((content, targets))
        return self._result


class _FakeGateway:
    def __init__(self, result: Optional[dict] = None) -> None:
        self.delivery_router = _FakeRouter(result)
        self._gateway_loop = object()


def _passthrough(coro: Any, gateway: Any, timeout: float) -> Any:
    """Stand-in for run_coroutine_threadsafe(...).result(): return deliver()'s value."""
    return coro


# ── FakeNotifier ─────────────────────────────────────────────────────────────


def test_fake_notifier_records_and_returns_true():
    n = FakeNotifier()
    assert n.send("hi", urgent=True) is True
    assert n.send("bye") is True
    assert n.sent == [
        {"text": "hi", "urgent": True},
        {"text": "bye", "urgent": False},
    ]


# ── DeliveryNotifier ──────────────────────────────────────────────────────────


def test_delivery_notifier_delivers_to_resolved_target():
    gw = _FakeGateway()
    n = DeliveryNotifier(
        get_gateway=lambda: gw,
        target="!room:srv",
        resolve_target=lambda raw: {"target": raw},
        run_on_loop=_passthrough,
    )
    assert n.send("hello", urgent=True) is True
    assert gw.delivery_router.calls == [("hello", [{"target": "!room:srv"}])]


def test_delivery_notifier_returns_false_when_gateway_uncaptured():
    # B2: a daemon push before any owner turn has been captured.
    gw = _FakeGateway()
    n = DeliveryNotifier(
        get_gateway=lambda: None,
        target="!room:srv",
        resolve_target=lambda raw: {"target": raw},
        run_on_loop=_passthrough,
    )
    assert n.send("x") is False
    assert gw.delivery_router.calls == []


def test_delivery_notifier_falls_back_to_home_channel():
    # No explicit target -> resolve Hermes's /sethome home channel.
    gw = _FakeGateway()
    n = DeliveryNotifier(
        get_gateway=lambda: gw,
        target=None,
        resolve_home=lambda gateway, source, default_platform: {"home": True},
        run_on_loop=_passthrough,
    )
    assert n.send("hello") is True
    assert gw.delivery_router.calls == [("hello", [{"home": True}])]


def test_delivery_notifier_override_beats_home():
    # Explicit INBOX_NOTIFY_TARGET wins; home resolver must not be consulted.
    gw = _FakeGateway()

    def _home_boom(gateway, source, default_platform):
        raise AssertionError("home resolver should not be called when target is set")

    n = DeliveryNotifier(
        get_gateway=lambda: gw,
        target="!room:srv",
        resolve_target=lambda raw: {"target": raw},
        resolve_home=_home_boom,
        run_on_loop=_passthrough,
    )
    assert n.send("hello") is True
    assert gw.delivery_router.calls == [("hello", [{"target": "!room:srv"}])]


def test_delivery_notifier_returns_false_when_no_destination():
    # No override and no home configured (owner never ran /sethome).
    gw = _FakeGateway()
    n = DeliveryNotifier(
        get_gateway=lambda: gw,
        target=None,
        resolve_home=lambda gateway, source, default_platform: None,
        run_on_loop=_passthrough,
    )
    assert n.send("x") is False
    assert gw.delivery_router.calls == []


def test_delivery_notifier_swallows_exceptions():
    gw = _FakeGateway()

    def _boom(coro: Any, gateway: Any, timeout: float) -> Any:
        raise RuntimeError("event loop down")

    n = DeliveryNotifier(
        get_gateway=lambda: gw,
        target="!room:srv",
        resolve_target=lambda raw: {"target": raw},
        run_on_loop=_boom,
    )
    assert n.send("x") is False  # never raises out of send()


def test_delivery_notifier_reports_delivery_failure_as_false():
    gw = _FakeGateway(result={"!room:srv": {"success": False, "error": "no adapter"}})
    n = DeliveryNotifier(
        get_gateway=lambda: gw,
        target="!room:srv",
        resolve_target=lambda raw: {"target": raw},
        run_on_loop=_passthrough,
    )
    assert n.send("x") is False


def test_delivery_notifier_returns_false_on_unresolvable_target():
    gw = _FakeGateway()
    n = DeliveryNotifier(
        get_gateway=lambda: gw,
        target="garbage",
        resolve_target=lambda raw: None,  # resolver couldn't build a target
        run_on_loop=_passthrough,
    )
    assert n.send("x") is False
    assert gw.delivery_router.calls == []


# ── _delivery_ok parsing ──────────────────────────────────────────────────────


def test_delivery_ok_parsing():
    assert _delivery_ok({"a": {"success": True}}) is True
    assert _delivery_ok({"a": {"success": True}, "b": {"success": False}}) is True
    assert _delivery_ok({"a": {"success": False}}) is False
    assert _delivery_ok({}) is False
    assert _delivery_ok(None) is False
    assert _delivery_ok("nope") is False


# ── _resolve_target (Matrix-id / parse-format collision) ──────────────────────


def _install_fake_gateway(monkeypatch):
    """Install minimal fake ``gateway.delivery`` + ``gateway.config`` modules."""

    class Platform(enum.Enum):
        MATRIX = "matrix"
        TELEGRAM = "telegram"
        LOCAL = "local"

    @dataclass
    class DeliveryTarget:
        platform: Any
        chat_id: Optional[str] = None
        thread_id: Optional[str] = None
        is_origin: bool = False
        is_explicit: bool = False

        @classmethod
        def parse(cls, target: str, origin=None) -> "DeliveryTarget":
            parts = target.split(":", 2)
            return cls(
                platform=Platform(parts[0].lower()),
                chat_id=parts[1] if len(parts) > 1 else None,
                thread_id=parts[2] if len(parts) > 2 else None,
                is_explicit=True,
            )

    gateway_pkg = types.ModuleType("gateway")
    delivery_mod = types.ModuleType("gateway.delivery")
    config_mod = types.ModuleType("gateway.config")
    delivery_mod.DeliveryTarget = DeliveryTarget
    config_mod.Platform = Platform
    monkeypatch.setitem(sys.modules, "gateway", gateway_pkg)
    monkeypatch.setitem(sys.modules, "gateway.delivery", delivery_mod)
    monkeypatch.setitem(sys.modules, "gateway.config", config_mod)
    return Platform, DeliveryTarget


def test_resolve_target_matrix_bare_room_id(monkeypatch):
    Platform, _ = _install_fake_gateway(monkeypatch)
    t = _resolve_target("!abcd:matrix.org")
    assert t.platform is Platform.MATRIX
    assert t.chat_id == "!abcd:matrix.org"  # colon in id preserved, NOT split
    assert t.thread_id is None


def test_resolve_target_matrix_prefixed(monkeypatch):
    Platform, _ = _install_fake_gateway(monkeypatch)
    t = _resolve_target("matrix:!abcd:matrix.org")
    assert t.platform is Platform.MATRIX
    assert t.chat_id == "!abcd:matrix.org"  # prefix stripped, rest kept whole
    assert t.thread_id is None


def test_resolve_target_alias_hash(monkeypatch):
    Platform, _ = _install_fake_gateway(monkeypatch)
    t = _resolve_target("#room:server")
    assert t.platform is Platform.MATRIX
    assert t.chat_id == "#room:server"


def test_resolve_target_telegram_uses_parse(monkeypatch):
    Platform, _ = _install_fake_gateway(monkeypatch)
    t = _resolve_target("telegram:123456")
    assert t.platform is Platform.TELEGRAM
    assert t.chat_id == "123456"


def test_resolve_target_bare_id_defaults_matrix(monkeypatch):
    Platform, _ = _install_fake_gateway(monkeypatch)
    t = _resolve_target("someroomid")
    assert t.platform is Platform.MATRIX
    assert t.chat_id == "someroomid"


def test_resolve_target_blank_is_none(monkeypatch):
    _install_fake_gateway(monkeypatch)
    assert _resolve_target("") is None
    assert _resolve_target("   ") is None


# ── _resolve_home_target (reuse Hermes's /sethome home channel) ───────────────


class _FakeHome:
    def __init__(self, chat_id: str, thread_id: Optional[str] = None) -> None:
        self.chat_id = chat_id
        self.thread_id = thread_id


class _FakeConfig:
    def __init__(self, home_by_platform: dict) -> None:
        self._home = home_by_platform

    def get_home_channel(self, platform):
        return self._home.get(platform)


class _FakeGatewayWithConfig:
    def __init__(self, config) -> None:
        self.config = config


def test_resolve_home_target_uses_source_platform(monkeypatch):
    Platform, DeliveryTarget = _install_fake_gateway(monkeypatch)
    gw = _FakeGatewayWithConfig(
        _FakeConfig({Platform.MATRIX: _FakeHome("!home:srv", thread_id="topic1")})
    )
    src = types.SimpleNamespace(platform=Platform.MATRIX)
    t = _resolve_home_target(gw, src, "matrix")
    assert isinstance(t, DeliveryTarget)
    assert t.platform is Platform.MATRIX
    assert t.chat_id == "!home:srv"
    assert t.thread_id == "topic1"


def test_resolve_home_target_default_platform_when_no_source(monkeypatch):
    Platform, _ = _install_fake_gateway(monkeypatch)
    gw = _FakeGatewayWithConfig(_FakeConfig({Platform.MATRIX: _FakeHome("!home:srv")}))
    t = _resolve_home_target(gw, None, "matrix")  # no source -> default platform
    assert t.platform is Platform.MATRIX
    assert t.chat_id == "!home:srv"
    assert t.thread_id is None


def test_resolve_home_target_none_when_home_unset(monkeypatch):
    Platform, _ = _install_fake_gateway(monkeypatch)
    gw = _FakeGatewayWithConfig(_FakeConfig({Platform.MATRIX: None}))  # never /sethome'd
    assert _resolve_home_target(gw, None, "matrix") is None


def test_resolve_home_target_none_when_gateway_has_no_config(monkeypatch):
    _install_fake_gateway(monkeypatch)
    assert _resolve_home_target(types.SimpleNamespace(), None, "matrix") is None


# ── /inboxnotifyprobe command handler signature ──────────────────────────────


def test_notify_probe_command_accepts_positional_arg():
    # The gateway dispatches plugin slash commands as ``plugin_handler(user_args)``
    # — ONE positional str (gateway/run.py) — and awaits the result if it's a
    # coroutine. The handler is async (to avoid deadlocking the gateway loop) and
    # must accept the positional arg. Regression guard for both the signature bug
    # ("Unknown command") and the loop-deadlock (TimeoutError) bugs.
    import asyncio

    from hermes_inbox_organizer import _make_notify_probe_command

    handler = _make_notify_probe_command(FakeNotifier())
    result = asyncio.run(handler(""))  # must NOT raise
    assert "delivered" in result.lower()
    # Also callable with trailing args, like a real "/inboxnotifyprobe foo".
    assert "delivered" in asyncio.run(handler("foo bar")).lower()
