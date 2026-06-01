# OAuth Modes — Testing vs Production Audience

This is the single most common gotcha for self-hosted operators: Google's OAuth **app audience** controls how long refresh tokens last.

---

## Testing mode (default after first consent-screen creation)

When your OAuth consent screen is in **Testing** mode:

- Refresh tokens expire after **7 days**.
- Only test users you explicitly list can grant access.
- Fine for initial setup and kicking the tires — dies after a week unless the user re-consents.

**Symptom:** Gmail sync stops with a "Token has been expired or revoked" error roughly seven days after OAuth connect.

---

## Production audience — unverified app

Publishing your app to **Production** audience removes the 7-day expiry, even without Google verification:

- Refresh tokens are long-lived (revoked only on explicit disconnect or Google security event).
- Any Google account can grant access, not just test users.
- Google shows a scary "This app isn't verified" interstitial to non-test users — that is expected and acceptable for single-operator, self-hosted use.

**This is the right mode for personal or small-team self-hosted deployments.**

### How to publish to Production

1. Open [Google Cloud Console](https://console.cloud.google.com/) and select your project.
2. Navigate to **APIs & Services → OAuth consent screen**.
3. Scroll to the **Publishing status** section.
4. Click **Publish App**.
5. Confirm the warning — Google will ask if you want to proceed without verification.
6. After publishing, any new OAuth grant will produce a long-lived refresh token.

Existing short-lived tokens from Testing mode are not automatically upgraded. Have each account re-connect via `/oauth/connect` to get a fresh long-lived token.

---

## Production audience — verified app

Verification removes the "unverified" warning and is required if:

- You are distributing to users outside your organization, **and**
- You want a clean consent screen without the scary interstitial.

Verification requires a Google security assessment for sensitive scopes like `gmail.modify`. This is out of scope for V1 self-hosted deployments.

---

## Summary

| Mode | Refresh token expiry | Required for |
|---|---|---|
| Testing | 7 days | Initial dev only |
| Production (unverified) | Long-lived | Self-hosted personal use |
| Production (verified) | Long-lived | Wide distribution, no warning |

For a single-operator self-hosted setup: publish to Production (unverified) and accept the one-time warning screen.
