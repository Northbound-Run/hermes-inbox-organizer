"""Encrypted token store round-trip + perms."""

from __future__ import annotations

import os

from hermes_inbox_organizer import crypto
from hermes_inbox_organizer.token_store import AccountToken, load_token, save_token


def _tok() -> AccountToken:
    return AccountToken(
        email="u@gmail.com",
        refresh_token="1//refresh",
        client_id="cid.apps.googleusercontent.com",
        client_secret="secret",
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )


def test_save_load_roundtrip(tmp_path) -> None:
    key = crypto.generate_key()
    path = str(tmp_path / "accounts" / "u.json")
    save_token(_tok(), key, path)
    assert load_token(key, path) == _tok()


def test_blob_on_disk_is_encrypted_and_0600(tmp_path) -> None:
    key = crypto.generate_key()
    path = str(tmp_path / "accounts" / "u.json")
    save_token(_tok(), key, path)
    raw = open(path).read()
    assert "1//refresh" not in raw  # ciphertext, not plaintext
    assert oct(os.stat(path).st_mode)[-3:] == "600"
