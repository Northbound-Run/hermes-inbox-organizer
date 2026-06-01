"""Connect a Gmail account via Desktop-OAuth loopback, store an encrypted token.

Runs on a machine WITH a browser (the laptop). `run_local_server` opens the
consent page, captures the code on a localhost port, and exchanges it for a
refresh token. For a headless deployment the resulting encrypted blob is shipped
to the host afterward (token-import). google libs are imported lazily.

Usage:
    python -m hermes_inbox_organizer.connect --client-secrets PATH --key HEX --out PATH
"""

from __future__ import annotations

import argparse

from .token_store import AccountToken, save_token

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def run_connect(client_secrets: str, key_hex: str, out_path: str) -> str:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    flow = InstalledAppFlow.from_client_secrets_file(client_secrets, SCOPES)
    # Fixed 127.0.0.1 loopback so it matches a registered redirect URI on a Web
    # client, and because Google increasingly rejects plain `localhost` loopback.
    creds = flow.run_local_server(
        host="127.0.0.1",
        port=8765,
        access_type="offline",
        prompt="consent",
        open_browser=True,
        timeout_seconds=300,
    )
    if not creds.refresh_token:
        raise RuntimeError("no refresh_token returned (need access_type=offline + consent)")
    email = (
        build("gmail", "v1", credentials=creds)
        .users()
        .getProfile(userId="me")
        .execute()["emailAddress"]
    )
    save_token(
        AccountToken(
            email=email,
            refresh_token=creds.refresh_token,
            client_id=creds.client_id,
            client_secret=creds.client_secret,
            token_uri=creds.token_uri,
            scopes=list(SCOPES),
        ),
        key_hex,
        out_path,
    )
    return email


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-secrets", required=True)
    ap.add_argument("--key", required=True, help="32-byte AES key as hex")
    ap.add_argument("--out", required=True, help="output path for the encrypted token blob")
    args = ap.parse_args()
    email = run_connect(args.client_secrets, args.key, args.out)
    print(f"CONNECTED {email} -> {args.out}")


if __name__ == "__main__":
    main()
