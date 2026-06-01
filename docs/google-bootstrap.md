# Google Cloud Setup

What the plugin needs from Google Cloud: a project with the **Gmail** + **Pub/Sub**
APIs, an OAuth **Web** client, and a Pub/Sub **topic + pull subscription** (plus a
narrow service-account key the plugin uses to authenticate the streaming pull).

## 1. Create the Cloud project

1. [console.cloud.google.com](https://console.cloud.google.com/) → project selector → **New Project**.
2. Name it (e.g. `hermes-inbox`), **Create**, and select it.

## 2. Enable APIs

**APIs & Services → Library** — enable both (both are required; the plugin uses
Pub/Sub for its streaming pull):

- **Gmail API** (`gmail.googleapis.com`)
- **Cloud Pub/Sub API** (`pubsub.googleapis.com`)

## 3. OAuth consent screen

1. **APIs & Services → OAuth consent screen** → **External** → **Create**.
2. App name + your support/developer email.
3. **Scopes** → add:
   - `https://www.googleapis.com/auth/gmail.modify`
   - `https://www.googleapis.com/auth/gmail.send`
   - `https://www.googleapis.com/auth/userinfo.email`
   - `https://www.googleapis.com/auth/userinfo.profile`
4. Add your Gmail as a **Test User**, then **Publish App** (Production audience)
   to remove the 7-day refresh-token expiry — see [oauth-modes.md](./oauth-modes.md).

## 4. OAuth client (Web)

1. **APIs & Services → Credentials → Create Credentials → OAuth client ID → Web application**.
2. **Authorized redirect URI**: a static callback page that displays the OAuth
   code for you to paste back to Hermes. The page holds no secrets, so you can
   either host the one in `oauth-callback/` yourself or reuse the shared instance
   at `https://inbox-organizer.northbound.run/`. Either way the OAuth client
   itself (the client id/secret below) must be **your own** — add whichever
   redirect URI you use to *this* client.
3. Put the client id/secret + redirect into the config mount as
   `inbox-oauth-client.json`:
   ```json
   { "client_id": "...", "client_secret": "...",
     "redirect_uri": "https://inbox-organizer.northbound.run/",
     "owner_matrix_ids": ["@you:your-homeserver"] }
   ```

## 5. Pub/Sub topic + pull subscription

1. **Pub/Sub → Topics → Create Topic**, e.g. `gmail-notifications`. Note the full
   name `projects/<project-id>/topics/gmail-notifications`.
2. On the topic, **Create Subscription** → delivery type **Pull** (not Push —
   the plugin connects outbound, no webhook), e.g. `gmail-inbox-organizer-pull`.
3. Record both in the config mount as `inbox-pubsub.json`:
   ```json
   { "project": "<project-id>", "topic": "projects/<project-id>/topics/gmail-notifications",
     "subscription": "gmail-inbox-organizer-pull" }
   ```

## 6. Let Gmail publish to the topic

Gmail's `watch()` publishes via a Google-managed account. Grant it on the topic:

- **Pub/Sub → Topics →** your topic **→ Permissions → Add Principal**
- Principal: `gmail-api-push@system.gserviceaccount.com`
- Role: **Pub/Sub Publisher**

## 7. Subscriber service-account key (for the plugin)

The plugin authenticates the streaming pull with its own narrow key — **not**
domain-wide delegation:

1. **IAM & Admin → Service Accounts → Create**, e.g. `hermes-inbox-pubsub`.
2. Grant it **`roles/pubsub.subscriber`** scoped to the subscription (on the
   subscription's **Permissions**), not project-wide.
3. Create a JSON key for it and install it in the config mount as
   `inbox-pubsub-sa.json`.

That's everything Google-side; the plugin reads all of it from the config mount.
