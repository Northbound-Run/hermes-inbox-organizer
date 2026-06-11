# Setup — deploying the plugin

`hermes-inbox-organizer` is an **in-process Hermes plugin**, not a standalone
service. You deploy it by baking it into your Hermes agent's container image and
enabling it in the agent's config; there's no separate server, no public HTTPS
endpoint, and no setup wizard.

## Prerequisites

- A **Hermes** deployment you can rebuild the image for (e.g. the
  [`hermes-template`](https://github.com/Northbound-Run/hermes-template) Dockge stack).
- A **Google Cloud project** with the Gmail + Pub/Sub APIs, an OAuth client, and
  a Pub/Sub topic + **pull** subscription — see [google-bootstrap.md](./google-bootstrap.md).
- An **OpenRouter API key** — <https://openrouter.ai/keys> (for the LLM-fallback classifier).
- The owner's Matrix/Signal user id (the plugin is owner-gated).

## 1. Provision Google Cloud

Follow [google-bootstrap.md](./google-bootstrap.md): project, Gmail + Pub/Sub
APIs, OAuth **Web** client, a Pub/Sub topic, a **pull** subscription, the
`gmail-api-push@system` publisher grant, and a narrow `pubsub.subscriber`
service-account key for the plugin's streaming pull.

## 2. Drop the secrets into the config mount

The plugin reads everything from the read-only config mount (`/opt/data/config`
in the Hermes volume). Install these (owner `10000`, mode `400`):

| File | Contents |
|---|---|
| `inbox-oauth-client.json` | `{client_id, client_secret, redirect_uri, owner_matrix_ids: [...]}` |
| `inbox-pubsub.json` | `{project, topic, subscription}` |
| `inbox-pubsub-sa.json` | the `pubsub.subscriber` service-account key |
| `inbox-encryption-key` | 32 bytes hex — `openssl rand -hex 32` (AES-GCM for tokens at rest) |

`OPENROUTER_API_KEY` comes from the Hermes environment (`.env`). Optional knobs
are the `INBOX_*` env vars (see [hermes-wiring.md](./hermes-wiring.md)).

## 3. Bake the plugin into the image + enable it

In your Hermes image build (see `hermes-template/Dockerfile.hermes`):

```dockerfile
COPY hermes-inbox-organizer /opt/build/hermes-inbox-organizer
RUN /opt/hermes/.venv/bin/python -m pip install --no-cache-dir "/opt/build/hermes-inbox-organizer[live]" \
 && rm -rf /opt/build/hermes-inbox-organizer
```

(`[live]` pulls the Gmail/Pub/Sub/OpenRouter deps.) Hermes auto-discovers the
plugin via its `hermes_agent.plugins` entry point. Enable it in `config.yaml`:

```yaml
plugins:
  enabled:
    - inbox_organizer
```

A `gateway:startup` hook (in `deploy/hooks/`, installed into the
volume by the entrypoint) starts the daemon at boot so it doesn't wait for the
first agent turn. See [hermes-wiring.md](./hermes-wiring.md).

The entrypoint also projects the package's bundled `dashboard/` UI plugin into
`$HERMES_HOME/plugins/inbox_organizer/dashboard/` so the Hermes web dashboard shows
an **Inbox Organizer** tab (connect/remove accounts). `register()` self-projects it
too, so it also works on a plain `pip install`. See [dashboard.md](./dashboard.md).

## 4. Deploy

```sh
# rsync the package into the stack's build context, then:
docker compose build hermes && docker compose up -d hermes
```

## 5. Connect a Gmail account

Two ways — same copy-paste OAuth either way:

- **By chatting** — tell your Hermes agent *"Connect a Gmail account."* It calls
  `inbox_connect_account`, returns a Google consent link, you approve, the static
  callback page shows a code, you paste it back, and `inbox_complete_connection`
  stores the encrypted token and hot-adds the account. (Only the owner — per
  `owner_matrix_ids` — may run these tools.)
- **From the dashboard** — the **Inbox Organizer** tab in the Hermes web dashboard
  has Connect/Remove buttons. See [dashboard.md](./dashboard.md) for the auth model
  (the dashboard's own access control is the gate) and the dashboard-restart note.

## Verify

- Ask the agent to run `inbox_list_accounts` — it should list the connected account.
- Send yourself a test email; within seconds (push) or a few minutes (the polling
  reconciler) it gets a numbered label.

## Further reading

- [google-bootstrap.md](./google-bootstrap.md) — Google Cloud setup
- [oauth-modes.md](./oauth-modes.md) — Testing vs Production OAuth audience
- [sync-modes.md](./sync-modes.md) — how sync works (streaming pull + reconciler)
- [security.md](./security.md) — security model
- [dashboard.md](./dashboard.md) — the web-dashboard tab (connect/remove accounts)
- [hermes-wiring.md](./hermes-wiring.md) — config + the agent tool surface
