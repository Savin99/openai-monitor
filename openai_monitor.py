#!/usr/bin/env python3
"""
OpenAI API Spending Monitor
Monitors spending and sends Telegram alerts when balance drops below threshold.
"""

import requests
import os
import sys
import time
import logging
from datetime import datetime, timezone, timedelta

import utils
from utils import load_state as _load_state_raw, save_state as _save_state_raw
from utils import (
    send_telegram_alert as _send_telegram_alert_raw,
    send_telegram_document as _send_telegram_document_raw,
    send_telegram_photo as _send_telegram_photo_raw,
    touch_heartbeat,
)

# Configuration — secrets from environment variables
CONFIG = {
    "openai_admin_key": os.environ.get("OPENAI_ADMIN_KEY", ""),
    "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
    "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    "alert_threshold": 100,  # USD
}

log = logging.getLogger("openai-monitor")

DEFAULT_TOTAL_DEPOSITED = 2408.71


def get_costs_for_period(start_ts, end_ts=None, max_retries=3):
    """Get costs from OpenAI API for a specific period.

    Returns (cost_float, None) on success, or (None, error_string) on failure.
    """
    url = "https://api.openai.com/v1/organization/costs"
    headers = {
        "Authorization": f"Bearer {CONFIG['openai_admin_key']}",
        "Content-Type": "application/json"
    }

    total = 0.0
    next_page = None
    last_err = None

    while True:
        params = {"start_time": start_ts, "limit": 100}
        if end_ts:
            params["end_time"] = end_ts
        if next_page:
            params["page"] = next_page

        data = None
        for attempt in range(max_retries):
            try:
                response = requests.get(url, headers=headers, params=params, timeout=30)
                if response.status_code != 200:
                    last_err = f"HTTP {response.status_code}: {response.text[:200]}"
                    log.error("OpenAI costs API (attempt %d): %s", attempt + 1, last_err)
                else:
                    data = response.json()
                    break
            except requests.RequestException as e:
                last_err = f"Ошибка сети: {e}"
                log.error("Error fetching costs (attempt %d): %s", attempt + 1, e)
            except (ValueError, KeyError) as e:
                last_err = f"Ошибка парсинга ответа: {e}"
                log.error("Error parsing costs response: %s", e)
                return None, last_err

            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)

        if data is None:
            return None, last_err

        if "error" in data:
            err = f"API error: {data['error']}"
            log.error("OpenAI costs API error response: %s", data["error"])
            return None, err

        for bucket in data.get("data", []):
            for result in bucket.get("results", []):
                amount = float(result.get("amount", {}).get("value", 0))
                total += amount

        if not data.get("has_more"):
            break
        next_page = data.get("next_page")

    return round(total, 2), None


def get_billing_balance():
    """Try to get real balance from OpenAI billing API.

    Returns (total_granted, total_used, remaining) or None if unavailable.
    """
    url = "https://api.openai.com/v1/dashboard/billing/credit_grants"
    headers = {
        "Authorization": f"Bearer {CONFIG['openai_admin_key']}",
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code != 200:
            return None
        data = response.json()
        total_granted = data.get("total_granted")
        total_used = data.get("total_used")
        if total_granted is not None and total_used is not None:
            remaining = round(float(total_granted) - float(total_used), 2)
            return {
                "total_granted": round(float(total_granted), 2),
                "total_used": round(float(total_used), 2),
                "remaining": remaining,
            }
    except (requests.RequestException, ValueError, KeyError) as e:
        log.debug(f"Billing API unavailable: {e}")
    return None


def get_total_costs():
    """Get total costs from OpenAI API for 2026.

    Returns (cost, error_string_or_None).
    """
    start_time = 1767225600  # Jan 1, 2026
    return get_costs_for_period(start_time)


def get_today_costs():
    """Get today's costs. Returns cost float or None."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start_time = int(today.timestamp())
    cost, _err = get_costs_for_period(start_time)
    return cost


def get_week_costs():
    """Get current week's costs (Monday to now)."""
    now = datetime.now(timezone.utc)
    monday = now - timedelta(days=now.weekday())
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    cost, _err = get_costs_for_period(int(monday.timestamp()))
    return cost


def get_month_costs():
    """Get current month's costs."""
    now = datetime.now(timezone.utc)
    first_day = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    cost, _err = get_costs_for_period(int(first_day.timestamp()))
    return cost


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

        cost, _err = get_costs_for_period(int(week_start.timestamp()), int(week_end.timestamp()))
        weeks.append({
            "start": week_start,
            "end": week_end,
            "cost": cost or 0
        })

    return weeks


def get_daily_costs(num_days=30):
    """Return [(date, amount)] ascending for the last `num_days` UTC days.

    Uses Costs API with bucket_width=1d; fills missing days with 0.
    """
    now = datetime.now(timezone.utc)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    start = end - timedelta(days=num_days)

    url = "https://api.openai.com/v1/organization/costs"
    headers = {"Authorization": f"Bearer {CONFIG['openai_admin_key']}"}
    params = {
        "start_time": int(start.timestamp()),
        "end_time": int(end.timestamp()),
        "bucket_width": "1d",
        "limit": max(num_days, 7),
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code != 200:
            log.error("daily costs HTTP %d: %s", resp.status_code, resp.text[:200])
            return []
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        log.error("daily costs request failed: %s", e)
        return []

    # Build (date → sum) from buckets
    per_day = {}
    for bucket in data.get("data", []):
        ts = bucket.get("start_time")
        if ts is None:
            continue
        d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        total = sum(float(r.get("amount", {}).get("value", 0))
                    for r in bucket.get("results", []))
        per_day[d] = per_day.get(d, 0) + total

    result = []
    d = start.date()
    end_d = end.date()
    while d < end_d:
        result.append((d, round(per_day.get(d, 0), 4)))
        d += timedelta(days=1)
    return result


def get_line_item_costs(start_ts, end_ts=None):
    """Return [(line_item_string, amount)] for the period, grouped by line_item.

    OpenAI Costs API `group_by=line_item`. Paginated if needed.
    """
    url = "https://api.openai.com/v1/organization/costs"
    headers = {"Authorization": f"Bearer {CONFIG['openai_admin_key']}"}

    totals = {}
    next_page = None
    while True:
        params = {
            "start_time": start_ts,
            "bucket_width": "1d",
            "group_by": "line_item",
            "limit": 100,
        }
        if end_ts:
            params["end_time"] = end_ts
        if next_page:
            params["page"] = next_page

        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            if resp.status_code != 200:
                log.error("line_item costs HTTP %d", resp.status_code)
                break
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            log.error("line_item costs failed: %s", e)
            break

        for bucket in data.get("data", []):
            for r in bucket.get("results", []):
                li = r.get("line_item") or "unknown"
                amt = float(r.get("amount", {}).get("value", 0))
                totals[li] = totals.get(li, 0) + amt

        if not data.get("has_more"):
            break
        next_page = data.get("next_page")

    return sorted(totals.items(), key=lambda x: x[1], reverse=True)


def get_month_line_item_costs():
    """Return line-item breakdown for the current calendar month."""
    now = datetime.now(timezone.utc)
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return get_line_item_costs(int(first.timestamp()))


def forecast_days_remaining(remaining, avg_window_days=7):
    """Estimate whole days until `remaining` runs out, based on last N days avg.

    Returns int (>= 0) or None if we have no usable data.
    """
    if remaining is None:
        return None
    if remaining <= 0:
        return 0
    daily = get_daily_costs(avg_window_days)
    if not daily:
        return None
    total = sum(v for _, v in daily)
    if total <= 0:
        return None
    avg_daily = total / len(daily)
    if avg_daily <= 0:
        return None
    return int(remaining / avg_daily)


def _parse_topup_date(entry):
    """Best-effort parse of a topup history entry's date into a date object."""
    for field in ("actual_datetime", "date"):
        val = entry.get(field)
        if not val:
            continue
        for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
            try:
                return datetime.strptime(val, fmt).date()
            except ValueError:
                continue
    return None


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

        cost, _err = get_costs_for_period(int(first_day.timestamp()), int(next_month.timestamp()))
        months.append({
            "month": first_day,
            "cost": cost or 0
        })

    return months


def send_telegram_alert(message, max_retries=3):
    """Send alert to Telegram (thin wrapper around utils)."""
    return _send_telegram_alert_raw(
        message,
        CONFIG["telegram_bot_token"],
        CONFIG["telegram_chat_id"],
        max_retries=max_retries,
    )


def send_telegram_photo(photo_bytes, caption=None, filename="chart.png", max_retries=3):
    return _send_telegram_photo_raw(
        photo_bytes,
        CONFIG["telegram_bot_token"],
        CONFIG["telegram_chat_id"],
        caption=caption,
        filename=filename,
        max_retries=max_retries,
    )


def send_telegram_document(doc_bytes, caption=None, filename="document", max_retries=3):
    return _send_telegram_document_raw(
        doc_bytes,
        CONFIG["telegram_bot_token"],
        CONFIG["telegram_chat_id"],
        caption=caption,
        filename=filename,
        max_retries=max_retries,
    )


def load_state():
    """Load state with OpenAI-specific defaults."""
    state = _load_state_raw()
    defaults = {
        "last_alert_date": None,
        "last_balance": None,
        "total_deposited": DEFAULT_TOTAL_DEPOSITED,
        "bot_offset": None,
        "topup_history": [],
    }
    return {**defaults, **state}


def save_state(state):
    """Save state to file."""
    _save_state_raw(state)


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
    total_spent, costs_err = get_total_costs()
    state = load_state()
    threshold = state.get("alert_threshold", CONFIG["alert_threshold"])

    billing = get_billing_balance()

    if total_spent is None and billing is None:
        return (
            f"Не удалось получить данные от OpenAI API.\n\n"
            f"<b>Ошибка:</b> <code>{costs_err}</code>"
        )

    if billing:
        total_deposited = billing["total_granted"]
        remaining = billing["remaining"]
        source = "авто"
    else:
        total_deposited = state.get("total_deposited", DEFAULT_TOTAL_DEPOSITED)
        remaining = round(total_deposited - (total_spent or 0), 2)
        source = "ручной"

    today_spent = get_today_costs()

    lines = [f"<b>OpenAI API — Статус</b>\n"]
    lines.append(f"Остаток: <b>${remaining}</b> ({source})")
    lines.append(f"Потрачено сегодня: ${today_spent or '?'}")
    lines.append(f"Потрачено за 2026: ${total_spent or '?'}")
    lines.append(f"Всего внесено: ${total_deposited}")
    lines.append(f"Порог алерта: ${threshold}")

    if costs_err:
        lines.append(f"\n⚠️ Costs API: <code>{costs_err}</code>")

    return "\n".join(lines)


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
    threshold = state.get("alert_threshold", CONFIG["alert_threshold"])

    print("OpenAI Spending Monitor")
    print("=" * 40)

    billing = get_billing_balance()
    if billing:
        total_deposited = billing["total_granted"]
        total_spent = billing["total_used"]
        remaining = billing["remaining"]
        print("Balance source: auto (billing API)")
    else:
        print("Billing API unavailable, using manual mode")
        total_deposited = state.get("total_deposited", DEFAULT_TOTAL_DEPOSITED)
        print("Fetching costs from OpenAI API...")
        total_spent, costs_err = get_total_costs()
        if total_spent is None:
            print(f"Failed to fetch costs: {costs_err}")
            return
        remaining = round(total_deposited - total_spent, 2)

    today_spent = get_today_costs()

    print(f"\nTotal deposited: ${total_deposited}")
    print(f"Total spent: ${total_spent}")
    print(f"Today spent: ${today_spent}")
    print(f"Remaining balance: ${remaining}")
    print(f"Alert threshold: ${threshold}")

    today = datetime.now().strftime("%Y-%m-%d")

    alert_step = state.get("alert_step", 10)

    if remaining <= threshold:
        print(f"\n[!] Balance below threshold!")

        last_alert_level = state.get("last_alert_level")
        # Current alert level: round down to nearest alert_step
        current_level = int(remaining // alert_step) * alert_step

        if last_alert_level is None or current_level < last_alert_level:
            spent_since_threshold = round(threshold - remaining, 2)
            message = (
                f"🚨 <b>OpenAI API — Алерт</b>\n\n"
                f"Остаток: <b>${remaining}</b>\n"
                f"Потрачено сегодня: ${today_spent}\n"
                f"Потрачено за 2026: ${total_spent}\n\n"
                f"Порог: ${threshold}\n"
                f"С момента алерта: <b>-${spent_since_threshold}</b>\n"
                f"<b>Нужно пополнить!</b>"
            )

            if send_telegram_alert(message):
                print("Telegram alert sent!")
                state["last_alert_date"] = today
                state["last_balance"] = remaining
                state["last_alert_level"] = current_level
                save_state(state)
            else:
                print("Failed to send Telegram alert")
                # Non-zero exit → systemd OnFailure fires → monitor-alert@ fires
                # (which re-attempts Telegram via a different path). Prevents
                # a silent drop where exit=0 hides a failed delivery.
                sys.exit(1)
    else:
        print(f"\n[OK] Balance above threshold")
        # Reset alert level when balance is topped up above threshold
        if "last_alert_level" in state:
            del state["last_alert_level"]
            save_state(state)

    return remaining


def send_status_report():
    """Send rich daily status to Telegram — caption + 3-panel PNG chart.

    Graceful fallback: if chart generation fails, still sends the text status
    as a regular message so the daily report never silently drops.
    """
    missing = [k for k in ("openai_admin_key", "telegram_bot_token", "telegram_chat_id")
               if not CONFIG[k]]
    if missing:
        print(f"Missing environment variables: {', '.join(v.upper() for v in missing)}")
        return

    # 1) Gather numbers
    total_spent, costs_err = get_total_costs()
    state = load_state()
    threshold = state.get("alert_threshold", CONFIG["alert_threshold"])
    billing = get_billing_balance()

    if billing:
        total_deposited = billing["total_granted"]
        remaining = billing["remaining"]
        source = "авто"
    else:
        total_deposited = state.get("total_deposited", DEFAULT_TOTAL_DEPOSITED)
        remaining = round(total_deposited - (total_spent or 0), 2)
        source = "ручной"

    today_spent = get_today_costs()
    forecast = forecast_days_remaining(remaining) if remaining is not None else None

    # 2) Build text caption (HTML)
    lines = ["<b>OpenAI API — Ежедневный статус</b>", ""]
    lines.append(f"Остаток: <b>${remaining}</b> ({source})")
    lines.append(f"Потрачено сегодня: ${today_spent if today_spent is not None else '?'}")
    lines.append(f"Потрачено за 2026: ${total_spent if total_spent is not None else '?'}")
    lines.append(f"Всего внесено: ${total_deposited}")
    lines.append(f"Порог алерта: ${threshold}")
    if forecast is not None:
        if forecast == 0:
            lines.append("\n⚠️ <b>Баланс исчерпан</b>")
        elif forecast <= 7:
            lines.append(f"\n⚠️ Прогноз: хватит на <b>{forecast} дн.</b>")
        else:
            lines.append(f"\nПрогноз: хватит на <b>{forecast} дн.</b>")
    if costs_err:
        lines.append(f"\nCosts API: <code>{costs_err}</code>")

    caption = "\n".join(lines)

    # 3) Build chart (best-effort); fallback to text-only on any error
    try:
        import charts  # local import so non-chart paths don't require matplotlib
        daily = get_daily_costs(30)
        # topups only for the charted window
        if daily:
            window_start = daily[0][0]
            topups_window = []
            for entry in state.get("topup_history", []):
                d = _parse_topup_date(entry)
                if d and d >= window_start:
                    topups_window.append((d, float(entry.get("amount", 0))))
        else:
            topups_window = []
        line_items = get_month_line_item_costs()
        png = charts.build_status_chart(
            daily_costs=daily,
            topup_events=topups_window,
            line_item_costs=line_items,
            current_balance=remaining if remaining is not None else 0,
            alert_threshold=threshold,
            forecast_days=forecast,
            title_suffix=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )
    except Exception as e:
        log.error("Chart build failed, falling back to text-only: %s", e)
        png = None

    # 4) Send
    if png:
        if send_telegram_photo(png, caption=caption, filename="status.png"):
            print("Status report (chart) sent!")
            return
        print("sendPhoto failed, falling back to text-only")

    if send_telegram_alert(caption):
        print("Status report (text) sent!")
    else:
        print("Failed to send status report")
        # Non-zero exit so systemd OnFailure fires — otherwise the daily
        # digest silently drops when Telegram is unreachable.
        sys.exit(1)


def backup_state():
    """Upload monitor_state.json to Telegram as a document (daily snapshot)."""
    missing = [k for k in ("telegram_bot_token", "telegram_chat_id") if not CONFIG[k]]
    if missing:
        print(f"Missing environment variables: {', '.join(v.upper() for v in missing)}")
        return
    state_path = utils.DEFAULT_STATE_FILE
    if not state_path.exists():
        print(f"State file not found at {state_path}")
        return
    data = state_path.read_bytes()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    filename = f"monitor_state_{stamp}.json"
    caption = f"📦 Backup monitor_state.json — {stamp} UTC ({len(data)} bytes)"
    if send_telegram_document(data, caption=caption, filename=filename):
        print(f"Backup sent: {filename}")
    else:
        print("Failed to send backup")
        sys.exit(1)


def main():
    # heartbeat is only reached when the subcommand completes without
    # sys.exit(1). That makes "last successful run" visible to the watchdog.
    if len(sys.argv) >= 2 and sys.argv[1] == "--bot":
        run_bot()
        # run_bot is a long-running loop — don't expect to touch heartbeat here
    elif len(sys.argv) >= 2 and sys.argv[1] == "--status":
        send_status_report()
        touch_heartbeat("openai-monitor-status")
    elif len(sys.argv) >= 2 and sys.argv[1] == "--backup":
        backup_state()
        touch_heartbeat("openai-monitor-backup")
    elif len(sys.argv) >= 3 and sys.argv[1] == "--topup":
        try:
            amount = float(sys.argv[2])
        except ValueError:
            print("Usage: python3 openai_monitor.py --topup <amount>")
            return
        topup(amount)
    else:
        check_and_alert()
        touch_heartbeat("openai-monitor-check")


if __name__ == "__main__":
    main()
