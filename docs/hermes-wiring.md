# Hermes Wiring — enabling the plugin

The plugin loads **in-process** with the Hermes agent. There is no MCP server,
no URL, and no bearer token — you enable it in the agent's config and it
registers its tools + daemon at load time.

## config.yaml

```yaml
plugins:
  enabled:
    - inbox_organizer        # the entry-point name (pyproject: inbox_organizer = "hermes_inbox_organizer")
```

That's the whole wiring. Everything else (OAuth client, Pub/Sub coordinates,
encryption key, owner allowlist) is read from the config mount and `INBOX_*`
env — see [setup.md](./setup.md).

### Config the plugin reads

| Source | Purpose |
|---|---|
| `config/inbox-oauth-client.json` | OAuth client + `owner_matrix_ids` (the allowlist) |
| `config/inbox-pubsub.json` | `{project, topic, subscription}` |
| `config/inbox-pubsub-sa.json` | `pubsub.subscriber` SA key (streaming pull auth) |
| `config/inbox-encryption-key` | AES-GCM key for tokens at rest |
| env `OPENROUTER_API_KEY` | LLM-fallback classifier |
| env `INBOX_*` (optional) | overrides — `INBOX_DATA_DIR`, `INBOX_DB_PATH`, `INBOX_CLASSIFIER_MODEL`, `INBOX_OWNER_MATRIX_IDS`, `INBOX_LABELS_ENABLED` (off = classify/draft only, no labels or archiving), … (see `config.py`) |

## Agent tools

`register(ctx)` exposes these to the agent:

| Tool | Description |
|---|---|
| `inbox_list_accounts` | List connected Gmail accounts |
| `inbox_list_emails` | Search/list mail across inboxes (Gmail search syntax) |
| `inbox_get_email` | Fetch a single message |
| `inbox_get_thread` | Fetch a thread + its messages |
| `inbox_unread_rollup` | On-demand "what needs me?" digest across accounts (read-only) |
| `inbox_create_draft` | Write a reply **draft** for a thread (never sends) — Hermes composes the body |
| `inbox_connect_account` / `inbox_complete_connection` / `inbox_disconnect_account` | Owner-only account onboarding |
| `inbox_draft_feedback_status` | Inspect what the reinforcement loop has learned (outcome histogram, lessons, learned-note senders) — read-only |
| `inbox_forget_lesson` | Soft-disable a learned do/don't lesson by id — owner-gated |
| `inbox_clear_learned_notes` | Clear a correspondent's learned voice note — owner-gated |

The daemon also classifies + labels mail and triggers drafts autonomously — those
aren't tools, they happen in the background.

## Owner gating

The account-management tools are restricted to the owner via a `pre_tool_call`
hook that checks the turn's sender against `owner_matrix_ids` (resolved per turn
through `pre_gateway_dispatch` → `pre_llm_call`). A non-owner call is blocked.

## Verifying

After enabling + rebuilding, the boot log shows the hook loading and the daemon
threads coming up:

```
[hooks] Loaded hook 'inbox-organizer-boot' for events: ['gateway:startup']
[inbox-organizer-boot] inbox daemon threads alive: ['inbox-poll-reconciler', 'inbox-watch-renewal']
```

Then ask the agent to run `inbox_list_accounts` to confirm the tools are live.
(The runtime logs via Python `logging`, which isn't on docker stdout — confirm
the daemon via those boot-hook thread lines, not by grepping for runtime logs.)

## Further reading

- [draft-reinforcement-loop.md](./draft-reinforcement-loop.md) — how the draft
  feedback loop works, all `INBOX_DRAFT_FEEDBACK_*` knobs, and how to inspect
  or revert the learned state
