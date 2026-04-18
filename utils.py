"""Shared utilities for the financial monitor scripts."""

import json
import logging
import time
from pathlib import Path

import requests

DEFAULT_STATE_FILE = Path(__file__).parent / "monitor_state.json"

log = logging.getLogger("monitor-utils")


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
