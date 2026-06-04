"""The module registry — runs enabled modules at the right points, safely.

Owns the ordered list of enabled modules and three responsibilities:

* :meth:`classify` — DECISION phase. Tries each module's ``classify_override`` in
  priority order; the first non-None result that resolves to a real, non-sent
  category wins (validated exactly like ``classifier.classify``: unknown or
  ``sent_only`` categories are rejected). Otherwise falls back to the default
  classifier. Runs under the runtime lock, so it never does I/O itself — a module
  override that raises is caught and skipped.
* :meth:`dispatch_inbound` / :meth:`dispatch_sent` — NOTIFICATION phase. Fan each
  event out to every enabled module's observer, OFFLOADED to a bounded thread
  pool so a slow/blocking module can't stall the drain (which holds the runtime
  lock) and an exception in one module is isolated from the others and the drain.
* :meth:`tools` / :meth:`periodic` — aggregate module contributions for wiring.

A module crash is always contained + logged, never propagated into triage.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from ..classifier import classify as _default_classify
from ..labels import category_by_name
from .base import InboundEvent, Module, PeriodicJob, SentEvent, ToolSpec

logger = logging.getLogger(__name__)

ClassifyFn = Callable[[dict], str]


class InlineExecutor:
    """Runs submitted work synchronously (tests / a deterministic mode).

    Mirrors the slice of the ``concurrent.futures.Executor`` API the registry
    uses (``submit`` + ``shutdown``) so dispatch is observable without waiting on
    a real pool.
    """

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        fn(*args, **kwargs)

    def shutdown(self, wait: bool = True) -> None:  # noqa: D401 - parity shim
        return None


class ModuleRegistry:
    def __init__(
        self,
        modules: Optional[list[Module]] = None,
        *,
        classify_fn: ClassifyFn = _default_classify,
        max_workers: int = 4,
        executor: Any = None,
    ) -> None:
        # Keep only enabled modules; stable-sort by priority (lower = earlier).
        enabled = [m for m in (modules or []) if _is_enabled(m)]
        self._modules: list[Module] = sorted(enabled, key=lambda m: getattr(m, "priority", 100))
        self._classify_fn = classify_fn
        self._executor = executor or ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="inbox-module"
        )

    @property
    def modules(self) -> list[Module]:
        return list(self._modules)

    # -- decision phase ---------------------------------------------------------
    def classify(self, parsed: dict) -> str:
        """First valid module override (by priority) wins, else the default classifier."""
        for m in self._modules:
            try:
                cat = m.classify_override(parsed)
            except Exception:
                logger.exception(
                    "inbox module %s: classify_override raised; deferring", _name(m)
                )
                continue
            if cat is None:
                continue
            resolved = category_by_name(str(cat))
            if resolved is None or resolved.sent_only:
                logger.warning(
                    "inbox module %s: classify_override returned invalid category %r; deferring",
                    _name(m),
                    cat,
                )
                continue
            logger.info("inbox module %s: classify_override -> %s", _name(m), resolved.name)
            return resolved.name
        return self._classify_fn(parsed)

    # -- notification phase (offloaded) -----------------------------------------
    def dispatch_inbound(self, event: InboundEvent) -> None:
        for m in self._modules:
            self._submit(m, "on_inbound", event)

    def dispatch_sent(self, event: SentEvent) -> None:
        for m in self._modules:
            self._submit(m, "on_sent", event)

    def _submit(self, module: Module, hook: str, event: Any) -> None:
        fn = getattr(module, hook, None)
        if fn is None:
            return

        def _run() -> None:
            try:
                fn(event)
            except Exception:
                logger.exception(
                    "inbox module %s: %s failed for message %s",
                    _name(module),
                    hook,
                    getattr(event, "message_id", "?"),
                )

        try:
            self._executor.submit(_run)
        except RuntimeError:
            # Executor already shut down — run inline so the event isn't dropped.
            _run()

    # -- contributions ----------------------------------------------------------
    def tools(self) -> list[ToolSpec]:
        out: list[ToolSpec] = []
        for m in self._modules:
            try:
                out.extend(m.tools() or [])
            except Exception:
                logger.exception("inbox module %s: tools() failed", _name(m))
        return out

    def periodic(self) -> list[PeriodicJob]:
        out: list[PeriodicJob] = []
        for m in self._modules:
            try:
                out.extend(m.periodic() or [])
            except Exception:
                logger.exception("inbox module %s: periodic() failed", _name(m))
        return out

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)


def _is_enabled(module: Module) -> bool:
    try:
        return bool(module.enabled)
    except Exception:
        logger.exception("inbox module %s: enabled check raised; treating as disabled", _name(module))
        return False


def _name(module: Any) -> str:
    return getattr(module, "name", module.__class__.__name__)
