#!/usr/bin/env python3
"""
Gmail API — one-time OAuth2 authorization helper for selectel_monitor.

Reads gmail_credentials.json (OAuth client of type "Desktop app", скачать из
Google Cloud Console → APIs & Services → Credentials), запускает локальный
браузер на консент, сохраняет refresh+access токены в gmail_token.json.

Refresh token у Google не истекает, пока юзер не отзовёт доступ или не сменит
пароль — повторно этот скрипт обычно не нужен.

Usage:
    python3 selectel_auth.py
"""

from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCRIPT_DIR = Path(__file__).parent
CREDENTIALS_FILE = SCRIPT_DIR / "gmail_credentials.json"
TOKEN_FILE = SCRIPT_DIR / "gmail_token.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main():
    if not CREDENTIALS_FILE.exists():
        raise SystemExit(
            f"Missing {CREDENTIALS_FILE}. Скачай OAuth-client (тип Desktop) "
            "из Google Cloud Console → APIs & Services → Credentials и положи рядом."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=8765, open_browser=True)
    TOKEN_FILE.write_text(creds.to_json())
    print(f"Token saved to {TOKEN_FILE}")
    print(f"Scopes: {creds.scopes}")
    print(f"Has refresh token: {bool(creds.refresh_token)}")


if __name__ == "__main__":
    main()
