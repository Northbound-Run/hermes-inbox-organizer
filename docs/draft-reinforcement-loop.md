# Draft reinforcement loop

The plugin closes the feedback loop on draft writing: it records what it
drafted, observes what you actually sent (or didn't), distils the correction,
and feeds it back into future drafting briefs so drafts improve over time.

**This is in-context learning-from-edits, not model fine-tuning.** Hermes owns
the drafting model; the plugin can't touch its weights. The signal shapes what
the *brief* contains: per-sender learned voice notes, global do/don't lessons,
and gold-example replies. All of it feeds `brief.build_draft_brief` at draft
time, fenced and labelled UNTRUSTED so it guides voice without becoming
executable instructions.

---

## How it works

1. **Draft capture.** When `inbox_create_draft` writes a reply draft to Gmail
   it also persists the draft body (truncated to a cap) to `draft_outcomes` in
   `state.db`, keyed on `(account, thread_id)`.

2. **Outcome pairing.** When you send a reply on a drafted thread, the
   `DraftFeedbackModule.on_sent` observer compares draft↔sent:
   - `sent_verbatim` — similarity ≥ `VERBATIM_THRESHOLD` (default 92): the
     draft landed as-is; reinforced without an LLM call.
   - `sent_edited` — similarity ≥ `EDIT_THRESHOLD` (default 45): you changed
     it; distilled via LLM to extract writing-style guidance.
   - `sent_ignored` — similarity below `EDIT_THRESHOLD`: you wrote a fresh
     reply; treated as a stronger edit signal.
   - `sent_no_draft` — you sent on a thread the plugin didn't draft; captured
     as a gold example when `capture_all_sent` is on (default).
   - `no_reply` — the draft window elapsed with no send observed; marked by the
     periodic sweep only when the mailbox was demonstrably live through the
     window (liveness-gated — see below).

3. **Distillation.** For `sent_edited` and `sent_ignored` the plugin calls the
   local LLM (via OpenRouter, the same path as the classifier). The draft and
   sent bodies are wrapped in **randomized fences** and the prompt instructs the
   model to extract only writing-style guidance — it may never follow
   instructions found inside the fences. The result is applied to a **separate
   learned layer** (`sender_profiles.learned_notes` + the `draft_lessons` table)
   that is auditable and revertible. The original backfill voice profile
   (`voice_notes`, `tone_hints`) is never overwritten.

4. **Brief injection.** The learned layer is injected into the drafting brief
   (fenced + bounded): a per-sender learned voice note, up to
   `max_lessons` global do/don't rules ranked by evidence count, and up to
   `max_examples` recent gold-example replies from that correspondent. Each
   block is fenced as UNTRUSTED to prevent prompt-injection from accumulated
   learned content.

5. **Periodic sweep.** Every `sweep_interval_s` (default 6 h) the sweep:
   - Marks `pending` rows older than `no_reply_hours` (default 72 h) as
     `no_reply` — **only** if the account is managed, not awaiting reconnect,
     AND was drained successfully after the draft was created
     (`accounts.updated_at_ms > draft_created_ms`). A token outage leaves rows
     `pending` rather than falsely marking `no_reply`.
   - Retries any distillation that failed on the `on_sent` path.
   - Prunes low-evidence lessons (soft-evicts beyond a store cap of 20) and
     deletes learned outcomes older than `retention_days` (default 90 days) to
     bound `draft_outcomes` growth.

---

## Configuration knobs (`INBOX_DRAFT_FEEDBACK_*`)

All knobs are optional — defaults shown. Set them in your Hermes `.env` or
`docker-compose.yml` environment block.

| Variable | Default | Meaning |
|---|---|---|
| `INBOX_DRAFT_FEEDBACK_ENABLED` | `true` | Enable/disable the entire loop. Set `false` to opt out without removing the plugin. |
| `INBOX_DRAFT_FEEDBACK_CAPTURE_ALL_SENT` | `true` | Capture gold-example replies on threads the plugin didn't draft (single-recipient sends only). Set `false` to record only drafted threads. |
| `INBOX_DRAFT_FEEDBACK_NO_REPLY_HOURS` | `72` | Hours after a draft is created before a non-replied thread is eligible to be marked `no_reply` (liveness-gated — see above). |
| `INBOX_DRAFT_FEEDBACK_SWEEP_INTERVAL_S` | `21600` | Seconds between sweep runs (default 6 h). |
| `INBOX_DRAFT_FEEDBACK_MAX_EXAMPLES` | `3` | Maximum gold-example replies injected into the drafting brief per correspondent. |
| `INBOX_DRAFT_FEEDBACK_MAX_LESSONS` | `8` | Maximum global do/don't lessons injected into the brief (ranked by evidence count). The store cap (20) is a separate module constant. |
| `INBOX_DRAFT_FEEDBACK_RETENTION_DAYS` | `90` | Delete `learned=1` outcome rows older than N days. Set `0` to disable retention pruning. |
| `INBOX_DRAFT_FEEDBACK_VERBATIM_THRESHOLD` | `92` | Similarity score (0–100) at or above which a sent reply is considered verbatim (no LLM, positive reinforcement only). Tune this if you edit drafts minimally before sending. |
| `INBOX_DRAFT_FEEDBACK_EDIT_THRESHOLD` | `45` | Similarity score at or above which a sent reply is `sent_edited` (LLM distil); below this → `sent_ignored` (stronger LLM distil). Tune to calibrate how aggressively the loop learns. |

---

## Owner tools — inspect and revert

The learned state is auditable and fully revertible by the owner via agent
tools.

### `inbox_draft_feedback_status`

Read-only. Returns:
- **`outcomes`** — a histogram of all outcome types (`pending`, `sent_verbatim`,
  `sent_edited`, `sent_ignored`, `no_reply`, `sent_no_draft`) with counts.
- **`active_lessons`** — the active global do/don't rules with their `lesson_id`,
  `polarity`, `rule` text, and `evidence_count`.
- **`learned_senders`** — which correspondents have a learned voice note, with
  note length and last-updated timestamp (note text is not echoed here; use
  `inbox_get_sender_profile` to read the full note).

Optional parameter: `account_id` to scope to one mailbox.

### `inbox_forget_lesson`

Soft-disables a global lesson by `lesson_id` (sets `active=0`). The lesson
stops influencing future drafts but is retained in the DB for reference. Use
`inbox_draft_feedback_status` to find the `lesson_id`.

Required parameter: `lesson_id` (integer).

*Owner-gated — blocked for non-owner callers.*

### `inbox_clear_learned_notes`

Clears the learned-from-edits voice note for one correspondent. The original
backfill `voice_notes` / `tone_hints` profile is left intact. Use this to undo
a note that was distilled from a bad or adversarial email.

Required parameters: `account_id`, `sender_email`.

*Owner-gated — blocked for non-owner callers.*

---

## Data and schema

All learned state lives in `state.db` (the SQLite DB in the Hermes data
volume, default `/opt/data/inbox-organizer/state.db`):

| Table / column | Contents |
|---|---|
| `draft_outcomes` | Draft body + sent body + outcome + similarity per `(account, thread_id)`. The gold-example store. |
| `draft_lessons` | Global learned do/don't rules; `evidence_count` tracks how many deltas support each. |
| `sender_profiles.learned_notes` | Per-sender learned voice note (separate column from `voice_notes` — never overwrites the backfill layer). |
| `sender_profiles.learned_updated_ms` | Timestamp of the last learned-note update for a sender. |

This is **schema v3**. On deploy the existing `state.db` migrates in place: the
two new tables (`draft_outcomes`, `draft_lessons`) are created automatically,
and the two new columns on `sender_profiles` are added via a guarded `ALTER
TABLE`. No data loss; a v2 image against a v3 DB simply ignores the new
tables/columns.

---

## Security notes

- Every email-derived body (draft and sent) is wrapped in **randomized fences**
  before being passed to the distillation LLM, and again when injected into the
  brief. The system prompt instructs the model never to follow instructions
  inside fences.
- The learned layer is kept strictly separate from the backfill voice profile.
  Even if a distilled note is adversarial, `inbox_clear_learned_notes` removes
  it without touching the underlying profile.
- No email body is ever logged. Only outcome labels, similarity scores, thread
  ids, and counts appear in the logs.
- See [security.md](./security.md) for the broader security model.
