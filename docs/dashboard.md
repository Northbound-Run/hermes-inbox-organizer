# Dashboard — connect & remove accounts from the web UI

The plugin ships an optional **dashboard plugin** that adds an **Inbox Organizer**
tab to the Hermes web dashboard (`hermes dashboard`). It's a thin UI over the same
encrypted token store the daemon uses, for the one thing that's awkward in chat:
**connecting and removing Google accounts**.

It's additive to — not a replacement for — the chat onboarding tools
(`inbox_connect_account` / `inbox_disconnect_account`); use whichever you prefer.

## What the tab does

- **Lists** the connected Google accounts.
- **Connect** — copy-paste OAuth: click *Connect a Google account*, approve in the
  Google tab, paste the code from the callback page, *Finish*.
- **Remove** — revokes the account at Google and deletes its stored token.

Connect/remove only touch the encrypted token files in the data dir; the running
daemon converges its live account set on its next poll tick (≤ ~5 min) or on
restart — see [Convergence](#convergence).

## How it's wired as a Hermes dashboard plugin

Hermes's web dashboard is extensible via a `dashboard/` directory under
`$HERMES_HOME/plugins/<name>/` holding a `manifest.json`, a JS bundle, and an
optional FastAPI `plugin_api.py`
([upstream docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/extending-the-dashboard)).
Unlike the CLI/gateway plugin (loaded via the `hermes_agent.plugins` entry point),
dashboard plugins are **directory-discovered** — there is no entry-point hook — so
the package projects its bundled `dashboard/` into
`$HERMES_HOME/plugins/inbox_organizer/dashboard/`:

- **On plugin load** — `register()` self-projects it, so any install (including a
  plain `pip install`) gets the tab.
- **pip / manual** — (re)install any time with `hermes inbox-organizer install-dashboard`.
- **Baked image** — the entrypoint also projects it at boot (parity with the
  dashboard-visibility shim), so the tab exists before the first agent turn.

The backend logic lives in the installed package
(`hermes_inbox_organizer.dashboard_api`); the projected `dashboard/plugin_api.py`
is a one-line re-export, so the routes version and unit-test with the plugin.

> **Restart the dashboard after first install.** Hermes mounts plugin backend
> routes once at dashboard startup, so a freshly projected `plugin_api.py` needs a
> `hermes dashboard` restart. (The *tab* alone can be picked up without a restart
> via `GET /api/dashboard/plugins/rescan`.)

## Authentication

The plugin's backend routes (`/api/plugins/inbox_organizer/*`) **bypass
session-token auth** — that's how Hermes dashboard plugins work; the dashboard
binds to localhost and its own auth is the gate. This plugin is **single-owner**,
so it adds no extra gate of its own: anyone who can reach the dashboard can
connect/remove accounts. Keep the dashboard localhost-only (SSH-tunnel it) or
behind the dashboard's gated-mode auth — do **not** serve it with `--host 0.0.0.0`
on an untrusted network.

(The chat onboarding tools are gated differently — by `owner_matrix_ids`. The
dashboard has no per-sender concept, so it relies on the dashboard's own access
control instead. See [security.md](./security.md).)

## Convergence

The dashboard runs in a **separate process** from the gateway/daemon; they share
only the `/opt/data` volume. So the tab can't hot-add/remove an account in the
daemon's memory directly — it writes/deletes the encrypted token file, and the
daemon's **poll reconciler** diffs the on-disk tokens against its live set each
tick:

- new token file → arm the watch + start triaging it
- deleted token file → drop it from routing
- **changed** token file (a reconnect, e.g. after a 7-day refresh-token expiry) →
  re-arm it and clear the owner's reconnect nudge

So a connect/remove from the tab goes live within one poll interval, no restart
needed. (The same reconciler is what makes a chat-tool connect on one process
visible to the daemon.)

## Further reading

- [setup.md](./setup.md) — deploying the plugin (image + entrypoint)
- [security.md](./security.md) — token encryption + the owner gate
- [oauth-modes.md](./oauth-modes.md) — Testing vs Production OAuth audience
