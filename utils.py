"""Shared utilities for the financial monitor scripts."""

import json
import logging
import time
from pathlib import Path

import requests

DEFAULT_STATE_FILE = Path(__file__).parent / "monitor_state.json"
HEARTBEAT_DIR = Path("/var/lib/monitor/heartbeat")

log = logging.getLogger("monitor-utils")


def touch_heartbeat(unit, heartbeat_dir=None):
    """Write current unix timestamp to HEARTBEAT_DIR/<unit>.ts.

    Called from main() after a CLI subcommand returns successfully. If the
    command sys.exit(1)ed on a Telegram failure, we never reach this line
    and the watchdog will eventually see a stale heartbeat → alert.

    Failures here are best-effort (warn, do not raise) — we don't want a
    filesystem glitch to mask a successful delivery.
    """
    try:
        d = Path(heartbeat_dir) if heartbeat_dir else HEARTBEAT_DIR
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{unit}.ts").write_text(str(int(time.time())))
    except OSError as e:
        log.warning("heartbeat write failed for %s: %s", unit, e)


def load_state(state_file=None):
    """Load state from JSON file. Returns empty dict if file doesn't exist."""
    if state_file is None:
        state_file = DEFAULT_STATE_FILE
    if state_file.exists():
        with open(state_file) as f:
            return json.load(f)
    return {}


def save_state(state, state_file=None):
    """Save state to JSON file (pretty-printed, utf-8)."""
    if state_file is None:
        state_file = DEFAULT_STATE_FILE
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def send_telegram_alert(message, bot_token, chat_id, max_retries=3, parse_mode="HTML"):
    """Send a message via Telegram Bot API with retries."""
    if not bot_token or not chat_id:
        log.error("Telegram bot_token or chat_id is empty")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": parse_mode,
    }

    for attempt in range(max_retries):
        try:
            response = requests.post(url, json=payload, timeout=10)
            data = response.json()
            if response.status_code == 200 and data.get("ok"):
                return True
            log.error(
                "Telegram API error (attempt %d): %s",
                attempt + 1,
                data.get("description", response.status_code),
            )
        except requests.RequestException as e:
            log.error("Telegram request failed (attempt %d): %s", attempt + 1, e)

        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)

    return False


def send_telegram_photo(photo_bytes, bot_token, chat_id,
                        caption=None, parse_mode="HTML",
                        filename="chart.png", max_retries=3):
    """Send a PNG image to Telegram via sendPhoto with retries."""
    if not bot_token or not chat_id:
        log.error("Telegram bot_token or chat_id is empty")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    for attempt in range(max_retries):
        try:
            files = {"photo": (filename, photo_bytes, "image/png")}
            data = {"chat_id": str(chat_id)}
            if caption is not None:
                data["caption"] = caption
                data["parse_mode"] = parse_mode
            response = requests.post(url, data=data, files=files, timeout=30)
            resp_json = response.json()
            if response.status_code == 200 and resp_json.get("ok"):
                return True
            log.error(
                "Telegram sendPhoto error (attempt %d): %s",
                attempt + 1,
                resp_json.get("description", response.status_code),
            )
        except requests.RequestException as e:
            log.error("Telegram sendPhoto failed (attempt %d): %s", attempt + 1, e)

        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)

    return False


def send_telegram_document(document_bytes, bot_token, chat_id,
                           caption=None, filename="document",
                           max_retries=3):
    """Send an arbitrary file to Telegram via sendDocument (plain text caption)."""
    if not bot_token or not chat_id:
        log.error("Telegram bot_token or chat_id is empty")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    for attempt in range(max_retries):
        try:
            files = {"document": (filename, document_bytes, "application/octet-stream")}
            data = {"chat_id": str(chat_id)}
            if caption is not None:
                data["caption"] = caption
            response = requests.post(url, data=data, files=files, timeout=30)
            resp_json = response.json()
            if response.status_code == 200 and resp_json.get("ok"):
                return True
            log.error(
                "Telegram sendDocument error (attempt %d): %s",
                attempt + 1,
                resp_json.get("description", response.status_code),
            )
        except requests.RequestException as e:
            log.error("Telegram sendDocument failed (attempt %d): %s",
                      attempt + 1, e)

        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)

    return False
