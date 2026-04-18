#!/usr/bin/env python3
"""
Sber Business API — one-time OAuth2 authorization helper.

Поднимает локальный HTTPS-сервер на redirect_uri, открывает браузер
для логина в СберБизнес, получает authorization_code, обменивает его
на пару access_token + refresh_token и сохраняет в sber_tokens.json.

Usage:
    python3 sber_auth.py

Env vars:
    SBER_CLIENT_ID       — client_id из Sber API
    SBER_CLIENT_SECRET   — client_secret из ЛК СберБизнес
    SBER_REDIRECT_URI    — https://localhost/callback (по умолчанию)
    SBER_OAUTH_BASE      — базовый URL OAuth (prod по умолчанию)
    SBER_SCOPE           — scope (см. default ниже)
"""

import base64
import json
import os
import secrets
import ssl
import subprocess
import sys
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).parent
TOKENS_FILE = SCRIPT_DIR / "sber_tokens.json"
CERT_DIR = SCRIPT_DIR / ".sber_auth_certs"
CERT_FILE = CERT_DIR / "localhost.crt"
KEY_FILE = CERT_DIR / "localhost.key"

DEFAULT_SCOPE = (
    "openid acr amr aud auth_time azp exp iat iss nonce sid2 sub "
    "BANK_CONTROL_STATEMENT BB_CREATE_LINK_APP BC_SBP_PAYMENT "
    "BUSINESS_CARDS_TRANSFER CARD_ISSUE CERTIFICATE_REQUEST "
    "CONFIRMATORY_DOCUMENTS_INQUIRY CORPORATE_CARDS CRYPTO_CERT_REQUEST_EIO "
    "CURRENCY_OPERATION_DETAILS CURR_CONTROL_INFO_REQ "
    "CURR_CONTROL_MESSAGE_FROM_BANK CURR_CONTROL_MESSAGE_TO_BANK "
    "DEPOSIT_REQUEST DICT ENCASHMENTS_REQUEST FILES "
    "GENERIC_LETTER_FROM_BANK GENERIC_LETTER_TO_BANK "
    "GET_CLIENT_ACCOUNTS GET_CORRESPONDENTS GET_CRYPTO_INFO "
    "GET_CRYPTO_INFO_EIO GET_STATEMENT_ACCOUNT GET_STATEMENT_TRANSACTION "
    "MINIMUMBALANCE_REQUEST NOMINAL_ACCOUNTS OrgName PAYROLL PAY_DOC_CUR "
    "PAY_DOC_RU SALARY_AGREEMENT SBERRATING_REPORT_FILE "
    "SBERRATING_REPORT_LINK SBERRATING_TRAFFIC_LIGHT accounts email "
    "individualExecutiveAgency inn name offerExpirationDate orgActualAddress "
    "orgFullName orgJuridicalAddress orgKpp orgLawForm orgLawFormShort "
    "orgOgrn orgOktmo phone_number terBank userPosition userSignatureType"
)

CONFIG = {
    "client_id": os.environ.get("SBER_CLIENT_ID", ""),
    "client_secret": os.environ.get("SBER_CLIENT_SECRET", ""),
    "redirect_uri": os.environ.get("SBER_REDIRECT_URI", "https://localhost/callback"),
    "oauth_base": os.environ.get(
        "SBER_OAUTH_BASE", "https://fintech.sberbank.ru:9443"
    ),
    "scope": os.environ.get("SBER_SCOPE", DEFAULT_SCOPE),
}


_received = {}


class CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        _received["code"] = query.get("code", [None])[0]
        _received["state"] = query.get("state", [None])[0]
        _received["error"] = query.get("error", [None])[0]
        _received["error_description"] = query.get("error_description", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if _received["code"]:
            body = (
                "<h1>OK</h1><p>Authorization code получен. "
                "Можно закрыть вкладку и вернуться в терминал.</p>"
            )
        else:
            body = (
                "<h1>Error</h1><pre>"
                + json.dumps(_received, ensure_ascii=False, indent=2)
                + "</pre>"
            )
        self.wfile.write(body.encode("utf-8"))


def ensure_self_signed_cert():
    CERT_DIR.mkdir(exist_ok=True)
    if CERT_FILE.exists() and KEY_FILE.exists():
        return
    print(f"Генерирую self-signed сертификат для localhost в {CERT_DIR}...")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(KEY_FILE),
            "-out", str(CERT_FILE),
            "-days", "365", "-nodes",
            "-subj", "/CN=localhost",
        ],
        check=True,
    )
    print("Сертификат создан. Браузер попросит его принять один раз.")


def build_auth_url(state, nonce):
    params = {
        "response_type": "code",
        "client_id": CONFIG["client_id"],
        "redirect_uri": CONFIG["redirect_uri"],
        "scope": CONFIG["scope"],
        "state": state,
        "nonce": nonce,
    }
    return (
        f"{CONFIG['oauth_base']}/ic/sso/api/v2/oauth/authorize?"
        + urllib.parse.urlencode(params)
    )


def exchange_code_for_tokens(code):
    token_url = f"{CONFIG['oauth_base']}/ic/sso/api/v2/oauth/token"
    basic = base64.b64encode(
        f"{CONFIG['client_id']}:{CONFIG['client_secret']}".encode()
    ).decode()
    headers = {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CONFIG["redirect_uri"],
    }
    resp = requests.post(token_url, headers=headers, data=data, timeout=30)
    if resp.status_code != 200:
        raise SystemExit(
            f"Token exchange failed: HTTP {resp.status_code}\n{resp.text}"
        )
    return resp.json()


def save_tokens(tokens):
    expires_in = int(tokens.get("expires_in", 3600))
    now = int(time.time())
    payload = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token"),
        "id_token": tokens.get("id_token"),
        "expires_at": now + expires_in,
        "issued_at": now,
        "refresh_issued_at": now,
        "scope": tokens.get("scope", CONFIG["scope"]),
    }
    with open(TOKENS_FILE, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"Токены сохранены в {TOKENS_FILE}")


def main():
    missing = [k for k in ("client_id", "client_secret") if not CONFIG[k]]
    if missing:
        print(
            "Missing env vars: "
            + ", ".join(f"SBER_{v.upper()}" for v in missing),
            file=sys.stderr,
        )
        sys.exit(1)

    parsed_redirect = urllib.parse.urlparse(CONFIG["redirect_uri"])
    if parsed_redirect.scheme != "https":
        raise SystemExit("redirect_uri должен использовать https")
    host = parsed_redirect.hostname or "localhost"
    port = parsed_redirect.port or 443

    ensure_self_signed_cert()

    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(CERT_FILE), keyfile=str(KEY_FILE))

    server = HTTPServer((host, port), CallbackHandler)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)

    auth_url = build_auth_url(state, nonce)
    print("\nОткрываю браузер для входа в СберБизнес...")
    print("Если браузер не открылся сам — вот URL:")
    print(auth_url)
    print(
        f"\nЛокальный callback-сервер слушает на {host}:{port}. "
        "Если в браузере появится предупреждение о сертификате — "
        "прими его (Advanced → Proceed)."
    )
    webbrowser.open(auth_url)

    server.timeout = 300
    server.handle_request()

    if _received.get("error"):
        raise SystemExit(
            f"OAuth error: {_received['error']} — {_received.get('error_description')}"
        )

    if not _received.get("code"):
        raise SystemExit("Не получен code за 5 минут. Попробуй ещё раз.")

    if _received.get("state") != state:
        raise SystemExit("state mismatch — возможная CSRF-атака, авторизация отклонена")

    print("\nОбмениваю code на токены...")
    tokens = exchange_code_for_tokens(_received["code"])
    save_tokens(tokens)
    print("\nГотово. Теперь можно запустить: python3 sber_monitor.py --status")


if __name__ == "__main__":
    main()
