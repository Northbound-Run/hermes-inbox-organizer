# Hermes Plugin Standard

Shared conventions for the in-process **Hermes plugins** we (Northbound) build and
maintain. This is the canonical reference; it is committed verbatim to each plugin
repo so they stay in sync.

Current adopters:
- [`hermes-inbox-organizer`](https://github.com/Northbound-Run/hermes-inbox-organizer)
- [`hermes-chat-recorder`](https://github.com/Northbound-Run/hermes-chat-recorder)

A plugin is conformant if it follows the **MUST** items below. **SHOULD** items
are strong defaults; deviate only with a comment explaining why.

---

## 1. Identity & metadata

- **MUST** be MIT licensed, with a `LICENSE` file at the repo root.
- **MUST** declare PEP 639 license metadata in `pyproject.toml`:
  `license = "MIT"` + `license-files = ["LICENSE"]`, and
  `requires = ["setuptools>=77"]` in `[build-system]` (Metadata-Version 2.4).
- **MUST** set author to `Northbound <matthall28@gmail.com>`.
- **MUST** own the repo under the `Northbound-Run` GitHub org; use that exact
  casing in every URL (`[project.urls]`, badges, docs).
- **SHOULD** set `[project.urls]` Homepage, Repository, Issues, Documentation.
- **SHOULD** keep `classifiers` Development Status honest
  (`4 - Beta` once running in production; `3 - Alpha` while the schema/API churns).

## 2. Python & packaging

- **MUST** target **Python 3.11+** (`requires-python = ">=3.11"`).
- **MUST** test against the matrix **3.11, 3.12, 3.13, 3.14**.
- **MUST** be installable from PyPI as `hermes-<name>` and expose the Hermes
  entry point (see §3).
- **MUST** ship the plugin manifest + any runtime assets as `package-data` (so a
  non-editable `pip install` includes them), and **assert in CI** that they land
  in the built wheel.
- Dependencies: **SHOULD** stay standard-library-first. Pull a third-party dep
  only when it earns its place (e.g. `cryptography` for at-rest encryption,
  `google-cloud-pubsub` for streaming pull). Keep the heavy/live deps behind an
  optional extra (e.g. `[live]`) when the unit tests don't need them.

## 3. Discovery, layout & the entry point

Hermes discovers plugins two ways. Which you support drives your layout.

- **Entry-point (pip)** — `pip install` exposes the `hermes_agent.plugins`
  entry-point group; Hermes imports the module and calls its `register`.
- **Directory (`hermes plugins install <owner>/<repo>`)** — git-clones the repo
  into `~/.hermes/plugins/<name>/` and execs the repo-root module. This path runs
  **no `pip install`**, so it only works for **stdlib-only** plugins.

Rules:

- **MUST** declare the entry point as the **module path only** — no `:register`
  suffix. Hermes does `getattr(module, "register")`; pointing at the function
  makes `register` resolve to `None`.
  ```toml
  [project.entry-points."hermes_agent.plugins"]
  <name> = "hermes_<name>"
  ```
- **MUST** expose a top-level `register(ctx)` and keep `__init__.py` **thin** — a
  docstring, `__version__`, and a re-export/delegation to the real
  implementation in `plugin.py`. Business logic does **not** live in `__init__`.
- **Layout — pick by install model:**
  - *Stdlib-only plugin that supports the one-line directory install* — **MUST**
    use a `src/hermes_<name>/` layout plus a repo-root `__init__.py` shim that
    puts `src/` on `sys.path` and re-exports `register` (so the directory loader
    finds a working `register` at the clone root). The pip path ignores the shim.
  - *Plugin with compiled/heavy deps (pip-only)* — **SHOULD** use a flat
    `hermes_<name>/` package at the repo root. A `src/` move buys nothing here
    (the directory install can't work anyway) and only churns packaging. The
    thin-`__init__` + `plugin.py` split from above still applies.
- **MUST** keep a `plugin.yaml` manifest. If both a packaged copy
  (`hermes_<name>/plugin.yaml`) and a root copy exist (directory installs read
  the root), keep them in sync and say so in a comment.

## 4. Configuration & secrets

- **MUST** resolve every setting through a single config module
  (`config.py` with a cached accessor), not scattered `os.environ.get(...)`.
- **MUST** read **secrets as files** from a read-only config mount (encryption
  keys, OAuth client JSON, service-account keys) — never from the repo, never
  committed.
- **SHOULD** namespace env vars with a per-plugin prefix (e.g. `INBOX_*`), and
  prefer endpoint-agnostic config: an OpenAI-compatible LLM should take a
  `*_BASE_URL` + `*_API_KEY`, not hardcode one provider.
- **SHOULD** ship a `setup`/`status` CLI (a `[project.scripts]` console script,
  also runnable via `python -m hermes_<name>`) that writes the config files
  (mode `0600`), generates keys, and reports configured capabilities — **never
  echoing secret values**.

## 5. Security

- **MUST NOT** log tokens, secrets, or PII.
- **MUST** encrypt credentials at rest (AES-256-GCM) with an operator-supplied
  key when the plugin stores third-party tokens.
- **MUST** fence untrusted content (email bodies, chat messages) before it
  reaches any LLM prompt, so it can't steer the model.
- **SHOULD** prefer outbound connections over any inbound/public surface.
- **SHOULD** owner-gate privileged tools against the gateway-reported
  `source.user_id` (backend-agnostic — not Matrix-specific), and fail closed.

## 6. Tooling

- **MUST** lint with **ruff**, config in `pyproject.toml`:
  ```toml
  [tool.ruff]
  line-length = 100
  target-version = "py311"
  [tool.ruff.lint]
  select = ["E", "F", "I", "W", "UP", "B", "SIM", "RUF"]
  ```
  Relax test-only noise via `[tool.ruff.lint.per-file-ignores]` rather than
  weakening the global set.
- **MUST** type-check with **mypy** and ship `py.typed`. A non-strict config is
  fine; silence unavoidable missing stubs for lazily-imported live deps with
  per-module `ignore_missing_imports`, not blanket ignores.
- **MUST** test with **pytest**, and tests **MUST run fully offline** — network,
  Google/cloud, and agent calls sit behind injectable seams. Preserve the seams.

## 7. CI

- **MUST** use `actions/checkout@v5` + `actions/setup-python@v6` (Node-20 actions
  are EOL).
- **MUST** run, on push to `main` + PRs:
  1. `ruff check .`
  2. `pytest -q` across the 3.11–3.14 matrix
  3. build sdist+wheel, `twine check`, and **assert the manifest/assets are in
     the wheel** (`unzip -l dist/*.whl | grep -q hermes_<name>/plugin.yaml`).

## 8. Release

- **MUST** publish to PyPI via **OIDC Trusted Publishing** — no API tokens in the
  repo. Use a `pypi` (and `testpypi`) GitHub Environment.
- **MUST** trigger on a pushed **`vX.Y.Z` tag**, and the build job **MUST assert
  the tag matches `project.version`** before publishing (a forgotten bump fails
  loudly instead of shipping the wrong version).
- **SHOULD** offer a `workflow_dispatch` **TestPyPI dry-run** target.
- **MUST** follow SemVer, keep `pyproject.version` and `__version__` in sync, and
  cut a **GitHub Release** whose notes are the CHANGELOG section.

## 9. Docs & community health

- **MUST** have a `README.md` shaped: one-line value prop + badges (CI, PyPI,
  Python, License) → **Why this exists** → **Quick Start** (copy-paste) →
  **Updating** → **Documentation** → reference sections.
- **MUST** keep a `CHANGELOG.md` (Keep a Changelog + SemVer).
- **MUST** have `CONTRIBUTING.md`, `SECURITY.md` (private vulnerability
  reporting), `.github/ISSUE_TEMPLATE/` (bug + feature + `config.yml` routing
  security reports privately), and `.github/PULL_REQUEST_TEMPLATE.md`.
- **SHOULD** document the release process (`docs/releasing.md`) and, where the
  setup is involved, ship an agent-executable `HERMES_SELF_INSTALL.md` so a
  Hermes agent can install + configure the plugin itself.

---

*Changes to this standard should be made in one repo and copied to the others in
the same change set, so the canonical copies never drift.*
