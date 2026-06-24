# Hermes self-install runbook

**This document is written for the Hermes agent to execute.** A human owner can
hand it to you and say *"read this and install + configure the inbox organizer
for me."* Your job is to install the `hermes-inbox-organizer` plugin into your own
deployment, wire up its config, and walk the owner through the parts only they
can do (the Google Cloud setup happens in their browser, under their Google
account — you cannot do that for them).

Work through the phases in order. After each phase, run its **Verify** step
before moving on. If a verify fails, fix it (see **Troubleshooting**) before
continuing — do not push past a broken step.

---

## Rules for you (the agent)

1. **Secrets.** You will handle an OAuth client secret, a service-account key, and
   an AES encryption key. Never echo a secret back to the user, never print one
   into your reply or any log, and never paste one into a chat message. Write
   them straight to files with `chmod 600`. When you must refer to one, refer to
   the *file*, not its contents.
2. **Confirm before irreversible or outward actions.** Ask the owner before
   anything that touches their Google account, sends data to Google, or restarts
   their Hermes. Approval for one step is not approval for the next.
3. **The encryption key is load-bearing.** Generate it once and keep it stable.
   If it is ever lost or changed, every stored Gmail token becomes undecryptable
   and all accounts must be reconnected. Never regenerate it on a reinstall.
4. **Idempotent.** Re-running a step should be safe. Check whether a file/dir/env
   already exists before overwriting it, and ask before clobbering.
5. **If you cannot run shell commands or write files** in this deployment, switch
   to *guide mode*: give the owner each exact command/file to run themselves and
   wait for them to confirm before the next step.

---

## Phase 0 — Preflight

**Goal:** locate the environment you're installing into.

1. Confirm your capabilities: can you run shell commands and write files? If not,
   use guide mode (see rule 5).
2. Find the Hermes CLI and its Python environment — the plugin **must** install
   into the same interpreter that runs `hermes`:
   ```sh
   which hermes
   head -1 "$(which hermes)"     # the shebang shows the python it uses
   ```
   Use that interpreter's `pip` for Phase 2 (e.g. `/opt/hermes/.venv/bin/pip`).
3. Determine the config + data roots. Defaults (under `HERMES_HOME=/opt/data`):
   - `INBOX_CONFIG_DIR` → `/opt/data/config` (secrets you install; read-only mount)
   - `INBOX_DATA_DIR` → `/opt/data/inbox-organizer` (SQLite DB + encrypted tokens; writable)

   If this deployment's `HERMES_HOME` differs, set both env vars in Phase 3.
4. Note the deployment shape, because it changes *how durable* the install is and
   *how* env vars are set:
   - **Docker / `docker compose` Hermes** (most common): a runtime `pip install`
     inside the container gets you running now, but is **wiped on the next image
     rebuild** (`docker compose up --build`). For a durable install, bake the
     package into the image instead — see [`docs/setup.md`](docs/setup.md). Set
     env vars in the service's `environment:` / `.env`.
   - **Bare / systemd Hermes:** the runtime install persists. Set env vars in the
     unit file or shell profile.

**Verify:** you can name (a) the pip to use, (b) the config dir, (c) the data
dir, and (d) how env vars get set for this deployment. Tell the owner these four
things before continuing.

---

## Phase 1 — Google Cloud (owner does this; you guide)

**Goal:** a Google Cloud project that lets the plugin read/label Gmail and
receive change notifications over Pub/Sub. **You cannot do this** — it needs the
owner's Google login and browser. Walk them through it one step at a time and
collect the values listed at the end. Full screenshots:
[`docs/google-bootstrap.md`](docs/google-bootstrap.md).

Tell the owner to:

1. **Project** — [console.cloud.google.com](https://console.cloud.google.com/) →
   create or pick a project (e.g. `hermes-inbox`).
2. **Enable APIs** — enable **Gmail API** (`gmail.googleapis.com`) and **Cloud
   Pub/Sub API** (`pubsub.googleapis.com`).
3. **OAuth consent screen** — *External*. Add the **`gmail.modify`** scope (that
   is the only scope the plugin actually requests — it covers reading, labeling,
   and writing drafts; the plugin never sends mail). Add the owner as a **Test
   User**, then **Publish App** to **Production**. Publishing matters: a "Testing"
   consent screen expires refresh tokens after **7 days**, which silently
   disconnects every account. (See [`docs/oauth-modes.md`](docs/oauth-modes.md).)
4. **OAuth client** — Credentials → Create OAuth client ID → **Web application**.
   For **Authorized redirect URI**, use a static page that just displays the auth
   code for copy-paste: reuse `https://inbox-organizer.northbound.run/` (it holds
   no secrets — the OAuth client stays theirs) or self-host the `oauth-callback/`
   page. Save the **client ID** and **client secret**.
5. **Pub/Sub** — create a **Topic** (e.g. `gmail-notifications`), then on it a
   **Subscription** with delivery type **Pull** (e.g. `gmail-inbox-organizer-pull`).
6. **Let Gmail publish** — on the topic → Permissions → add principal
   `gmail-api-push@system.gserviceaccount.com` with role **Pub/Sub Publisher**.
7. **Subscriber key** — create a service account, grant it
   **`roles/pubsub.subscriber`** *on the subscription* (not project-wide), and
   download its **JSON key**.

**Collect from the owner** (ask them to paste these to you; treat the last two as
secrets):
- OAuth **client_id**, **client_secret**, and the **redirect_uri** they registered
- Pub/Sub **project id**, **topic** (full path `projects/<id>/topics/<name>`), **subscription** name
- the **service-account key JSON** (a secret)
- a **classifier LLM API key** — OpenRouter
  ([openrouter.ai/keys](https://openrouter.ai/keys)) by default, or a key for any
  OpenAI-compatible endpoint you prefer (a secret)
- their **Hermes user ID** — the sender id the gateway reports for the channel
  they talk to Hermes on, used to authorize them as the owner who may connect
  mailboxes. It is **not** Matrix-specific (Matrix is just `@you:your-homeserver`;
  another backend uses that platform's id). If unsure, see "Finding the owner ID"
  in Phase 3.

**Verify:** you have all of the above. Do not proceed until the consent screen is
**Published / Production**.

---

## Phase 2 — Install + enable the plugin

**Goal:** the plugin is installed in the Hermes env and enabled.

```sh
# use the pip from Phase 0
<hermes-pip> install "hermes-inbox-organizer[live]"
hermes plugins enable inbox_organizer
```

`[live]` pulls the Gmail / Pub/Sub / OpenRouter dependencies. `enable` is needed
because pip/entry-point plugins are opt-in.

**Verify:**
```sh
hermes plugins list        # inbox_organizer → enabled
```
If it doesn't appear: `HERMES_PLUGINS_DEBUG=1 hermes plugins list`.

---

## Phase 3 — Write config + secrets

**Goal:** the four config files exist in `INBOX_CONFIG_DIR` and the required env
vars are set. Apply rule 1 throughout.

1. Create the dirs and lock them down:
   ```sh
   mkdir -p "$INBOX_CONFIG_DIR" "$INBOX_DATA_DIR"
   chmod 700 "$INBOX_CONFIG_DIR"
   ```
2. Write the OAuth client file — `$INBOX_CONFIG_DIR/inbox-oauth-client.json`:
   ```json
   {
     "client_id": "<client_id>",
     "client_secret": "<client_secret>",
     "redirect_uri": "https://inbox-organizer.northbound.run/",
     "owner_matrix_ids": ["@you:your-homeserver"]
   }
   ```
   `owner_matrix_ids` here is the **owner allowlist** — the Hermes user ID(s)
   permitted to connect/disconnect mailboxes. Despite the field name it is **not
   Matrix-only**: put whatever sender id your gateway reports for the owner's
   channel (Matrix `@you:server`, or another backend's id). Putting it in this
   file (rather than an env var) means it survives a container rebuild. (A Google
   `client_secret.json` with a `web`/`installed` node is also accepted in place of
   the flat shape.)

   **Finding the owner ID:** it's the gateway's `source.user_id` for the owner. On
   Matrix that's their `@user:homeserver`. If you're unsure on another backend,
   set it provisionally, attempt Phase 6, and if the connect is refused, check the
   Hermes gateway logs for the rejected sender id and use that exact value.
3. Write the Pub/Sub config — `$INBOX_CONFIG_DIR/inbox-pubsub.json`:
   ```json
   {
     "project": "<project-id>",
     "topic": "projects/<project-id>/topics/<topic-name>",
     "subscription": "<subscription-name>"
   }
   ```
4. Write the service-account key — `$INBOX_CONFIG_DIR/inbox-pubsub-sa.json` —
   verbatim from the JSON the owner downloaded.
5. Generate the AES encryption key (**once, then never change it** — see rule 3):
   ```sh
   openssl rand -hex 32 > "$INBOX_CONFIG_DIR/inbox-encryption-key"
   ```
6. Lock down every secret:
   ```sh
   chmod 600 "$INBOX_CONFIG_DIR"/inbox-*.json "$INBOX_CONFIG_DIR/inbox-encryption-key"
   ```
7. Set environment variables in the Hermes environment (method per Phase 0):
   - **Classifier LLM** — required for LLM classification. Default: set
     **`OPENROUTER_API_KEY`**. For any other OpenAI-compatible endpoint set
     **`INBOX_CLASSIFIER_API_KEY`** + **`INBOX_CLASSIFIER_BASE_URL`** instead
     (and optionally **`INBOX_CLASSIFIER_MODEL`**).
   - **Owner allowlist** — required so account-connect works. You already set it
     via `owner_matrix_ids` in the OAuth JSON (step 2); alternatively set
     **`INBOX_OWNER_MATRIX_IDS`** (comma-separated), which takes precedence. Both
     hold gateway sender ids (any backend, not just Matrix). The gate is
     **fail-closed**: with neither set, *every* connect attempt is refused,
     including the owner's.
   - `HERMES_API_URL` — *optional*; the local Hermes OpenAI-compatible endpoint.
     Needed only for drafted replies; triage + labeling run without it.
   - `API_SERVER_KEY` — *optional*; bearer token if the wake endpoint requires one.
   - `INBOX_CONFIG_DIR` / `INBOX_DATA_DIR` — only if you chose non-default paths.

**Verify (without printing secrets):**
```sh
ls -l "$INBOX_CONFIG_DIR"          # 4 files, all mode 600
python -c "import json,os; d=os.environ['INBOX_CONFIG_DIR']; \
  [json.load(open(os.path.join(d,f))) for f in ('inbox-oauth-client.json','inbox-pubsub.json','inbox-pubsub-sa.json')]; \
  print('json files parse OK')"
```

---

## Phase 4 — Start the daemon at boot (recommended)

**Goal:** the triage daemon starts on gateway boot, not just on the first agent
turn after a restart.

Hermes loads entry-point plugins lazily, so after a restart the daemon stays
dormant until someone next chats. A `gateway:startup` hook fixes that. The hook
lives in the repo at `deploy/hooks/inbox-organizer-boot/` and is **not** shipped
in the pip wheel, so create the two files yourself in the Hermes hooks dir
(usually `~/.hermes/hooks/inbox-organizer-boot/`):

`HOOK.yaml`:
```yaml
name: inbox-organizer-boot
description: Load entry-point plugins at gateway startup so the triage daemon starts at boot.
events:
  - gateway:startup
```

`handler.py`:
```python
async def handle(event_type, context):  # Hermes hook signature
    tag = "[inbox-organizer-boot]"
    try:
        from hermes_cli.plugins import discover_plugins
        print(f"{tag} {event_type}: loading entry-point plugins at boot", flush=True)
        discover_plugins()  # idempotent; runs each enabled plugin's register()
        print(f"{tag} discover_plugins() complete", flush=True)
    except Exception as exc:
        import traceback
        print(f"{tag} FAILED to load plugins at startup: {exc!r}", flush=True)
        traceback.print_exc()
```

(If you have repo access, copying `deploy/hooks/inbox-organizer-boot/` verbatim is
equivalent and includes a daemon-thread health log.)

**Verify:** both files exist under the hooks dir.

---

## Phase 5 — Restart + verify load

**Goal:** Hermes restarts with the new env + plugin and the daemon arms.

1. Ask the owner before restarting (rule 2), then restart Hermes the way this
   deployment does (`docker compose up -d hermes`, `systemctl restart hermes`, …).
2. Verify:
   ```sh
   hermes plugins list        # inbox_organizer → enabled
   ```
3. In a chat session, confirm `/plugins` lists it as loaded, then call the
   `inbox_list_accounts` tool — it should return an empty list (no accounts yet),
   which proves the tools are wired and config loaded without error.

**If `inbox_list_accounts` errors about config**, re-check Phase 3 (file paths,
JSON validity, env vars) before connecting an account.

---

## Phase 6 — Connect the owner's Gmail

**Goal:** the owner's mailbox is connected and triaged.

1. Call **`inbox_connect_account`**. It returns a Google sign-in `auth_url`.
2. Give the owner the link. Tell them: approve access; if Google shows a
   *"hasn't verified this app"* screen, choose **Advanced → Continue** (expected
   for their own client); then copy the **code shown on the callback page**.
3. When they paste the code back, call **`inbox_complete_connection`** with
   `{ "code": "<pasted code>" }`. A success looks like
   `{"ok": true, "email": "...", "live": true}`.
   - `live: true` → the account was hot-added to the running daemon.
   - `live: false` → the token is saved; it will be picked up on the next restart.
   - These tools are **owner-gated**; if you get *"restricted to this assistant's
     owner,"* the caller's gateway user ID isn't in the allowlist — fix Phase 3 step 7.
4. Repeat for any additional mailboxes.

**Verify:**
- `inbox_list_accounts` now lists the connected address.
- Ask the owner to send themselves a test email; it should get a numbered label
  within seconds (Pub/Sub push) or a few minutes (the polling reconciler).

---

## Phase 7 — Hand back to the owner

Tell the owner, in plain language:

- What's running: each new email is classified and labeled `1: To Respond` …
  `8: Marketing`; only *To Respond* and *FYI* stay in the inbox, the rest archive.
  For *To Respond*, you'll draft a reply in their voice (**drafts only — never
  sent**).
- How to add/remove mailboxes later: just ask you to *"connect another Gmail"* or
  *"disconnect <email>"*.
- The kill switch: set `INBOX_LABELS_ENABLED=0` to keep classification + drafting
  but stop all label/archive/move mutations.
- Proactive pushes (2FA codes, shipping updates — if those modules are enabled) go
  to your Hermes **home channel** on whatever platform you use. Run `/sethome` in
  the room you want them delivered to, or set `INBOX_NOTIFY_TARGET` to override.
- Where state lives: `INBOX_DATA_DIR` (SQLite `state.db` + encrypted tokens). Back
  up the **encryption key** somewhere safe — losing it means reconnecting every
  account.
- If this is a Docker deployment and you did a runtime `pip install`, remind them
  to bake the package into the image for durability ([`docs/setup.md`](docs/setup.md)).

---

## Troubleshooting

- **Plugin not in `hermes plugins list`** — installed into the wrong Python env
  (Phase 0); reinstall with the `hermes` CLI's own pip. Then
  `HERMES_PLUGINS_DEBUG=1 hermes plugins list`.
- **Loads but no triage after restart** — the boot hook (Phase 4) is missing, or
  the daemon didn't arm. Check Hermes stdout for `[inbox-organizer-boot]` lines.
- **`OAuth client not configured`** — `inbox-oauth-client.json` is missing,
  unreadable, or lacks `client_id`/`client_secret`/`redirect_uri`.
- **`no refresh_token in token response`** — the consent screen isn't granting
  offline access; ensure the app is Published and the owner did a fresh consent.
- **Accounts disconnect every ~7 days** — the consent screen is still in
  "Testing." Publish it to Production and reconnect ([`docs/oauth-modes.md`](docs/oauth-modes.md)).
- **`restricted to this assistant's owner`** — the owner allowlist is empty or
  doesn't include the caller's gateway user ID (Phase 3 step 7).
- **No labels appear** — confirm `INBOX_LABELS_ENABLED` isn't `0`, and that the
  Pub/Sub subscriber key has `roles/pubsub.subscriber` on the subscription.

For deeper detail see [`docs/setup.md`](docs/setup.md), [`docs/sync-modes.md`](docs/sync-modes.md),
and [`docs/security.md`](docs/security.md).
