"""Proactive outbound push to the owner — the ``Notifier`` seam.

Modules (2FA codes, shipping updates) need to message the owner *directly* —
WITHOUT inducing a multi-minute agent draft turn (that's what ``draft_trigger``
does, and it's the wrong tool for a one-line "your code is 123456"). Hermes's
gateway already owns a proactive-delivery primitive (the same one cron-job
outputs and gateway startup notifications use): ``DeliveryRouter.deliver``.

We reach it via the SAME captured ``GatewayRunner`` the draft trigger uses:
Hermes hands the runner to plugins as the ``gateway=`` kwarg of the
``pre_gateway_dispatch`` hook (never at register time), and ``_GatewayCapture``
in ``__init__`` grabs it on the first inbound turn. The runner exposes
``delivery_router`` + ``config`` and stores its own event loop, so from our
daemon thread we schedule the async ``deliver`` onto that loop with
``run_coroutine_threadsafe`` — which runs ``adapter.send`` on the loop its
network client is bound to.

GROUNDED against ``NousResearch/hermes-agent`` @ main (cloned 2026-06-04; line
numbers may drift, the shape is what matters):

  * ``gateway/run.py:1863``  ``self.delivery_router = DeliveryRouter(self.config)``
  * ``gateway/run.py:4249``  ``self._gateway_loop = asyncio.get_running_loop()``
  * ``gateway/run.py:4864``  ``home = self.config.get_home_channel(platform)`` (the canonical "send to home" pattern)
  * ``gateway/run.py:7407``  ``_invoke_hook("pre_gateway_dispatch", …, gateway=self, …)``
  * ``gateway/delivery.py:195``  ``async def deliver(content, targets, …) -> dict``
  * ``gateway/delivery.py:94``   ``DeliveryTarget(platform, chat_id, thread_id, …)``
  * ``gateway/config.py:202``   ``HomeChannel(platform, chat_id, name, thread_id)``
  * ``gateway/config.py:556``   ``GatewayConfig.get_home_channel(platform) -> HomeChannel | None``
  * ``gateway/config.py:115``   ``Platform.MATRIX = "matrix"``

Destination resolution (what the Phase-0 spike settled):

1. The delivery destination is a **chat_id (the ROOM)**, NOT the sender's
   ``user_id``. By DEFAULT we reuse Hermes's own **home channel** — the chat the
   owner designated with the native ``/sethome`` command, resolved through
   ``gateway.config.get_home_channel(platform)`` (the same destination Hermes
   uses for its startup/cron notifications). So once ``/sethome`` has been run,
   no extra inbox config is needed. The platform is the one the owner talks to
   Hermes on (the captured session ``source``), falling back to
   ``default_platform`` ("matrix") before any turn is captured.
2. ``INBOX_NOTIFY_TARGET`` is an OPTIONAL explicit override (a room id, e.g.
   ``!abcd:server`` — or ``telegram:123`` for another platform), useful for
   testing or sending somewhere other than home.
3. Matrix room/alias ids contain ``:`` (``!opaque:homeserver``), which COLLIDES
   with ``DeliveryTarget.parse``'s ``platform:chat_id:thread_id`` format — so the
   override path builds the target directly for Matrix.

Everything live sits behind seams (``get_gateway``, ``get_source``,
``resolve_target``, ``resolve_home``, ``run_on_loop``) so the seam — and the
modules that use it — are unit-tested without Hermes, mirroring
``draft_trigger``'s ``FakeDispatcher``/``NoopPoster``.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Optional, Protocol

logger = logging.getLogger(__name__)

# A direct push is interactive-fast; bound the wait so a wedged loop can never
# hang a daemon-thread caller (a stale 2FA code is useless anyway — see B3).
DEFAULT_TIMEOUT_S = 20.0

# Platform assumed for the home channel before a session source is captured.
DEFAULT_PLATFORM = "matrix"


class Notifier(Protocol):
    """Push one plain-text message to the owner. Returns True on delivery.

    ``urgent`` is reserved for the per-notification-class failure policy (2FA =
    best-effort + short retry then drop; shipping = retry-via-state) added with
    the modules; today it is informational and never raises out of ``send``.
    """

    def send(self, text: str, *, urgent: bool = False) -> bool: ...


class FakeNotifier:
    """Records sends instead of touching Hermes (tests / unconfigured)."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, text: str, *, urgent: bool = False) -> bool:
        self.sent.append({"text": text, "urgent": urgent})
        return True

    def send_with_detail(self, text: str) -> tuple[bool, dict]:
        self.sent.append({"text": text, "urgent": False})
        return True, {"fake": True}


def _resolve_target(raw: str) -> Any:
    """Explicit ``INBOX_NOTIFY_TARGET`` -> a gateway ``DeliveryTarget`` (live import).

    Robust to the Matrix-id/parse-format collision (see module docstring):

    * ``!room:server`` / ``#alias:server`` -> Matrix target, id kept verbatim.
    * ``matrix:!room:server``              -> Matrix target, ``matrix:`` stripped.
    * ``telegram:123``                     -> delegated to ``DeliveryTarget.parse``.
    * bare ``anything-else``               -> assumed a Matrix room id.
    """
    from gateway.config import Platform
    from gateway.delivery import DeliveryTarget

    raw = (raw or "").strip()
    if not raw:
        return None
    # Matrix room (!) / alias (#) ids embed ':' — never feed them to parse().
    if raw[0] in "!#":
        return DeliveryTarget(platform=Platform.MATRIX, chat_id=raw, is_explicit=True)
    head = raw.split(":", 1)[0].lower()
    known = {p.value for p in Platform}
    if ":" in raw and head in known:
        if head == "matrix":  # strip prefix; keep the (colon-bearing) id intact
            return DeliveryTarget(
                platform=Platform.MATRIX, chat_id=raw.split(":", 1)[1], is_explicit=True
            )
        return DeliveryTarget.parse(raw)
    return DeliveryTarget(platform=Platform.MATRIX, chat_id=raw, is_explicit=True)


def _resolve_home_target(gateway: Any, source: Any, default_platform: str) -> Any:
    """Reuse Hermes's configured home channel (set via ``/sethome``) as the target.

    Resolves ``gateway.config.get_home_channel(platform)`` — where ``platform`` is
    the one the owner talks to Hermes on (the captured session ``source``), or
    ``default_platform`` before any turn is captured. Returns a ``DeliveryTarget``
    on the home chat (carrying its thread/topic id), or ``None`` when no home is
    configured (the owner hasn't run ``/sethome``). Live-only import.
    """
    from gateway.config import Platform
    from gateway.delivery import DeliveryTarget

    platform = getattr(source, "platform", None)
    if not isinstance(platform, Platform):
        try:
            platform = Platform(default_platform)
        except ValueError:
            return None
    config = getattr(gateway, "config", None)
    get_home = getattr(config, "get_home_channel", None)
    if get_home is None:
        return None
    home = get_home(platform)
    if not home or not getattr(home, "chat_id", None):
        return None
    return DeliveryTarget(
        platform=platform,
        chat_id=home.chat_id,
        thread_id=getattr(home, "thread_id", None),
    )


def _run_on_gateway_loop(coro: Awaitable[Any], gateway: Any, timeout: float) -> Any:
    """Run an awaitable on the runner's OWN loop from our (non-loop) daemon thread.

    ``gateway._gateway_loop`` is the loop the Matrix adapter's client lives on,
    so ``deliver``/``adapter.send`` must run there. ``run_coroutine_threadsafe``
    is the correct cross-thread bridge (and is only safe from a thread that is
    NOT the loop's own — which a daemon thread is). Falls back to the plugin's
    private background loop if the runner ever lacks ``_gateway_loop``.
    """
    import asyncio

    loop = getattr(gateway, "_gateway_loop", None)
    if loop is None:
        logger.warning("inbox notifier: gateway has no _gateway_loop; using background loop")
        from ._background_loop import get_background_loop

        return get_background_loop().run_coro_sync(coro, timeout=timeout)
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)


def _delivery_ok(results: Any) -> bool:
    """``DeliveryRouter.deliver`` returns ``{target_str: {"success": bool, …}}``.

    True iff at least one target reports success (we send to exactly one, so this
    is "it was delivered"). Defensive against an unexpected shape.
    """
    if not isinstance(results, dict) or not results:
        return False
    return any(isinstance(v, dict) and v.get("success") for v in results.values())


def _target_repr(target: Any) -> str:
    """Compact, log-safe repr of a DeliveryTarget (or any stand-in) for diagnostics."""
    platform = getattr(getattr(target, "platform", None), "value", getattr(target, "platform", "?"))
    return f"{platform}:{getattr(target, 'chat_id', None)} thread={getattr(target, 'thread_id', None)}"


def _redact(detail: dict) -> dict:
    """Drop the (large) traceback before logging the detail dict."""
    return {k: v for k, v in detail.items() if k != "traceback"}


class DeliveryNotifier:
    """Push to the owner via the captured ``GatewayRunner``'s ``DeliveryRouter``.

    Destination = the explicit ``target`` (``INBOX_NOTIFY_TARGET``) if set, else
    Hermes's home channel (``/sethome``). ``get_gateway``/``get_source`` are the
    lazy getters shared with the draft dispatcher's ``_GatewayCapture`` (populated
    on the first ``pre_gateway_dispatch``). ``resolve_target``/``resolve_home``/
    ``run_on_loop`` are seams so the whole path is unit-tested without Hermes.

    ``send`` NEVER raises (Notifier contract) — it logs and returns False when
    the gateway isn't captured yet (B2: a boot-time push before any owner turn),
    no destination resolves (no ``/sethome`` and no override), or delivery
    errors/reports failure.
    """

    def __init__(
        self,
        *,
        get_gateway: Callable[[], Any],
        target: Optional[str] = None,
        get_source: Callable[[], Any] = lambda: None,
        default_platform: str = DEFAULT_PLATFORM,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        resolve_target: Callable[[str], Any] = _resolve_target,
        resolve_home: Callable[[Any, Any, str], Any] = _resolve_home_target,
        run_on_loop: Callable[[Awaitable[Any], Any, float], Any] = _run_on_gateway_loop,
    ) -> None:
        self._get_gateway = get_gateway
        self._target = target
        self._get_source = get_source
        self._default_platform = default_platform
        self._timeout = timeout_s
        self._resolve_target = resolve_target
        self._resolve_home = resolve_home
        self._run_on_loop = run_on_loop

    def send(self, text: str, *, urgent: bool = False) -> bool:
        ok, detail = self.send_with_detail(text)
        if not ok:
            logger.warning("inbox notifier: push not delivered: %s", _redact(detail))
        return ok

    def send_with_detail(self, text: str) -> tuple[bool, dict]:
        """Like :meth:`send` but returns ``(ok, detail)`` for diagnostics.

        ``detail`` records every decision point — whether the gateway was
        captured, which loop ran the coroutine, the resolved target, and the raw
        ``deliver()`` result (or the exception + a trimmed traceback). NEVER
        raises (Notifier contract). Used by the ``/inboxnotifyprobe`` probe so a
        failure shows *why* in-chat (the runtime logger isn't on docker stdout).
        """
        detail: dict[str, Any] = {
            "gateway_captured": False,
            "loop": None,
            "target": None,
            "target_source": None,
            "result": None,
            "error": None,
        }
        gateway = self._get_gateway()
        if gateway is None:
            # B2: the daemon can fire before any Matrix turn has been captured.
            detail["error"] = "gateway not captured yet (no Matrix turn seen this process)"
            return False, detail
        detail["gateway_captured"] = True
        detail["loop"] = (
            "gateway" if getattr(gateway, "_gateway_loop", None) is not None else "background-fallback"
        )
        try:
            # Explicit override first, then Hermes's home channel (/sethome).
            target = self._resolve_target(self._target) if self._target else None
            detail["target_source"] = "override" if target is not None else None
            if target is None:
                target = self._resolve_home(gateway, self._get_source(), self._default_platform)
                detail["target_source"] = "home"
            if target is None:
                detail["error"] = "no destination (no /sethome home channel and no INBOX_NOTIFY_TARGET)"
                return False, detail
            detail["target"] = _target_repr(target)
            results = self._run_on_loop(
                gateway.delivery_router.deliver(text, [target]), gateway, self._timeout
            )
            detail["result"] = results
            return _delivery_ok(results), detail
        except Exception as exc:
            import traceback

            detail["error"] = f"{type(exc).__name__}: {exc}"
            detail["traceback"] = traceback.format_exc()[-600:]
            return False, detail
