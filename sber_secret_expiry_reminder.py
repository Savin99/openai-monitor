#!/usr/bin/env python3
"""Send a Telegram reminder before the Sber API client_secret expires."""

import os
import sys

from utils import send_telegram_alert


MESSAGE = """\
⚠️ <b>Sber API — скоро истекает client_secret</b>

Срок действия текущего ключа: <b>28.06.2026</b>.
Осталось 3 дня: нужно выпустить/заменить <code>SBER_CLIENT_SECRET</code> в ЛК СберБизнес и обновить systemd env на VPS.
"""


def main():
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not send_telegram_alert(MESSAGE, bot_token, chat_id):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
