#!/usr/bin/env python3
"""
OpenAI API Spending Monitor
Monitors spending and sends Telegram alerts when balance drops below threshold.
"""

import requests
import json
import os
import sys
import time
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Configuration — secrets from environment variables
CONFIG = {
    "openai_admin_key": os.environ.get("OPENAI_ADMIN_KEY", ""),
    "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
    "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    "alert_threshold": 100,  # USD
}

# State file to track last alert and total_deposited
STATE_FILE = Path(__file__).parent / "monitor_state.json"
DEFAULT_TOTAL_DEPOSITED = 2408.71


def get_costs_for_period(start_ts, end_ts=None):
    """Get costs from OpenAI API for a specific period."""
    url = "https://api.openai.com/v1/organization/costs"
    headers = {
        "Authorization": f"Bearer {CONFIG['openai_admin_key']}",
        "Content-Type": "application/json"
    }

    total = 0.0
    next_page = None

    while True:
        params = {"start_time": start_ts, "limit": 100}
        if end_ts:
            params["end_time"] = end_ts
        if next_page:
            params["page"] = next_page

        response = requests.get(url, headers=headers, params=params)
        data = response.json()

        if "error" in data:
            print(f"Error: {data['error']}")
            return None

        for bucket in data.get("data", []):
            for result in bucket.get("results", []):
                amount = float(result.get("amount", {}).get("value", 0))
                total += amount

        if not data.get("has_more"):
            break
        next_page = data.get("next_page")

    return round(total, 2)


def get_total_costs():
    """Get total costs from OpenAI API for 2026."""
    # Start from Jan 1, 2026
    start_time = 1767225600
    return get_costs_for_period(start_time)


def get_today_costs():
    """Get today's costs."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start_time = int(today.timestamp())
    return get_costs_for_period(start_time)


def get_week_costs():
    """Get current week's costs (Monday to now)."""
    now = datetime.now(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    return get_costs_for_period(int(monday.timestamp()))


def get_month_costs():
    """Get current month's costs."""
    now = datetime.now(timezone.utc)
    first_day = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return get_costs_for_period(int(first_day.timestamp()))


def get_last_weeks_costs(num_weeks=4):
    """Get costs for the last N weeks."""
    now = datetime.now(timezone.utc)
    weeks = []

    for i in range(num_weeks):
        # Calculate week start (Monday)
        week_start = now - timedelta(days=now.weekday() + 7 * i)
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)

        # Calculate week end (Sunday)
        week_end = week_start + timedelta(days=7)

        cost = get_costs_for_period(int(week_start.timestamp()), int(week_end.timestamp()))
        weeks.append({
            "start": week_start,
            "end": week_end,
            "cost": cost or 0
        })

    return weeks


def get_last_months_costs(num_months=3):
    """Get costs for the last N months."""
    now = datetime.now(timezone.utc)
    months = []

    for i in range(num_months):
        # Calculate month
        month = now.month - i
        year = now.year
        while month <= 0:
            month += 12
            year -= 1

        first_day = datetime(year, month, 1, tzinfo=timezone.utc)

        # Next month first day
        if month == 12:
            next_month = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            next_month = datetime(year, month + 1, 1, tzinfo=timezone.utc)

        cost = get_costs_for_period(int(first_day.timestamp()), int(next_month.timestamp()))
        months.append({
            "month": first_day,
            "cost": cost or 0
        })

    return months


def send_telegram_alert(message):
    """Send alert to Telegram."""
    url = f"https://api.telegram.org/bot{CONFIG['telegram_bot_token']}/sendMessage"
    payload = {
        "chat_id": CONFIG["telegram_chat_id"],
        "text": message,
        "parse_mode": "HTML"
    }

    response = requests.post(url, json=payload)
    return response.status_code == 200


def load_state():
    """Load state from file."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {
        "last_alert_date": None,
        "last_balance": None,
        "total_deposited": DEFAULT_TOTAL_DEPOSITED,
        "bot_offset": None,
        "topup_history": []
    }


def save_state(state):
    """Save state to file."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def topup(amount):
    """Record a top-up."""
    state = load_state()
    old = state.get("total_deposited", DEFAULT_TOTAL_DEPOSITED)
    state["total_deposited"] = round(old + amount, 2)
    save_state(state)
    print(f"Top-up recorded: +${amount}")
    print(f"Total deposited: ${old} -> ${state['total_deposited']}")


def get_status_message():
    """Build status message with current balance info."""
    total_spent = get_total_costs()
    if total_spent is None:
        return "Не удалось получить данные от OpenAI API."

    today_spent = get_today_costs()
    state = load_state()
    total_deposited = state.get("total_deposited", DEFAULT_TOTAL_DEPOSITED)
    threshold = state.get("alert_threshold", CONFIG["alert_threshold"])
    remaining = round(total_deposited - total_spent, 2)

    return (
        f"<b>OpenAI API — Статус</b>\n\n"
        f"Остаток: <b>${remaining}</b>\n"
        f"Потрачено сегодня: ${today_spent}\n"
        f"Потрачено за 2026: ${total_spent}\n"
        f"Всего внесено: ${total_deposited}\n"
        f"Порог алерта: ${threshold}"
    )


def get_week_report():
    """Build weekly report message."""
    weeks = get_last_weeks_costs(4)

    lines = ["<b>📊 Отчёт по неделям</b>\n"]
    total = 0

    for w in weeks:
        start_str = w["start"].strftime("%d.%m")
        end_str = (w["end"] - timedelta(days=1)).strftime("%d.%m")
        cost = w["cost"]
        total += cost
        lines.append(f"{start_str} — {end_str}: <b>${cost}</b>")

    lines.append(f"\nИтого за 4 недели: <b>${round(total, 2)}</b>")
    return "\n".join(lines)


def get_month_report():
    """Build monthly report message."""
    months = get_last_months_costs(3)

    month_names = {
        1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
        5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
        9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"
    }

    lines = ["<b>📊 Отчёт по месяцам</b>\n"]
    total = 0

    for m in months:
        month_name = month_names[m["month"].month]
        cost = m["cost"]
        total += cost
        lines.append(f"{month_name} {m['month'].year}: <b>${cost}</b>")

    lines.append(f"\nИтого за 3 месяца: <b>${round(total, 2)}</b>")
    return "\n".join(lines)


def get_topup_history():
    """Build topup history message."""
    state = load_state()
    history = state.get("topup_history", [])

    if not history:
        return "История пополнений пуста."

    lines = ["<b>💰 История пополнений</b>\n"]

    # Last 10 entries with index for deletion
    start_idx = max(0, len(history) - 10)
    for i, entry in enumerate(history[-10:], start=start_idx):
        date_str = entry.get("date", "?")
        amount = entry.get("amount", 0)
        actual_datetime = entry.get("actual_datetime", "")

        if actual_datetime:
            lines.append(f"{i+1}. <b>+${amount}</b> — {actual_datetime}")
        else:
            lines.append(f"{i+1}. <b>+${amount}</b> — {date_str}")

    total = state.get("total_deposited", DEFAULT_TOTAL_DEPOSITED)
    lines.append(f"\nВсего внесено: <b>${total}</b>")
    lines.append("\n<i>Удалить: /del 1</i>")
    return "\n".join(lines)


def tg_api(method, **kwargs):
    """Call Telegram Bot API."""
    url = f"https://api.telegram.org/bot{CONFIG['telegram_bot_token']}/{method}"
    resp = requests.post(url, json=kwargs)
    return resp.json()


MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "📊 Баланс"}, {"text": "💰 Пополнить"}],
        [{"text": "📈 Отчёты"}, {"text": "⚙️ Настройки"}],
    ],
    "resize_keyboard": True,
}


def reply(chat_id, text, reply_markup=None):
    """Send a reply to a specific chat."""
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    params["reply_markup"] = reply_markup or MAIN_KEYBOARD
    tg_api("sendMessage", **params)


def answer_callback(callback_query_id, text=""):
    """Answer a callback query (dismiss the loading indicator on button)."""
    tg_api("answerCallbackQuery", callback_query_id=callback_query_id, text=text)


def do_topup(chat_id, amount, actual_datetime=None):
    """Record a top-up and reply."""
    state = load_state()
    old = state.get("total_deposited", DEFAULT_TOTAL_DEPOSITED)
    state["total_deposited"] = round(old + amount, 2)

    # Add to history
    if "topup_history" not in state:
        state["topup_history"] = []

    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    entry = {
        "date": now_str,
        "amount": amount,
    }
    if actual_datetime:
        entry["actual_datetime"] = actual_datetime

    state["topup_history"].append(entry)
    save_state(state)

    msg = f"✅ Пополнение: <b>+${amount}</b>\nВсего внесено: ${old} → ${state['total_deposited']}"
    if actual_datetime:
        msg += f"\nДата/время оплаты: {actual_datetime}"
    reply(chat_id, msg)


def do_delete_topup(chat_id, index):
    """Delete a top-up from history."""
    state = load_state()
    history = state.get("topup_history", [])

    if index < 1 or index > len(history):
        reply(chat_id, f"Неверный номер. Доступно: 1-{len(history)}")
        return

    entry = history[index - 1]
    amount = entry.get("amount", 0)

    # Remove from history
    del history[index - 1]
    state["topup_history"] = history

    # Subtract from total
    state["total_deposited"] = round(state.get("total_deposited", 0) - amount, 2)
    save_state(state)

    reply(chat_id, f"🗑 Удалено пополнение <b>${amount}</b>\nВсего внесено: ${state['total_deposited']}")


def send_topup_menu(chat_id):
    """Send top-up menu with preset buttons."""
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "$100", "callback_data": "topup:100"},
                {"text": "$200", "callback_data": "topup:200"},
                {"text": "$500", "callback_data": "topup:500"},
            ],
            [
                {"text": "$1000", "callback_data": "topup:1000"},
                {"text": "$2000", "callback_data": "topup:2000"},
            ],
            [
                {"text": "Другая сумма", "callback_data": "topup:custom"},
                {"text": "📜 История", "callback_data": "topup:history"},
            ],
        ]
    }
    reply(chat_id, "Выбери сумму пополнения:", reply_markup=keyboard)


def send_reports_menu(chat_id):
    """Send reports menu."""
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "📅 По неделям", "callback_data": "report:weeks"},
                {"text": "📆 По месяцам", "callback_data": "report:months"},
            ],
        ]
    }
    reply(chat_id, "Выбери тип отчёта:", reply_markup=keyboard)


def send_settings_menu(chat_id):
    """Send settings menu."""
    state = load_state()
    current = state.get("alert_threshold", CONFIG["alert_threshold"])
    keyboard = {
        "inline_keyboard": [
            [
                {"text": f"🔔 Порог алерта (${current})", "callback_data": "settings:threshold"},
            ],
        ]
    }
    reply(chat_id, "⚙️ <b>Настройки</b>", reply_markup=keyboard)


def handle_callback(callback_query):
    """Handle inline keyboard button presses."""
    chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))
    data = callback_query.get("data", "")
    cq_id = callback_query.get("id")

    if chat_id != CONFIG["telegram_chat_id"]:
        return

    if data.startswith("topup:"):
        value = data.split(":")[1]
        if value == "custom":
            answer_callback(cq_id)
            state = load_state()
            state["awaiting"] = "topup_amount"
            save_state(state)
            reply(chat_id, "Введи сумму пополнения:\n\n<i>Можно с датой/временем: 500 01.02.2026 14:30</i>")
        elif value == "history":
            answer_callback(cq_id)
            reply(chat_id, get_topup_history())
        else:
            amount = float(value)
            answer_callback(cq_id)
            # Ask for datetime
            state = load_state()
            state["awaiting"] = "topup_datetime"
            state["pending_topup"] = amount
            save_state(state)
            now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
            keyboard = {
                "inline_keyboard": [
                    [{"text": f"Сейчас ({now_str})", "callback_data": "topup_dt:now"}],
                    [{"text": "Указать дату/время", "callback_data": "topup_dt:custom"}],
                ]
            }
            reply(chat_id, f"Пополнение на <b>${amount}</b>\nКогда была оплата?", reply_markup=keyboard)

    elif data.startswith("topup_dt:"):
        value = data.split(":")[1]
        state = load_state()
        amount = state.get("pending_topup", 0)

        if value == "now":
            answer_callback(cq_id, "✅")
            now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
            state.pop("awaiting", None)
            state.pop("pending_topup", None)
            save_state(state)
            do_topup(chat_id, amount, now_str)
        elif value == "custom":
            answer_callback(cq_id)
            state["awaiting"] = "topup_datetime_input"
            save_state(state)
            reply(chat_id, "Введи дату и время оплаты:\n<i>Например: 01.02.2026 14:30</i>")

    elif data.startswith("threshold:"):
        value = data.split(":")[1]
        if value == "custom":
            answer_callback(cq_id)
            state = load_state()
            state["awaiting"] = "threshold"
            save_state(state)
            reply(chat_id, "Введи новый порог:")
        else:
            new_threshold = float(value)
            state = load_state()
            state["alert_threshold"] = new_threshold
            save_state(state)
            reply(chat_id, f"✅ Порог алерта: <b>${new_threshold}</b>")
            answer_callback(cq_id, f"${new_threshold}")

    elif data.startswith("settings:"):
        value = data.split(":")[1]
        if value == "threshold":
            answer_callback(cq_id)
            send_threshold_menu(chat_id)

    elif data.startswith("report:"):
        value = data.split(":")[1]
        answer_callback(cq_id)
        if value == "weeks":
            reply(chat_id, get_week_report())
        elif value == "months":
            reply(chat_id, get_month_report())


def send_threshold_menu(chat_id):
    """Send threshold menu with preset buttons."""
    state = load_state()
    current = state.get("alert_threshold", CONFIG["alert_threshold"])
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "$50", "callback_data": "threshold:50"},
                {"text": "$100", "callback_data": "threshold:100"},
                {"text": "$200", "callback_data": "threshold:200"},
            ],
            [
                {"text": "$500", "callback_data": "threshold:500"},
                {"text": "Другой", "callback_data": "threshold:custom"},
            ],
        ]
    }
    reply(chat_id, f"Текущий порог: <b>${current}</b>\nВыбери новый:", reply_markup=keyboard)


def handle_bot_message(message):
    """Handle an incoming Telegram message."""
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()

    if chat_id != CONFIG["telegram_chat_id"]:
        return

    state = load_state()

    # Keyboard buttons (with emoji)
    if text in ("📊 Баланс", "Баланс", "/status", "/balance"):
        reply(chat_id, get_status_message())

    elif text in ("💰 Пополнить", "Пополнить", "/topup"):
        send_topup_menu(chat_id)

    elif text in ("📈 Отчёты", "Отчёты", "/reports"):
        send_reports_menu(chat_id)

    elif text in ("⚙️ Настройки", "Настройки", "/settings"):
        send_settings_menu(chat_id)

    elif text == "/threshold":
        send_threshold_menu(chat_id)

    elif text == "/weeks":
        reply(chat_id, get_week_report())

    elif text == "/months":
        reply(chat_id, get_month_report())

    elif text == "/history":
        reply(chat_id, get_topup_history())

    elif text.startswith("/del "):
        try:
            index = int(text.split()[1])
            do_delete_topup(chat_id, index)
        except (ValueError, IndexError):
            reply(chat_id, "Формат: /del 1")

    elif text.startswith("/topup "):
        # /topup 500 or /topup 500 01.02.2026 14:30
        parts = text.split(maxsplit=2)[1:]  # Keep datetime together
        try:
            amount = float(parts[0])
            actual_datetime = parts[1] if len(parts) > 1 else None
            do_topup(chat_id, amount, actual_datetime)
        except (ValueError, IndexError):
            reply(chat_id, "Формат: /topup 500 или /topup 500 01.02.2026 14:30")

    elif text.startswith("/threshold "):
        try:
            new_threshold = float(text.split()[1])
            state["alert_threshold"] = new_threshold
            save_state(state)
            reply(chat_id, f"✅ Порог алерта: <b>${new_threshold}</b>")
        except (ValueError, IndexError):
            reply(chat_id, "Формат: /threshold 200")

    # Awaiting states
    elif state.get("awaiting") == "topup_amount":
        # Parse: amount or amount datetime
        parts = text.split(maxsplit=1)
        try:
            amount = float(parts[0])
            actual_datetime = parts[1] if len(parts) > 1 else None
            state.pop("awaiting", None)
            save_state(state)
            do_topup(chat_id, amount, actual_datetime)
        except ValueError:
            reply(chat_id, "Введи сумму числом, например: 350 или 350 01.02.2026 14:30")

    elif state.get("awaiting") == "topup_datetime_input":
        amount = state.get("pending_topup", 0)
        state.pop("awaiting", None)
        state.pop("pending_topup", None)
        save_state(state)
        do_topup(chat_id, amount, text)

    elif state.get("awaiting") == "threshold":
        try:
            new_threshold = float(text)
            state.pop("awaiting", None)
            state["alert_threshold"] = new_threshold
            save_state(state)
            reply(chat_id, f"✅ Порог алерта: <b>${new_threshold}</b>")
        except ValueError:
            reply(chat_id, "Введи порог числом, например 200")

    elif text.startswith("/help") or text == "/start":
        reply(chat_id, (
            "<b>OpenAI Monitor</b>\n\n"
            "Используй кнопки или команды:\n\n"
            "/status — баланс\n"
            "/topup — пополнить\n"
            "/topup 500 — быстрое пополнение\n"
            "/topup 500 01.02.2026 14:30 — с датой/временем\n"
            "/history — история пополнений\n"
            "/del 1 — удалить пополнение\n"
            "/weeks — отчёт по неделям\n"
            "/months — отчёт по месяцам\n"
            "/threshold — порог алерта\n"
            "/settings — настройки"
        ))


def run_bot():
    """Run Telegram bot with long polling."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    log = logging.getLogger("bot")

    missing = [k for k in ("telegram_bot_token", "telegram_chat_id") if not CONFIG[k]]
    if missing:
        log.error(f"Missing env vars: {', '.join(v.upper() for v in missing)}")
        return

    log.info("Bot started, listening for commands...")
    state = load_state()
    offset = state.get("bot_offset")

    while True:
        try:
            url = f"https://api.telegram.org/bot{CONFIG['telegram_bot_token']}/getUpdates"
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset

            resp = requests.get(url, params=params, timeout=35)
            data = resp.json()

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                # Save offset immediately to prevent duplicates on restart/error
                state = load_state()
                state["bot_offset"] = offset
                save_state(state)

                if update.get("message"):
                    handle_bot_message(update["message"])
                elif update.get("callback_query"):
                    handle_callback(update["callback_query"])

        except Exception as e:
            log.error(f"Error: {e}")
            time.sleep(5)


def check_and_alert():
    """Cron mode: check balance and send alert if needed."""
    missing = [k for k in ("openai_admin_key", "telegram_bot_token", "telegram_chat_id")
               if not CONFIG[k]]
    if missing:
        print(f"Missing environment variables: {', '.join(v.upper() for v in missing)}")
        return

    state = load_state()
    total_deposited = state.get("total_deposited", DEFAULT_TOTAL_DEPOSITED)
    threshold = state.get("alert_threshold", CONFIG["alert_threshold"])

    print("OpenAI Spending Monitor")
    print("=" * 40)

    print("Fetching costs from OpenAI API...")
    total_spent = get_total_costs()

    if total_spent is None:
        print("Failed to fetch costs")
        return

    today_spent = get_today_costs()
    remaining = round(total_deposited - total_spent, 2)

    print(f"\nTotal deposited: ${total_deposited}")
    print(f"Total spent (2026): ${total_spent}")
    print(f"Today spent: ${today_spent}")
    print(f"Remaining balance: ${remaining}")
    print(f"Alert threshold: ${threshold}")

    today = datetime.now().strftime("%Y-%m-%d")

    if remaining <= threshold:
        print(f"\n[!] Balance below threshold!")

        if state.get("last_alert_date") != today or state.get("last_balance") != remaining:
            message = (
                f"🚨 <b>OpenAI API — Алерт</b>\n\n"
                f"Остаток: <b>${remaining}</b>\n"
                f"Потрачено сегодня: ${today_spent}\n"
                f"Потрачено за 2026: ${total_spent}\n\n"
                f"Порог: ${threshold}\n"
                f"<b>Нужно пополнить!</b>"
            )

            if send_telegram_alert(message):
                print("Telegram alert sent!")
                state["last_alert_date"] = today
                state["last_balance"] = remaining
                save_state(state)
            else:
                print("Failed to send Telegram alert")
    else:
        print(f"\n[OK] Balance above threshold")

    return remaining


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--bot":
        run_bot()
    elif len(sys.argv) >= 3 and sys.argv[1] == "--topup":
        try:
            amount = float(sys.argv[2])
        except ValueError:
            print("Usage: python3 openai_monitor.py --topup <amount>")
            return
        topup(amount)
    else:
        check_and_alert()


if __name__ == "__main__":
    main()
