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

## Deploy

The plugin is baked into a Hermes agent image via the `hermes-template` repo
(`pip install ".[live]"` + a `gateway:startup` boot hook), then deployed with
`docker compose build && up -d`. It needs a Google Cloud project with a Pub/Sub
topic/subscription, a Gmail OAuth client, and an OpenRouter API key.

## License

MIT — see [LICENSE](LICENSE).
