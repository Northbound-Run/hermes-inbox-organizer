"""Encrypted per-account OAuth token storage.

One AES-GCM-encrypted JSON blob per connected account, written with 0600 perms.
The blob holds the refresh token + client id/secret + token_uri + scopes — enough
for the plugin to mint fresh access tokens (Credentials auto-refresh).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass

from . import crypto


@dataclass
class AccountToken:
    email: str
    refresh_token: str
    client_id: str
    client_secret: str
    token_uri: str
    scopes: list[str]


def save_token(tok: AccountToken, key_hex: str, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    blob = crypto.encrypt(json.dumps(asdict(tok)), key_hex)
    with open(path, "w") as f:
        f.write(blob)
    os.chmod(path, 0o600)


def load_token(key_hex: str, path: str) -> AccountToken:
    with open(path) as f:
        blob = f.read()
    return AccountToken(**json.loads(crypto.decrypt(blob, key_hex)))
