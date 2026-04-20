#!/usr/bin/env python3
"""
Selectel email monitor.

Раз в час читает Gmail, ищет письма от no-reply@selectel.ru за последние 2 дня
(защита от пропусков при простое VPS — дедуп по message-id отфильтрует уже
форварднутое), форвардит Subject + первые ~500 символов в Telegram.

Usage:
    python3 selectel_monitor.py             # forward new messages
    python3 selectel_monitor.py --status    # smoke test: count + Telegram report

Требования:
    Перед первым запуском выполнить `python3 selectel_auth.py` локально,
    чтобы получить gmail_token.json (требует gmail_credentials.json от
    Google Cloud OAuth client типа Desktop).
"""

import base64
import html
import json
import logging
import os
import re
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from utils import (
    load_state,
    save_state,
    send_telegram_alert,
    send_telegram_photo,
    touch_heartbeat,
)

log = logging.getLogger("selectel-monitor")

SCRIPT_DIR = Path(__file__).parent
TOKEN_FILE = SCRIPT_DIR / "gmail_token.json"
ASSETS_DIR = SCRIPT_DIR / "assets"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# MONITOR_INSTANCE_NAME identifies *which* sender this run is for. It picks
# the state-bucket key and heartbeat unit name, so the same script can serve
# multiple Gmail-forward unit files (selectel, vdska, …) without colliding.
INSTANCE = os.environ.get("MONITOR_INSTANCE_NAME", "selectel")

CONFIG = {
    "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
    "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    "sender_filter": os.environ.get("GMAIL_SENDER_FILTER", "no-reply@selectel.ru"),
    "lookback": os.environ.get("GMAIL_LOOKBACK", "2d"),
    "body_preview_len": int(os.environ.get("SELECTEL_BODY_PREVIEW_LEN", "500")),
    "service_label": os.environ.get("SELECTEL_SERVICE_LABEL", "Selectel"),
    "max_processed_ids": int(os.environ.get("SELECTEL_MAX_PROCESSED_IDS", "200")),
    "image_path": os.environ.get(
        "SELECTEL_IMAGE_PATH", str(ASSETS_DIR / f"{INSTANCE}.png")
    ),
    "instance": INSTANCE,
}


def _load_credentials():
    """Read token, refresh if expired. Returns google.oauth2.credentials.Credentials."""
    if not TOKEN_FILE.exists():
        raise FileNotFoundError(f"{TOKEN_FILE} missing — run selectel_auth.py first")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_FILE.write_text(creds.to_json())
        else:
            raise RuntimeError(
                "Credentials invalid and cannot refresh — re-run selectel_auth.py"
            )
    return creds


def _build_gmail_service(creds):
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _decode_b64url(data):
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _strip_html(s):
    # Сначала вырезаем <style>…</style>, <script>…</script> и HTML-комментарии
    # целиком — иначе их содержимое (CSS-правила, условные MSO-комменты) просочится
    # сквозь обычный strip-tags и превратит письмо в мусор вроде
    # «a {text-decoration: none;} sup { font-size: 100% !important; } …».
    s = re.sub(
        r"<(style|script)\b[^>]*>.*?</\1\s*>",
        " ",
        s,
        flags=re.IGNORECASE | re.DOTALL,
    )
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.DOTALL)
    no_tags = re.sub(r"<[^>]+>", " ", s)
    unescaped = html.unescape(no_tags)
    return re.sub(r"\s+", " ", unescaped).strip()


def _extract_body(payload):
    """Recursively walk MIME tree, return text body. Prefer text/plain over text/html."""
    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})
    data = body.get("data")

    if mime == "text/plain" and data:
        return _decode_b64url(data).strip()
    if mime == "text/html" and data:
        return _strip_html(_decode_b64url(data))

    parts = payload.get("parts", []) or []
    for p in parts:
        if p.get("mimeType") == "text/plain" and p.get("body", {}).get("data"):
            return _decode_b64url(p["body"]["data"]).strip()
    for p in parts:
        if p.get("mimeType") == "text/html" and p.get("body", {}).get("data"):
            return _strip_html(_decode_b64url(p["body"]["data"]))
    for p in parts:
        sub = _extract_body(p)
        if sub:
            return sub
    return ""


def _header(headers, name):
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


def list_message_ids(service, sender, lookback):
    """Return list of message ids matching from:<sender> newer_than:<lookback>."""
    q = f"from:{sender} newer_than:{lookback}"
    ids = []
    page_token = None
    while True:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=q, pageToken=page_token, maxResults=50)
            .execute()
        )
        for m in resp.get("messages", []):
            ids.append(m["id"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return ids


def fetch_message(service, msg_id):
    """Return dict {id, subject, from, date, body} for one message."""
    raw = (
        service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    )
    headers = raw.get("payload", {}).get("headers", [])
    return {
        "id": msg_id,
        "subject": _header(headers, "Subject"),
        "from": _header(headers, "From"),
        "date": _header(headers, "Date"),
        "body": _extract_body(raw.get("payload", {})),
    }


def format_for_telegram(msg, service_label, body_preview_len):
    """Build HTML-safe Telegram message: '📩 Label\n<b>Subject</b>\n\n<body trimmed>'."""
    subject = msg.get("subject", "(без темы)") or "(без темы)"
    body = msg.get("body", "") or ""
    if len(body) > body_preview_len:
        body = body[:body_preview_len].rstrip() + "…"
    return (
        f"📩 {html.escape(service_label)}\n"
        f"<b>{html.escape(subject)}</b>\n\n"
        f"{html.escape(body)}"
    )


def _load_image_bytes(path):
    """Read image file, return bytes or None if missing/unreadable."""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        return p.read_bytes()
    except OSError as e:
        log.warning("failed to read image %s: %s", p, e)
        return None


def _deliver(text, image_bytes):
    """Send via Telegram. Prefer photo+caption if image available, fallback to text."""
    if image_bytes:
        ok = send_telegram_photo(
            image_bytes,
            CONFIG["telegram_bot_token"],
            CONFIG["telegram_chat_id"],
            caption=text,
            parse_mode="HTML",
            filename=f"{CONFIG['instance']}.png",
        )
        if ok:
            return True
        log.warning("sendPhoto failed, falling back to text-only")
    return send_telegram_alert(
        text,
        CONFIG["telegram_bot_token"],
        CONFIG["telegram_chat_id"],
    )


def forward_new():
    """Main loop: fetch new messages from configured sender and forward to Telegram."""
    state = load_state()
    instance_state = state.setdefault(CONFIG["instance"], {})
    processed = list(instance_state.get("processed_message_ids", []))
    processed_set = set(processed)

    creds = _load_credentials()
    service = _build_gmail_service(creds)

    ids = list_message_ids(service, CONFIG["sender_filter"], CONFIG["lookback"])
    new_ids = [i for i in ids if i not in processed_set]

    if not new_ids:
        print(f"No new messages from {CONFIG['sender_filter']}")
        return

    print(f"Found {len(new_ids)} new message(s) to forward")
    image_bytes = _load_image_bytes(CONFIG["image_path"])
    # Process oldest-first so order in Telegram matches arrival order.
    for msg_id in reversed(new_ids):
        msg = fetch_message(service, msg_id)
        text = format_for_telegram(
            msg, CONFIG["service_label"], CONFIG["body_preview_len"]
        )
        if not _deliver(text, image_bytes):
            print(f"Failed to send Telegram for message {msg_id}")
            sys.exit(1)
        processed.append(msg_id)
        # Trim FIFO so state file doesn't grow unbounded.
        if len(processed) > CONFIG["max_processed_ids"]:
            processed = processed[-CONFIG["max_processed_ids"] :]
        instance_state["processed_message_ids"] = processed
        save_state(state)
        print(f"Forwarded message {msg_id}: {msg.get('subject', '')[:60]}")


def send_status():
    """Smoke test: count messages from configured sender, report to Telegram."""
    creds = _load_credentials()
    service = _build_gmail_service(creds)
    ids = list_message_ids(service, CONFIG["sender_filter"], CONFIG["lookback"])

    state = load_state()
    processed = state.get(CONFIG["instance"], {}).get("processed_message_ids", [])
    processed_set = set(processed)
    new_count = sum(1 for i in ids if i not in processed_set)

    msg = (
        f"📩 {html.escape(CONFIG['service_label'])} monitor status\n"
        f"Окно: <code>newer_than:{CONFIG['lookback']}</code>\n"
        f"Отправитель: <code>{html.escape(CONFIG['sender_filter'])}</code>\n"
        f"Найдено писем: {len(ids)}\n"
        f"Из них новых: {new_count}\n"
        f"В state: {len(processed)} обработанных id"
    )
    if not send_telegram_alert(
        msg, CONFIG["telegram_bot_token"], CONFIG["telegram_chat_id"]
    ):
        print("Failed to send status")
        sys.exit(1)
    print("Status sent")


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = sys.argv[1:]
    # heartbeat is reached only when the subcommand completes without
    # sys.exit(1) — watchdog uses the timestamp to spot silent liveness issues.
    if "--status" in args:
        send_status()
    else:
        forward_new()
    touch_heartbeat(f"{CONFIG['instance']}-monitor-check")


if __name__ == "__main__":
    main()
