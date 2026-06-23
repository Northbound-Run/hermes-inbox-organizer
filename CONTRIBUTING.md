# Contributing

Thanks for your interest in hermes-inbox-organizer. This is a single Python
package (`hermes_inbox_organizer/`) that loads into a [Hermes](https://hermes-agent.nousresearch.com)
agent as a plugin. Issues and pull requests are welcome.

## Development setup

```sh
uv venv && uv pip install -e ".[dev]"
.venv/bin/python -m pytest -q     # 419 tests, no Hermes or live Google needed
uvx ruff check                    # lint
```

The unit tests run entirely offline — network, Google, and agent calls sit behind
seams that the tests stub. **Preserve those seams**: a change that makes the
suite require live credentials or a running Hermes won't be accepted.

## Conventions

These mirror `CLAUDE.md`, which is the source of truth:

- **Config** — read every `INBOX_*` setting through `config.get_config()`. Don't
  scatter `os.environ.get(...)` in new code.
- **Database** — SQLite via `db.py`. Use `db.connect()` and the typed accessors;
  don't open raw connections elsewhere.
- **Logging** — stdlib `logging` (`logging.getLogger(__name__)`). **Never log
  tokens, OAuth secrets, or email contents/PII.**
- **Security** — untrusted email content is wrapped in randomized fences before
  any LLM prompt (`classifier.py`); keep it that way. OAuth tokens are
  AES-256-GCM encrypted at rest (`crypto.py` / `token_store.py`).
- **Drafts** — the plugin only triggers the agent and writes the MIME draft. It
  never sends mail.

## Pull requests

1. Fork and branch from `main`.
2. Keep changes focused; add or update tests for behavior changes.
3. Make sure `pytest` and `ruff check` both pass.
4. Use [Conventional Commit](https://www.conventionalcommits.org/) subjects to
   match the history, e.g. `feat(classifier): …`, `fix(dashboard): …`,
   `docs(readme): …`.
5. Note any user-facing change in `CHANGELOG.md` under `## [Unreleased]`.

## Reporting bugs / security issues

Use the [issue templates](https://github.com/Northbound-Run/hermes-inbox-organizer/issues/new/choose)
for bugs and feature requests. For anything security-sensitive, **do not open a
public issue** — follow [SECURITY.md](SECURITY.md) instead.
