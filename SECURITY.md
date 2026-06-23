# Security Policy

hermes-inbox-organizer handles OAuth access to your Gmail and the contents of
your mail, so security reports are taken seriously.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through GitHub's
[private vulnerability reporting](https://github.com/Northbound-Run/hermes-inbox-organizer/security/advisories/new)
(Security → Report a vulnerability). Include the affected version, a description,
and reproduction steps if you have them. You'll get an acknowledgement, and a
fix or mitigation plan once the report is triaged.

Please give a reasonable window to address the issue before any public
disclosure.

## Supported versions

This project is pre-1.0; only the latest released version receives security
fixes.

| Version | Supported |
|---|---|
| 0.1.x   | ✅ |
| < 0.1   | ❌ |

## Security model

The design assumptions a reviewer should know:

- **Tokens at rest** — OAuth refresh/access tokens are AES-256-GCM encrypted with
  a key supplied via the read-only config mount (`crypto.py` / `token_store.py`).
  Tokens are never logged.
- **Secrets are never in the repo** — the encryption key, OAuth client JSON, and
  Pub/Sub service-account key live only in the deployment's config mount.
- **No inbound surface** — Gmail change notifications arrive over an *outbound*
  Pub/Sub streaming pull. There is no webhook, public endpoint, or tunnel to
  attack.
- **Prompt-injection containment** — untrusted email content is wrapped in
  randomized fences before it reaches any LLM (`classifier.py`), so a crafted
  message cannot steer classification or drafting.
- **Drafts only** — the plugin composes draft replies and writes them to Gmail;
  it never sends mail on your behalf.
- **Owner-gating** — account connect/disconnect is gated to the configured owner.

If you find a gap in any of these, that's exactly the kind of report we want.
