"""gateway:startup hook — start the inbox-organizer daemon at boot.

Why this exists
---------------
Hermes loads ``hermes_agent.plugins`` entry-point plugins lazily, on the first
AIAgent construction (not at gateway boot). The inbox-organizer plugin starts
its continual Pub/Sub triage daemon inside ``register(ctx)``, so after a restart
or ``docker compose up`` the daemon stays dormant until someone next interacts
with Hermes — autonomous triage is paused in the meantime.

This hook fires on ``gateway:startup`` and calls ``discover_plugins()``, which
runs every enabled plugin's ``register()`` (and thus starts the daemon) right
away. ``discover_plugins()`` is idempotent (it uses the global PluginManager
singleton), so the later per-agent load is a no-op and tools/hooks still
propagate to agents exactly as before.

Errors are swallowed + logged (the gateway hook runner already isolates hook
failures) so a problem here can never block gateway boot.
"""

from __future__ import annotations


async def handle(event_type, context):  # Hermes hook signature: (event_type, context)
    tag = "[inbox-organizer-boot]"
    try:
        from hermes_cli.plugins import discover_plugins

        print(f"{tag} {event_type}: loading entry-point plugins so the inbox daemon starts at boot", flush=True)
        discover_plugins()
        print(f"{tag} discover_plugins() complete", flush=True)
    except Exception as exc:  # never block gateway boot
        import traceback

        print(f"{tag} FAILED to load plugins at startup: {exc!r}", flush=True)
        traceback.print_exc()
        return

    # Best-effort, NON-blocking confirmation that the daemon thread actually came
    # up (the inbox runtime logs via `logging`, which isn't on docker stdout, so
    # we check for its background thread instead). Scheduled on the running loop
    # so it never delays gateway boot; purely informational.
    try:
        import asyncio

        asyncio.get_running_loop().create_task(_confirm_daemon(tag))
    except Exception:
        pass


async def _confirm_daemon(tag: str) -> None:
    import asyncio
    import threading

    await asyncio.sleep(8)  # give register()'s start thread time to arm + subscribe
    names = sorted(t.name for t in threading.enumerate() if "inbox" in t.name.lower())
    if names:
        print(f"{tag} inbox daemon threads alive: {names}", flush=True)
    else:
        print(f"{tag} WARNING: no inbox daemon threads found — daemon may not have started", flush=True)
