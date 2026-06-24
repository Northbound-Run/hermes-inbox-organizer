"""Tests for the setup/status CLI (hermes_inbox_organizer.cli)."""

from __future__ import annotations

import json
import os
import stat

import pytest

from hermes_inbox_organizer import cli
from hermes_inbox_organizer.config import get_config, reset_config


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A Config rooted at a tmp config/data dir, with a clean environment."""
    for var in (
        "INBOX_OWNER_MATRIX_IDS",
        "OPENROUTER_API_KEY",
        "INBOX_CLASSIFIER_API_KEY",
        "INBOX_CLASSIFIER_BASE_URL",
        "INBOX_CLASSIFIER_MODEL",
        "HERMES_API_URL",
        "INBOX_OAUTH_REDIRECT_URI",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("INBOX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("INBOX_DATA_DIR", str(tmp_path / "data"))
    reset_config()
    yield get_config()
    reset_config()


def _full_oauth():
    return {
        "client_id": "cid.apps.googleusercontent.com",
        "client_secret": "SUPER_SECRET_VALUE",
        "redirect_uri": "https://example.test/cb",
        "owner_matrix_ids": ["@me:server"],
    }


def _full_pubsub():
    return {
        "project": "proj-1",
        "topic": "projects/proj-1/topics/gmail",
        "subscription": "gmail-pull",
    }


# --- normalize_topic --------------------------------------------------------

@pytest.mark.parametrize(
    "topic,project,expected",
    [
        ("gmail", "proj-1", "projects/proj-1/topics/gmail"),
        ("projects/p/topics/t", "proj-1", "projects/p/topics/t"),
        ("", "proj-1", ""),
        ("  gmail  ", "proj-1", "projects/proj-1/topics/gmail"),
    ],
)
def test_normalize_topic(topic, project, expected):
    assert cli.normalize_topic(topic, project) == expected


# --- write_config -----------------------------------------------------------

def test_write_config_creates_locked_files(cfg):
    written = cli.write_config(
        cfg,
        oauth=_full_oauth(),
        pubsub=_full_pubsub(),
        sa_key_text='{"type": "service_account"}',
        env_vars={"INBOX_CLASSIFIER_API_KEY": "or-key", "HERMES_API_URL": "http://localhost:8080"},
    )
    # the four config files + the env file
    assert cfg.oauth_client_file in written
    assert cfg.key_file in written
    for path in (cfg.oauth_client_file, cfg.pubsub_config_file, cfg.sa_key_file, cfg.key_file):
        assert os.path.exists(path)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # oauth + pubsub round-trip as JSON
    assert json.load(open(cfg.oauth_client_file))["client_id"].endswith("googleusercontent.com")
    assert json.load(open(cfg.pubsub_config_file))["project"] == "proj-1"
    # generated key is a valid 32-byte hex key
    assert len(bytes.fromhex(open(cfg.key_file).read().strip())) == 32
    # env file carries the env-only settings
    env_text = open(os.path.join(cfg.config_dir, cli.ENV_FILE)).read()
    assert "INBOX_CLASSIFIER_API_KEY=or-key" in env_text
    assert "HERMES_API_URL=http://localhost:8080" in env_text


def test_write_config_preserves_existing_key(cfg):
    os.makedirs(cfg.config_dir, exist_ok=True)
    sentinel = "ab" * 32  # a fixed 32-byte hex key
    with open(cfg.key_file, "w") as f:
        f.write(sentinel)
    written = cli.write_config(
        cfg, oauth=_full_oauth(), pubsub=_full_pubsub(), sa_key_text="{}"
    )
    assert cfg.key_file not in written  # not rewritten
    assert open(cfg.key_file).read() == sentinel  # unchanged


# --- compute_status ---------------------------------------------------------

def test_status_empty(cfg):
    s = cli.compute_status(cfg)
    assert s["files"]["oauth_client"]["present"] is False
    assert s["owners"] == 0
    assert s["capabilities"] == {
        "mailbox_sync": False,
        "llm_classification": False,
        "drafted_replies": False,
        "account_connect": False,
    }


def test_status_full(cfg, monkeypatch):
    cli.write_config(cfg, oauth=_full_oauth(), pubsub=_full_pubsub(), sa_key_text="{}")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or")
    monkeypatch.setenv("HERMES_API_URL", "http://h")
    s = cli.compute_status(cfg)
    assert s["files"]["oauth_client"]["client_secret"] is True
    assert s["owners"] == 1  # from oauth file fallback
    caps = s["capabilities"]
    assert caps["mailbox_sync"] is True
    assert caps["llm_classification"] is True
    assert caps["drafted_replies"] is True
    assert caps["account_connect"] is True


def test_status_owner_from_env_takes_precedence(cfg, monkeypatch):
    cli.write_config(cfg, oauth=_full_oauth(), pubsub=_full_pubsub(), sa_key_text="{}")
    monkeypatch.setenv("INBOX_OWNER_MATRIX_IDS", "@a:s, @b:s")
    assert cli.compute_status(cfg)["owners"] == 2


def test_render_status_never_leaks_secrets(cfg, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "OPENROUTER_SECRET_XYZ")
    cli.write_config(cfg, oauth=_full_oauth(), pubsub=_full_pubsub(), sa_key_text="{}")
    text = cli.render_status(cli.compute_status(cfg))
    assert "SUPER_SECRET_VALUE" not in text  # client_secret
    assert "OPENROUTER_SECRET_XYZ" not in text
    assert "client_id=yes" in text and "secret=yes" in text


# --- cmd_setup (scripted, no TTY) -------------------------------------------

def test_cmd_setup_writes_config(cfg, tmp_path):
    sa_file = tmp_path / "sa.json"
    sa_file.write_text('{"type": "service_account", "project_id": "proj-1"}')

    answers = iter(
        [
            "cid.apps.googleusercontent.com",  # client id
            "",                                # redirect (accept default)
            "@me:server",                      # owner ids
            "proj-1",                          # project
            "gmail",                           # topic (bare → normalized)
            "gmail-pull",                      # subscription
            str(sa_file),                      # SA key path
            "",                                # classifier base URL (default)
            "",                                # classifier model (default)
            "",                                # HERMES_API_URL (skip)
        ]
    )
    secrets = iter(["the-client-secret", "the-classifier-key"])
    captured: list[str] = []

    rc = cli.cmd_setup(
        reader=lambda _prompt: next(answers),
        secret_reader=lambda _prompt: next(secrets),
        out=lambda *a: captured.append(" ".join(str(x) for x in a)),
    )

    assert rc == 0
    oauth = json.load(open(cfg.oauth_client_file))
    assert oauth["redirect_uri"] == cli.DEFAULT_REDIRECT
    assert oauth["owner_matrix_ids"] == ["@me:server"]
    pubsub = json.load(open(cfg.pubsub_config_file))
    assert pubsub["topic"] == "projects/proj-1/topics/gmail"
    # the classifier key is written under the generic (endpoint-agnostic) var;
    # defaults for base URL + model are NOT written
    env_text = open(os.path.join(cfg.config_dir, cli.ENV_FILE)).read()
    assert "INBOX_CLASSIFIER_API_KEY=the-classifier-key" in env_text
    assert "INBOX_CLASSIFIER_BASE_URL" not in env_text
    assert "INBOX_CLASSIFIER_MODEL" not in env_text
    # the wizard must never echo the secrets it was given
    blob = "\n".join(captured)
    assert "the-client-secret" not in blob
    assert "the-classifier-key" not in blob


def test_cmd_setup_bad_sa_path_fails(cfg):
    answers = iter(
        ["cid", "", "@me:s", "proj", "gmail", "sub", "/no/such/file.json", ""]
    )
    secrets = iter(["secret", "or-key"])
    rc = cli.cmd_setup(
        reader=lambda _p: next(answers),
        secret_reader=lambda _p: next(secrets),
        out=lambda *a: None,
    )
    assert rc == 1
    assert not os.path.exists(cfg.oauth_client_file)  # nothing written on failure


# --- main dispatch ----------------------------------------------------------

def test_main_status_smoke(cfg, capsys):
    assert cli.main(["status"]) == 0
    assert "configuration status" in capsys.readouterr().out


def test_main_no_command_prints_help(cfg):
    assert cli.main([]) == 0


# --- classifier endpoint resolution (any OpenAI-compatible endpoint) ---------

def test_status_custom_endpoint(cfg, monkeypatch):
    cli.write_config(cfg, oauth=_full_oauth(), pubsub=_full_pubsub(), sa_key_text="{}")
    monkeypatch.setenv("INBOX_CLASSIFIER_API_KEY", "sk-local")
    monkeypatch.setenv("INBOX_CLASSIFIER_BASE_URL", "http://localhost:1234/v1")
    s = cli.compute_status(cfg)
    assert s["capabilities"]["llm_classification"] is True
    assert s["env"]["classifier_base_url"] == "http://localhost:1234/v1"
    assert "http://localhost:1234/v1" in cli.render_status(s)


def test_classifier_key_falls_back_to_openrouter(cfg, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    reset_config()
    c = get_config()
    assert c.classifier_api_key == "or-key"
    assert c.classifier_base_url == "https://openrouter.ai/api/v1"  # default


def test_classifier_explicit_key_takes_precedence(cfg, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    monkeypatch.setenv("INBOX_CLASSIFIER_API_KEY", "explicit")
    reset_config()
    assert get_config().classifier_api_key == "explicit"
