# hermes-inbox-organizer

Autonomous Gmail triage as an in-process **Hermes plugin** — it sorts, labels,
and archives your mail and drafts replies in your voice, self-hosted, with no
third-party SaaS in the path of your inbox.

[![CI](https://github.com/Northbound-Run/hermes-inbox-organizer/actions/workflows/ci.yml/badge.svg)](https://github.com/Northbound-Run/hermes-inbox-organizer/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/hermes-inbox-organizer)](https://pypi.org/project/hermes-inbox-organizer/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## What it does

A Hermes agent (`NousResearch/hermes-agent`) loads this plugin in-process and it
runs a continual Gmail triage daemon: each new message is classified by a hybrid
**pre-classifier + OpenRouter LLM** pipeline and gets a colored Fyxer-style
numbered Gmail label (`1: To Respond` … `8: Marketing`). Only `1: To Respond`
and `2: FYI` stay in the inbox; the rest skip-inbox and archive. For
`1: To Respond`, the plugin asks **Hermes to compose a reply draft** in your
voice (drafts only, never sent). When you reply, the thread moves to
`7: Actioned`; when you send and are waiting, `6: Awaiting Reply`.

No separate service and no public webhook — it's one plugin that loads with the
agent. It can also add an optional **Inbox Organizer tab** to Hermes's own web
dashboard for connecting/removing accounts ([docs/dashboard.md](docs/dashboard.md)).

## Capabilities

- Multi-account Gmail (connect/disconnect by **chatting with Hermes**, or from an
  optional **web dashboard tab** — [docs/dashboard.md](docs/dashboard.md))
- Hybrid triage: deterministic header/sender rules + LLM fallback
- Hermes-drafted replies for `1: To Respond`
- Sent-handling (`Actioned` / `Awaiting Reply`)
- **Draft reinforcement loop** — learns from draft→sent deltas: distils
  per-sender voice notes + global do/don't lessons + gold-example replies into
  a separate auditable layer that feeds future drafting briefs (in-context
  learning-from-edits; no model fine-tuning)
- On-demand unread **rollup** tool ("what needs me across my inboxes?")
- Agent tools: `inbox_create_draft`, `inbox_list_accounts`, `inbox_list_emails`,
  `inbox_get_email`, `inbox_get_thread`, `inbox_unread_rollup`,
  `inbox_connect_account` / `inbox_complete_connection` / `inbox_disconnect_account`,
  `inbox_draft_feedback_status`, `inbox_forget_lesson`, `inbox_clear_learned_notes`

## How it works

- **Load**: `register(ctx)` registers the agent tools + a `pre_llm_call` nudge
  hook and starts the daemon. A `gateway:startup` hook starts the daemon at boot
  (so it doesn't wait for the first agent turn).
- **Sync**: Gmail `watch()` → Pub/Sub **streaming pull** (outbound connection —
  no webhook/tunnel) → drain history from a stored cursor → classify + label →
  wake Hermes to draft. A **polling reconciler** re-drains on a timer in case a
  push is dropped.
- **State**: SQLite (`state.db`) in the Hermes data volume — history cursors,
  draft idempotency, classified messages, thread state, draft outcomes + learned
  lessons (schema v3, migrates in place on deploy). OAuth tokens are
  AES-256-GCM encrypted at rest.
- **Cost**: the cheap local pre-classifier handles most mail; the LLM fallback
  (a small OpenRouter model) runs only when needed, so per-message cost is a
  fraction of a cent.

## Install

This is a **pip / entry-point Hermes plugin** — it ships the
`hermes_agent.plugins` entry point and your Hermes agent loads it in-process. It
is **not** installed with `hermes plugins install <repo>`: that command
git-clones a *directory* plugin and never installs Python dependencies, which
this plugin needs (Gmail, Pub/Sub, OpenRouter). Use `pip`. For the
discovery/enable model see the
[Hermes plugin docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins).

You need a **Hermes** deployment you control. The three steps below are: set up
**Google Cloud**, install + enable the **plugin**, then drop in its **config**.

### 1. Google Cloud

The plugin watches Gmail over Pub/Sub via an **outbound streaming pull** (no
webhook, no public endpoint), so it needs a Google Cloud project with the Gmail +
Pub/Sub APIs, an OAuth client, and a topic + pull subscription.

1. **Project** — [console.cloud.google.com](https://console.cloud.google.com/) →
   project selector → **New Project** (e.g. `hermes-inbox`).
2. **Enable APIs** — APIs & Services → Library → enable **Gmail API**
   (`gmail.googleapis.com`) and **Cloud Pub/Sub API** (`pubsub.googleapis.com`).
3. **OAuth consent screen** — APIs & Services → OAuth consent screen →
   **External**. Add scopes `gmail.modify`, `gmail.send`, `userinfo.email`,
   `userinfo.profile`; add your Gmail as a **Test User**; then **Publish App**
   (Production audience) to avoid the 7-day refresh-token expiry
   ([docs/oauth-modes.md](docs/oauth-modes.md)).
4. **OAuth client** — Credentials → Create Credentials → OAuth client ID →
   **Web application**. For the **Authorized redirect URI** use a static page
   that just displays the code for you to paste back — host `oauth-callback/`
   yourself or reuse `https://inbox-organizer.northbound.run/` (it holds no
   secrets; the OAuth client itself must still be **your own**). Save the
   client id/secret for step 3.
5. **Pub/Sub** — Pub/Sub → Topics → **Create Topic** (e.g. `gmail-notifications`);
   on that topic, **Create Subscription** with delivery type **Pull** (e.g.
   `gmail-inbox-organizer-pull`).
6. **Let Gmail publish** — on the topic → Permissions → **Add Principal**
   `gmail-api-push@system.gserviceaccount.com`, role **Pub/Sub Publisher**.
7. **Subscriber key** — IAM & Admin → Service Accounts → create one (e.g.
   `hermes-inbox-pubsub`), grant **`roles/pubsub.subscriber`** *on the
   subscription* (not project-wide), and download a JSON key.

(Screens and detail: [docs/google-bootstrap.md](docs/google-bootstrap.md).)

### 2. Install the plugin

Install into the **same Python environment** as your `hermes` CLI:

```sh
pip install "hermes-inbox-organizer[live]"
# or from source: pip install "hermes-inbox-organizer[live] @ git+https://github.com/Northbound-Run/hermes-inbox-organizer"
```

`[live]` pulls the Gmail/Pub/Sub/OpenRouter deps. Hermes auto-discovers the
plugin via its entry point; **enable** it (pip plugins are opt-in):

```sh
hermes plugins enable inbox_organizer    # or add `inbox_organizer` to plugins.enabled in ~/.hermes/config.yaml
```

### 3. Config + secrets

The plugin reads its secrets as **files** from a config dir
(`INBOX_CONFIG_DIR`, default `/opt/data/config`) and writes its SQLite DB +
encrypted tokens to a data dir (`INBOX_DATA_DIR`, default
`/opt/data/inbox-organizer`). Point both env vars at real paths if those
defaults don't fit your host, then install these files into `INBOX_CONFIG_DIR`:

| File | Contents |
|---|---|
| `inbox-oauth-client.json` | `{ "client_id", "client_secret", "redirect_uri", "owner_matrix_ids": ["@you:your-homeserver"] }` (from step 1.4) |
| `inbox-pubsub.json` | `{ "project", "topic": "projects/<id>/topics/<topic>", "subscription" }` (from step 1.5) |
| `inbox-pubsub-sa.json` | the `pubsub.subscriber` service-account key (step 1.7) |
| `inbox-encryption-key` | 32 hex bytes — `openssl rand -hex 32` (AES-GCM for tokens at rest) |

And in the Hermes environment:

- `OPENROUTER_API_KEY` — the classifier LLM
  ([openrouter.ai/keys](https://openrouter.ai/keys)); most Hermes setups already
  have one.
- `HERMES_API_URL` *(optional)* — the Hermes OpenAI-compatible endpoint, needed
  only for the drafted replies; triage + labeling run without it.

Restart Hermes. To start the daemon at boot (instead of on the first agent
turn), copy the `gateway:startup` hook from
[`deploy/hooks/inbox-organizer-boot/`](deploy/hooks/inbox-organizer-boot) into
`~/.hermes/hooks/inbox-organizer-boot/`.

### 4. Connect a Gmail account

Two ways — same copy-paste OAuth either way:

- **Chat with Hermes** — *"Connect a Gmail account."* The agent returns a Google
  consent link; approve it, paste the code from the callback page back, and the
  account is hot-added (owner-gated). Repeat for additional mailboxes.
- **Dashboard tab** — open Hermes's web dashboard and use the **Inbox Organizer**
  tab to connect/remove accounts with buttons instead of tools. If the tab isn't
  there yet, run `hermes inbox-organizer install-dashboard` once and restart
  `hermes dashboard`. See [docs/dashboard.md](docs/dashboard.md).

### Verify

```sh
hermes plugins list                          # inbox_organizer → enabled
HERMES_PLUGINS_DEBUG=1 hermes plugins list   # verbose discovery if it doesn't show up
```

In a running session, `/plugins` lists it as loaded. Ask the agent to run
`inbox_list_accounts`, then send yourself a test email — it should get a numbered
label within seconds (push) or a few minutes (the polling reconciler).

> **Deploying into a baked container image** (the daemon up at boot, secrets in a
> read-only mount)? That's how this repo's own stack runs — see
> [docs/setup.md](docs/setup.md) for the Dockerfile + entrypoint wiring.

## Layout

- **`hermes_inbox_organizer/`** — the plugin package (the module), including its
  bundled `dashboard/` web-UI plugin; with `tests/`, `deploy/` (the
  `gateway:startup` boot hook), and `pyproject.toml` at the repo root.
- **`oauth-callback/`** — the static OAuth callback page (Cloudflare Pages) used
  by chat onboarding.
- **`docs/`** — setup, Google Cloud, OAuth, sync, security, and dashboard notes.

## Develop

```sh
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

## License

MIT — see [LICENSE](LICENSE).
