# Security Model

The plugin runs **in-process** inside the Hermes agent. There are no network
endpoints to authenticate (no HTTP server, no webhook) — access control is about
who can drive the agent's tools, and data protection is about tokens at rest and
what content reaches the LLM.

## Access control — owner gating

The account-management tools (`inbox_connect_account`, `inbox_complete_connection`,
`inbox_disconnect_account`) are restricted to the owner. A `pre_tool_call` hook
checks the turn's sender (bound per turn via `pre_gateway_dispatch` →
`pre_llm_call`) against `owner_matrix_ids` (in `inbox-oauth-client.json` /
`INBOX_OWNER_MATRIX_IDS`) and blocks a non-owner call. The read tools + draft
creation are non-destructive (drafts are never sent).

## Refresh tokens at rest

Gmail OAuth tokens are encrypted with **AES-256-GCM** before being written to the
data volume (`crypto.py` / `token_store.py`). The key lives in the read-only
config mount (`inbox-encryption-key`), never in the repo or the image. Tokens are
never logged.

Caveat of the plugin form: the daemon holds decrypted tokens in the agent's
process memory while running (no process isolation from the agent) — at-rest
encryption mitigates disk exposure, not in-process memory.

## Privacy / data flow

- **To OpenRouter** (LLM-fallback classification only): sender, subject, snippet,
  and a truncated body. The cheap local pre-classifier (header/sender rules)
  handles a lot of mail with *no* external call. OpenRouter uses the operator's
  own `OPENROUTER_API_KEY`. (This is a single-owner deployment processing the
  owner's own mailboxes; there is no per-account consent gate — the
  `openrouter_consent_at` column exists but is not currently enforced.)
- **To Google** (Gmail API): label changes and draft creates.
- Nothing else leaves the host — no analytics, no telemetry.

## Prompt-injection guard

All untrusted email content is wrapped in randomized fences before it reaches the
classifier prompt, and the system prompt marks fenced content as DATA, never
instructions (`classifier.py`). Injection cases are covered in
`tests/test_classifier.py`.

## Secrets

The OAuth client JSON, the Pub/Sub `pubsub.subscriber` SA key, and the encryption
key all live in the read-only config mount, owned by the runtime uid, never
committed. The Pub/Sub SA is scoped to `pubsub.subscriber` on the one
subscription — not domain-wide delegation.

## Known non-goals

No multi-tenant isolation (single owner, own mailboxes), no cost-budget
kill-switch or data-retention cron (deliberately skipped for this deployment —
recoverable from git history if ever needed), no formal SLA or third-party pen test.

## Reporting

Responsible disclosure preferred. Contact: matthew@hall.vc
