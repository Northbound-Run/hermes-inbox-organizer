# Sync — streaming pull + polling reconciler

The plugin has **one** sync design (no push-vs-polling toggle, no webhook). Gmail
`watch()` publishes change notifications to a Pub/Sub topic; the plugin consumes
them via a **streaming pull** subscription — an *outbound* gRPC connection, so
there's no public HTTPS endpoint, no Cloudflare tunnel, and no OIDC verification.

## The loop

1. **Watch** — on startup each account arms `watch()` on `INBOX` + `SENT`
   (`runtime.start`). A renewal thread (`inbox-watch-renewal`) re-arms before the
   ~7-day expiry so sync never silently stops.
2. **Pull** — a streaming-pull subscriber receives notifications
   (`FlowControl(max_messages=1)` + a reentrant lock so callbacks can't
   double-drain). Each notification triggers a drain.
3. **Drain** — `drain_history` pulls Gmail history *since the stored cursor*,
   classifies + labels each new INBOX message, and handles SENT messages
   (Actioned / Awaiting Reply). The cursor (`accounts.history_cursor` in
   `state.db`) advances **only after** a successful drain, so a crash re-drains
   rather than skips (at-least-once).

## The reconciler (why no mail is lost)

Streaming pull is best-effort — Gmail can drop or delay a notification. So a
polling reconciler thread (`inbox-poll-reconciler`) re-drains every account from
its stored cursor every `POLL_INTERVAL_S` (default **300s**), serialized with
live notifications via the same lock. It's cursor-based, so it never
reprocesses — it just catches whatever a missed push left behind.

Net effect: pushes give near-real-time triage (seconds); the reconciler
guarantees eventual catch-up within ~5 minutes even if a push is lost.

## Stale cursor

If the stored cursor is older than Gmail's history retention (~1 week of no
sync), `drain_history` raises `StaleCursor` and the drain resets forward to the
latest history id rather than erroring.

## Credentials

- Per-account **OAuth** token (for `watch()` + history + label/draft writes),
  AES-GCM encrypted at rest.
- One narrow **`pubsub.subscriber`** service-account key for the streaming-pull
  subscription (NOT domain-wide delegation, NOT a push OIDC account). See
  [google-bootstrap.md](./google-bootstrap.md).
