# hermes-inbox-organizer

Autonomous Gmail triage as an in-process **Hermes plugin**.

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

No separate service, no public webhook, no HTTP server — it's one plugin that
loads with the agent.

## Capabilities

- Multi-account Gmail (connect/disconnect by **chatting with Hermes**)
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
`hermes_agent.plugins` entry point and Hermes loads it in-process. It is **not**
installed with `hermes plugins install <repo>`: that command git-clones a
*directory* plugin and never installs Python dependencies, which this plugin
needs (Gmail, Pub/Sub, OpenRouter). Use `pip`. For the discovery/enable model see
the [Hermes plugin docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins).

**Prerequisites**

- A **Hermes** deployment you control (the triage daemon runs in-process).
- A **Google Cloud** project (Gmail + Pub/Sub APIs, an OAuth client, a topic +
  pull subscription) and an **OpenRouter** API key. Full walkthrough:
  [docs/setup.md](docs/setup.md) and [docs/google-bootstrap.md](docs/google-bootstrap.md).

### Option A — bake into your Hermes image (recommended)

Hermes deployments are usually containers, and the daemon should start at boot,
so the production path is to install the package into your agent image. In your
Hermes `Dockerfile` (the venv is where the `hermes` CLI lives):

```dockerfile
RUN /opt/hermes/.venv/bin/python -m pip install --no-cache-dir "hermes-inbox-organizer[live]"
# from source instead of PyPI:
#   ... "hermes-inbox-organizer[live] @ git+https://github.com/Northbound-Run/hermes-inbox-organizer"
```

`[live]` pulls the Gmail/Pub/Sub/OpenRouter deps. Then **enable** it in the
agent's `config.yaml` (pip plugins are opt-in):

```yaml
plugins:
  enabled:
    - inbox_organizer
```

Finally: install the secrets into the read-only config mount, set
`OPENROUTER_API_KEY` in the Hermes environment, and install the `gateway:startup`
boot hook so the daemon starts at boot (otherwise it waits for the first agent
turn). The [`hermes-template`](https://github.com/Northbound-Run/hermes-template)
stack wires all of this up — the step-by-step is in **[docs/setup.md](docs/setup.md)**.

### Option B — pip into an existing Hermes

For a Hermes you can `pip install` into directly, install into the **same
environment** as the `hermes` CLI, then enable and restart:

```sh
pip install "hermes-inbox-organizer[live]"        # or: "...[live] @ git+https://github.com/Northbound-Run/hermes-inbox-organizer"
hermes plugins enable inbox_organizer             # or add `inbox_organizer` to plugins.enabled in ~/.hermes/config.yaml
```

Then configure it for a non-container host:

- `OPENROUTER_API_KEY` in the environment (the classifier LLM).
- Point `INBOX_CONFIG_DIR` (read-only secrets) and `INBOX_DATA_DIR` (writable DB
  + encrypted tokens) at real paths — they default to `/opt/data/...` for the
  container. Drop the OAuth client, Pub/Sub config, service-account key, and AES
  key into `INBOX_CONFIG_DIR` per [docs/setup.md](docs/setup.md).
- For start-at-boot, copy the hook from
  [`deploy/hooks/inbox-organizer-boot/`](deploy/hooks/inbox-organizer-boot) into
  `~/.hermes/hooks/inbox-organizer-boot/`; without it the daemon starts on the
  first agent turn.

### Connect a Gmail account

There's no web wizard — you connect by **chatting with Hermes**: *"Connect a
Gmail account."* The agent returns a Google consent link; approve it, paste the
code back, and the account is hot-added (owner-gated only).

### Verify

```sh
hermes plugins list                          # inbox_organizer → enabled
HERMES_PLUGINS_DEBUG=1 hermes plugins list   # verbose discovery if it doesn't show up
```

In a running session, `/plugins` lists it as loaded. Then ask the agent to run
`inbox_list_accounts`, and send yourself a test email — it should get a numbered
label within seconds (push) or a few minutes (the polling reconciler).

## Layout

- **`hermes_inbox_organizer/`** — the plugin package (the module), with `tests/`,
  `deploy/` (the `gateway:startup` boot hook), and `pyproject.toml` at the repo root.
- **`oauth-callback/`** — the static OAuth callback page (Cloudflare Pages) used
  by chat onboarding.
- **`docs/`** — setup, Google Cloud, OAuth, sync, and security notes.

## Develop

```sh
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

## License

MIT — see [LICENSE](LICENSE).
