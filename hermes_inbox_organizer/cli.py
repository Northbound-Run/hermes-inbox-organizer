"""Standalone setup/status CLI for hermes-inbox-organizer.

Installed as the ``hermes-inbox-organizer`` console script (see
``[project.scripts]``) and runnable as ``python -m hermes_inbox_organizer``. It
turns the file-based config contract (see :mod:`.config`) into an interactive
wizard, so an operator doesn't hand-write the JSON config + generate keys.

Security: secret values are never echoed back or printed; secret files are
written mode ``0600``. The pure helpers (``write_config``, ``compute_status``,
``render_status``, ``normalize_topic``) are unit-tested without prompting.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
from collections.abc import Callable, Mapping
from typing import Any

from . import crypto
from .config import Config, get_config, reset_config

SECRET_MODE = 0o600
DIR_MODE = 0o700
DEFAULT_REDIRECT = "https://inbox-organizer.northbound.run/"
DEFAULT_LLM_BASE_URL = "https://openrouter.ai/api/v1"  # keep in sync with config.py
DEFAULT_CLASSIFIER_MODEL = "google/gemini-2.5-flash-lite"  # keep in sync with config.py
ENV_FILE = "inbox.env"
GOOGLE_BOOTSTRAP = (
    "https://github.com/Northbound-Run/hermes-inbox-organizer/blob/main/docs/google-bootstrap.md"
)


# --- pure helpers (unit-tested) ---------------------------------------------

def normalize_topic(topic: str, project: str) -> str:
    """A bare topic name becomes ``projects/<project>/topics/<name>``."""
    topic = (topic or "").strip()
    if not topic or topic.startswith("projects/"):
        return topic
    return f"projects/{project}/topics/{topic}"


def _read_json(path: str) -> tuple[bool, dict | None]:
    """(present, parsed). ``parsed`` is None when present-but-invalid or absent."""
    try:
        with open(path) as f:
            return True, json.load(f)
    except FileNotFoundError:
        return False, None
    except (OSError, ValueError):
        return True, None


def _write_secret(path: str, content: str) -> None:
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, SECRET_MODE)


def write_config(
    cfg: Config,
    *,
    oauth: dict,
    pubsub: dict,
    sa_key_text: str,
    env_vars: dict | None = None,
) -> list[str]:
    """Write the config files into ``cfg.config_dir``; return the written paths.

    The encryption key is generated only when **absent** — never regenerated, as
    a new key makes every stored token undecryptable. ``env_vars`` (env-only
    settings such as the classifier key/endpoint and ``HERMES_API_URL``) are
    written to an ``inbox.env`` the operator wires into the Hermes environment.
    """
    os.makedirs(cfg.config_dir, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(cfg.config_dir, DIR_MODE)

    written: list[str] = []
    _write_secret(cfg.oauth_client_file, json.dumps(oauth, indent=2) + "\n")
    written.append(cfg.oauth_client_file)
    _write_secret(cfg.pubsub_config_file, json.dumps(pubsub, indent=2) + "\n")
    written.append(cfg.pubsub_config_file)
    _write_secret(cfg.sa_key_file, sa_key_text)
    written.append(cfg.sa_key_file)
    if not os.path.exists(cfg.key_file):
        _write_secret(cfg.key_file, crypto.generate_key())
        written.append(cfg.key_file)

    if env_vars:
        env_path = os.path.join(cfg.config_dir, ENV_FILE)
        _write_secret(env_path, "\n".join(f"{k}={v}" for k, v in env_vars.items()) + "\n")
        written.append(env_path)
    return written


def compute_status(cfg: Config, env: Mapping[str, str] | None = None) -> dict:
    """Inspect the on-disk config + environment. Captures NO secret values."""
    env = os.environ if env is None else env
    oauth_present, oauth = _read_json(cfg.oauth_client_file)
    pubsub_present, pubsub = _read_json(cfg.pubsub_config_file)
    sa_present, sa = _read_json(cfg.sa_key_file)

    key_present = os.path.exists(cfg.key_file)
    key_valid = False
    if key_present:
        try:
            with open(cfg.key_file) as f:
                key_valid = len(bytes.fromhex(f.read().strip())) == 32
        except (OSError, ValueError):
            key_valid = False

    node = (oauth or {}).get("web") or (oauth or {}).get("installed") or (oauth or {})
    redirect = (
        env.get("INBOX_OAUTH_REDIRECT_URI")
        or node.get("redirect_uri")
        or (node.get("redirect_uris") or [None])[0]
    )

    # owners: env first, then the oauth file — mirrors __init__._load_owners.
    owners = {s.strip() for s in env.get("INBOX_OWNER_MATRIX_IDS", "").split(",") if s.strip()}
    if not owners:
        owners = {str(i).strip() for i in (node.get("owner_matrix_ids") or []) if str(i).strip()}

    classifier_key = bool(env.get("INBOX_CLASSIFIER_API_KEY") or env.get("OPENROUTER_API_KEY"))
    classifier_base = env.get("INBOX_CLASSIFIER_BASE_URL", DEFAULT_LLM_BASE_URL)
    hermes_api = bool(env.get("HERMES_API_URL"))

    files: dict[str, dict[str, Any]] = {
        "oauth_client": {
            "present": oauth_present,
            "valid": oauth is not None,
            "client_id": bool(node.get("client_id")),
            "client_secret": bool(node.get("client_secret")),
            "redirect_uri": redirect,
        },
        "pubsub": {
            "present": pubsub_present,
            "valid": pubsub is not None,
            "project": (pubsub or {}).get("project"),
            "topic": (pubsub or {}).get("topic"),
            "subscription": (pubsub or {}).get("subscription"),
        },
        "sa_key": {"present": sa_present, "valid": sa is not None},
        "encryption_key": {"present": key_present, "valid": key_valid},
    }
    files_ok = bool(
        files["oauth_client"]["client_id"]
        and files["oauth_client"]["client_secret"]
        and redirect
        and pubsub is not None
        and sa is not None
        and key_valid
    )
    capabilities = {
        "mailbox_sync": files_ok,
        "llm_classification": classifier_key,
        "drafted_replies": hermes_api,
        "account_connect": bool(node.get("client_id")) and len(owners) > 0,
    }
    return {
        "config_dir": cfg.config_dir,
        "data_dir": cfg.data_dir,
        "files": files,
        "owners": len(owners),
        "env": {
            "classifier_api_key": classifier_key,
            "classifier_base_url": classifier_base,
            "hermes_api_url": hermes_api,
        },
        "capabilities": capabilities,
    }


def _chk(ok: bool) -> str:
    return "x" if ok else " "


def _yn(ok: bool) -> str:
    return "yes" if ok else "no"


def render_status(s: dict) -> str:
    """Human-readable status report. Contains no secret values."""
    f = s["files"]
    oc, ps = f["oauth_client"], f["pubsub"]
    lines = [
        "hermes-inbox-organizer — configuration status",
        f"  config dir: {s['config_dir']}",
        f"  data dir:   {s['data_dir']}",
        "",
        "Config files:",
        f"  [{_chk(oc['present'] and oc['valid'])}] inbox-oauth-client.json  "
        f"(client_id={_yn(oc['client_id'])}, secret={_yn(oc['client_secret'])}, "
        f"redirect={oc['redirect_uri'] or '—'})",
        f"  [{_chk(ps['present'] and ps['valid'])}] inbox-pubsub.json        "
        f"(project={ps['project'] or '—'}, topic={ps['topic'] or '—'}, sub={ps['subscription'] or '—'})",
        f"  [{_chk(f['sa_key']['present'] and f['sa_key']['valid'])}] inbox-pubsub-sa.json",
        f"  [{_chk(f['encryption_key']['valid'])}] inbox-encryption-key",
        "",
        f"Owner allowlist: {s['owners']} id(s)"
        + ("  — WARNING: empty, account connect is disabled" if s["owners"] == 0 else ""),
        "",
        "Environment:",
        f"  [{_chk(s['env']['classifier_api_key'])}] classifier API key "
        "(INBOX_CLASSIFIER_API_KEY or OPENROUTER_API_KEY)",
        f"        endpoint: {s['env']['classifier_base_url']}",
        f"  [{_chk(s['env']['hermes_api_url'])}] HERMES_API_URL (optional — drafted replies)",
        "",
        "Capabilities:",
        f"  mailbox sync (triage + labels): {_yn(s['capabilities']['mailbox_sync'])}",
        f"  LLM classification:             {_yn(s['capabilities']['llm_classification'])}",
        f"  drafted replies:                {_yn(s['capabilities']['drafted_replies'])}",
        f"  account connect:                {_yn(s['capabilities']['account_connect'])}",
    ]
    return "\n".join(lines)


# --- interactive commands ---------------------------------------------------

def cmd_setup(
    _args: Any = None,
    *,
    reader: Callable[[str], str] = input,
    secret_reader: Callable[[str], str] = getpass.getpass,
    out: Callable[..., None] = print,
) -> int:
    """Interactive wizard that writes the config files. ``reader``/``secret_reader``
    are injectable so the flow is testable without a TTY."""
    reset_config()
    cfg = get_config()
    out("hermes-inbox-organizer setup")
    out(f"Writes config into: {cfg.config_dir}  (secrets are never echoed back)")
    out(f"Do the Google Cloud setup first: {GOOGLE_BOOTSTRAP}")
    out("")

    client_id = reader("OAuth client ID: ").strip()
    client_secret = secret_reader("OAuth client secret (hidden): ").strip()
    redirect = reader(f"OAuth redirect URI [{DEFAULT_REDIRECT}]: ").strip() or DEFAULT_REDIRECT
    owners_raw = reader("Owner Hermes user id(s), comma-separated (e.g. @you:server): ").strip()
    owner_ids = [s.strip() for s in owners_raw.split(",") if s.strip()]

    project = reader("Google Cloud project id: ").strip()
    topic = normalize_topic(reader("Pub/Sub topic (name or full path): ").strip(), project)
    subscription = reader("Pub/Sub subscription name: ").strip()
    sa_path = reader("Path to the Pub/Sub service-account JSON key file: ").strip()
    try:
        with open(os.path.expanduser(sa_path)) as fh:
            sa_text = fh.read()
        json.loads(sa_text)
    except (OSError, ValueError) as exc:
        out(f"ERROR: could not read a valid JSON key from {sa_path!r}: {exc}")
        return 1

    llm_base = (
        reader(f"Classifier LLM base URL [{DEFAULT_LLM_BASE_URL}]: ").strip() or DEFAULT_LLM_BASE_URL
    )
    llm_key = secret_reader(
        "Classifier LLM API key (hidden; OpenRouter or any OpenAI-compatible key): "
    ).strip()
    llm_model = (
        reader(f"Classifier model [{DEFAULT_CLASSIFIER_MODEL}]: ").strip() or DEFAULT_CLASSIFIER_MODEL
    )
    hermes_url = reader("Hermes API URL (optional, for drafted replies) []: ").strip()

    missing = [
        label
        for label, val in [
            ("client ID", client_id),
            ("client secret", client_secret),
            ("project", project),
            ("topic", topic),
            ("subscription", subscription),
        ]
        if not val
    ]
    if missing:
        out("ERROR: missing required values: " + ", ".join(missing))
        return 1
    if not owner_ids:
        out("WARNING: no owner id set — account connect will be refused for everyone.")
    if not llm_key:
        out(
            "WARNING: no classifier API key set — LLM classification is unavailable "
            "(the deterministic pre-classifier still runs)."
        )

    oauth: dict[str, Any] = {"client_id": client_id, "client_secret": client_secret, "redirect_uri": redirect}
    if owner_ids:
        oauth["owner_matrix_ids"] = owner_ids
    pubsub = {"project": project, "topic": topic, "subscription": subscription}

    env_vars: dict[str, str] = {}
    if llm_key:
        env_vars["INBOX_CLASSIFIER_API_KEY"] = llm_key
    if llm_base != DEFAULT_LLM_BASE_URL:
        env_vars["INBOX_CLASSIFIER_BASE_URL"] = llm_base
    if llm_model != DEFAULT_CLASSIFIER_MODEL:
        env_vars["INBOX_CLASSIFIER_MODEL"] = llm_model
    if hermes_url:
        env_vars["HERMES_API_URL"] = hermes_url

    key_existed = os.path.exists(cfg.key_file)
    written = write_config(
        cfg, oauth=oauth, pubsub=pubsub, sa_key_text=sa_text, env_vars=env_vars or None
    )

    out("")
    out("Wrote:")
    for path in written:
        out(f"  {path}")
    if key_existed:
        out(f"  (kept existing {cfg.key_file} — the encryption key is never regenerated)")
    if env_vars:
        env_path = os.path.join(cfg.config_dir, ENV_FILE)
        out("")
        out(f"Env vars written to {env_path} — wire them into your Hermes environment:")
        out(f"  docker compose:  add `env_file: [{env_path}]` to the hermes service")
        out(f"  shell / systemd: `set -a; . {env_path}; set +a`")
    out("")
    out('Next: restart Hermes, then in chat say "connect a Gmail account".')
    out("Check anytime with:  hermes-inbox-organizer status")
    return 0


def cmd_status(_args: Any = None, *, out: Callable[..., None] = print) -> int:
    reset_config()
    out(render_status(compute_status(get_config())))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hermes-inbox-organizer",
        description="Set up and inspect the hermes-inbox-organizer plugin config.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("setup", help="Interactive wizard: write the config files + keys.")
    sub.add_parser("status", help="Show what's configured and the resulting capabilities.")
    args = parser.parse_args(argv)
    if args.command == "setup":
        return cmd_setup(args)
    if args.command == "status":
        return cmd_status(args)
    parser.print_help()
    return 0
