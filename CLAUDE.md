**hermes-inbox-organizer** — autonomous Gmail triage packaged as an in-process **Hermes plugin** (Python). It loads into a Hermes agent (`NousResearch/hermes-agent`) via the `hermes_agent.plugins` entry point, runs a continual Pub/Sub streaming-pull daemon that classifies + labels new mail, and asks Hermes to draft replies for `1: To Respond`.

The plugin package is `hermes_inbox_organizer/` at the repo root (the production plugin). Tests live in `tests/`.

> An earlier Bun/TypeScript MCP-over-HTTP implementation was the porting reference; it was removed once the Python port reached parity (recoverable from git history). The Python plugin is the only implementation now.

## Language & tooling
- Python 3.11+ (runs on 3.13 in the Hermes image). Standard-library first.
- Run tests: `.venv/bin/python -m pytest -q` from the repo root (venv: `uv venv && uv pip install -e ".[dev]"`).
- No build step — it's a `pip install`-able package (`pyproject.toml`); Hermes auto-discovers it via the entry point `inbox_organizer = "hermes_inbox_organizer"`.

## Conventions
- **Config**: read every `INBOX_*` setting through `config.get_config()` (a cached `Config`). Don't scatter `os.environ.get(...)` in new code.
- **Database**: SQLite via `db.py` (`db.connect()` returns a connection with the schema applied; use the typed accessors). Don't open raw connections elsewhere. Email-keyed, single-owner; the DB is `<INBOX_DATA_DIR>/state.db` inside the Hermes `/opt/data` volume.
- **Logging**: stdlib `logging` (`logging.getLogger(__name__)`); never log tokens/PII. (The gateway boot hook deliberately uses `print` because the runtime logger isn't on docker stdout.)
- **Mutations**: label apply + sent-handling run on the daemon path (`runtime` → `triage`/`sent_handler` → `labels_apply`), serialized by the runtime's `RLock`. Hermes composes draft *bodies*; the plugin only triggers the agent and writes the MIME draft via `inbox_create_draft`.
- Tests run without Hermes or live Google — seams keep network/agent calls out of unit tests; preserve that.

## Architecture
- `register(ctx)` wires the agent tools + a `pre_llm_call` nudge hook and starts the daemon. A `gateway:startup` hook (`deploy/hooks/`) loads the plugin at boot so the daemon doesn't wait for the first agent turn.
- Daemon: Gmail `watch()` → Pub/Sub **streaming pull** → drain history from the stored cursor → pre-classifier + OpenRouter LLM → apply numbered label → (`1: To Respond`) wake Hermes to draft. A **polling reconciler** re-drains on a timer in case a push is dropped.
- 8 Fyxer-style numbered labels (`1: To Respond` … `8: Marketing`); only To Respond + FYI stay in the inbox, 3–8 skip-inbox + archive. The sent-handler moves a thread to `7: Actioned` (you replied) or `6: Awaiting Reply` (you sent + are waiting).

## Security invariants
- Untrusted email content is wrapped in randomized fences before any LLM prompt (`classifier.py`) — never let it steer the model.
- OAuth tokens are AES-256-GCM encrypted at rest (`crypto.py` / `token_store.py`); never log them.
- Secrets (encryption key, OAuth client JSON, Pub/Sub SA key) live in the read-only config mount, never in the repo.

## Deploy
Baked into the Hermes image via the **`hermes-template`** repo (`Dockerfile.hermes` COPYs the package + `pip install ".[live]"`; the entrypoint installs the boot hook into the volume). Deploy = rsync the package (`pyproject.toml`, `hermes_inbox_organizer/`, `deploy/`, `README.md`) into the stack's build context (`hermes-inbox-organizer/`), then `docker compose build hermes && docker compose up -d hermes`. History cursors + draft ledger persist in `state.db` in the volume.

The entrypoint also projects a **dashboard-visibility shim** (`deploy/dashboard-shim/`) into `$HERMES_HOME/plugins/inbox_organizer/` at boot. Hermes's web dashboard Plugins page lists only plugins it finds as *directories* there (its `_discover_all_plugins` scan never enumerates entry-points), so without this the plugin runs but is invisible in the UI. The shim's `plugin.yaml` is copied from the *installed* package (no drift); its `__init__.py` re-exports `register` as dormant insurance (the entry-point wins dedup, so it's normally never imported). Same volume-shadowing reason as the boot hook — staged in the image, copied into the volume each boot. Cosmetic only: it adds a Plugins-page row; the plugin already loads via its entry-point regardless.
