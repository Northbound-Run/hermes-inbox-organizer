# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-06-24

### Added

- **Setup wizard** — a `hermes-inbox-organizer` console script (also
  `python -m hermes_inbox_organizer`, and `hermes inbox-organizer …`) with:
  - `setup` — interactive wizard that writes the OAuth/Pub/Sub/service-account
    config files into `INBOX_CONFIG_DIR`, auto-generates the AES encryption key
    (never regenerates an existing one), and never echoes secrets; secret files
    are written mode `0600`.
  - `status` — reports what's configured and the resulting capabilities.
- **`HERMES_SELF_INSTALL.md`** — an agent-executable runbook so a Hermes agent can
  install + configure the plugin itself and walk the owner through Google Cloud.
- **Any OpenAI-compatible classifier endpoint** — the classifier LLM is no longer
  hardcoded to OpenRouter. Set `INBOX_CLASSIFIER_BASE_URL` + `INBOX_CLASSIFIER_API_KEY`
  (e.g. a local vLLM/Ollama/LM Studio server or another gateway);
  `OPENROUTER_API_KEY` is still accepted and remains the default.

### Changed

- README: a copy-paste **Quick Start** built around the `setup` wizard, an
  **Updating** section, status badges, and backend-neutral owner-id wording (the
  allowlist accepts any gateway `source.user_id`, not just Matrix).
- **Adopted the shared [Hermes Plugin Standard](HERMES_PLUGIN_STANDARD.md)**:
  full ruff ruleset (`E,F,I,W,UP,B,SIM,RUF`) + mypy type-checking with a shipped
  `py.typed`; CI now runs lint + mypy and a 3.11–3.14 test matrix on
  `actions/checkout@v5` / `setup-python@v6`, plus a build job that asserts the
  packaged manifest/assets ship in the wheel; author set to
  `Northbound <matthall28@gmail.com>`. (Line-length 100 is declared; a one-time
  `ruff format` pass to enforce E501 is a tracked follow-up.)

## [0.1.0] - 2026-06-23

First public release. Autonomous Gmail triage that loads into a
[Hermes](https://hermes-agent.nousresearch.com) agent in-process as a
`pip`-installable plugin.

### Added

- **Triage daemon** — Gmail `watch()` → Pub/Sub streaming pull (outbound only, no
  webhook) → drain history from a stored cursor → classify → label. A polling
  reconciler re-drains on a timer so a dropped push never strands mail.
- **Hybrid classifier** — a deterministic header/sender pre-classifier handles
  most mail; a small OpenRouter model is the fallback, keeping per-message cost a
  fraction of a cent. Untrusted email is fenced before it ever reaches the model.
- **Eight Fyxer-style labels** (`1: To Respond` … `8: Marketing`). Only
  `1: To Respond` and `2: FYI` stay in the inbox; the rest skip-inbox and
  archive. `INBOX_LABELS_ENABLED=0` runs classify/draft-only with no label or
  archive mutations.
- **Hermes-drafted replies** for `1: To Respond` — the plugin wakes the agent to
  compose a reply in your voice and writes the MIME draft (drafts only, never
  sent).
- **Sent-handling** — moves a thread to `7: Actioned` after you reply, or
  `6: Awaiting Reply` when you send and are waiting.
- **Draft reinforcement loop** — learns from draft→sent edits, distilling
  per-sender voice notes, global do/don't lessons, and gold-example replies into
  a separate auditable layer that feeds future drafting briefs (in-context, no
  fine-tuning).
- **Unread rollup** tool — "what needs me across my inboxes?" across accounts.
- **Multi-account** — connect/disconnect by chatting with Hermes or from an
  optional **Inbox Organizer** tab in the Hermes web dashboard. Copy-paste OAuth.
- **Pluggable modules** — 2FA-code surfacing, shipping/17track tracking, rollup,
  and draft-feedback ship in-tree behind a module registry.
- **Agent tools** — `inbox_create_draft`, `inbox_list_accounts`,
  `inbox_list_emails`, `inbox_get_email`, `inbox_get_thread`,
  `inbox_unread_rollup`, `inbox_connect_account` / `inbox_complete_connection` /
  `inbox_disconnect_account`, `inbox_draft_feedback_status`,
  `inbox_forget_lesson`, `inbox_clear_learned_notes`.
- **State** — SQLite (`state.db`, schema v3, migrates in place) for history
  cursors, draft idempotency, classified messages, thread state, and draft
  outcomes/lessons. OAuth tokens are AES-256-GCM encrypted at rest.

[Unreleased]: https://github.com/Northbound-Run/hermes-inbox-organizer/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Northbound-Run/hermes-inbox-organizer/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Northbound-Run/hermes-inbox-organizer/releases/tag/v0.1.0
