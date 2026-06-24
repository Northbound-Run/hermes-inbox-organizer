"""Dedicated background asyncio loop on a daemon thread.

Proven pattern (lifted from hermes-chat-recorder): a plugin owns a private
event loop on a ``daemon=True`` thread and bridges sync→async work onto it via
``run_coro_sync``. Used here to (a) run the gateway coroutine for synthetic
draft-turn injection from our daemon thread, and (b) host the Pub/Sub
streaming-pull subscriber's callbacks if needed.

``asyncio.run`` is unsafe from inside a running loop; ``run_coroutine_threadsafe``
works only from a thread that is NOT the loop's own — hence a dedicated thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Awaitable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_instance: BackgroundLoop | None = None
_lock = threading.Lock()


class BackgroundLoop:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="inbox-background-loop", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        except Exception:  # pragma: no cover - daemon thread
            logger.exception("inbox background loop crashed")
        finally:
            with contextlib.suppress(Exception):  # pragma: no cover
                self._loop.close()

    def run_coro_sync(self, coro: Awaitable[T], *, timeout: float = 120.0) -> T:
        if not self._loop.is_running():
            raise RuntimeError("background loop is not running")
        fut = asyncio.run_coroutine_threadsafe(_ensure_coro(coro), self._loop)
        return fut.result(timeout=timeout)

    def is_running(self) -> bool:
        return self._loop.is_running()


async def _ensure_coro(awaitable: Awaitable[T]) -> T:
    return await awaitable


def get_background_loop() -> BackgroundLoop:
    global _instance
    with _lock:
        if _instance is None:
            _instance = BackgroundLoop()
        return _instance


def _reset_for_tests() -> None:
    global _instance
    with _lock:
        _instance = None
