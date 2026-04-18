#!/usr/bin/env python3
"""
Sber Business API — монитор баланса расчётного счёта.

Логика: если суммарный баланс рублёвых р/с держится выше порога
дольше tolerance дней, отправить Telegram-алерт «деньги лежат,
пора положить на депозит».

Usage:
    python3 sber_monitor.py             # check + alert
    python3 sber_monitor.py --status    # send Telegram status
    python3 sber_monitor.py --force-refresh  # force token refresh (debug)

Требования:
    Перед первым запуском выполнить `python3 sber_auth.py` один раз локально,
    чтобы получить sber_tokens.json.
"""

import base64
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from utils import (
    load_state,
    save_state,
    send_telegram_alert,
    send_telegram_photo,
)


def _parse_labels(raw):
    """Парсит SBER_ACCOUNT_LABELS: JSON или '' → dict. Номера нормализуются (без пробелов)."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return {_normalize_account_number(k): v for k, v in data.items()}


def _normalize_account_number(number):
    """Приводит номер счёта к виду без пробелов/дефисов для сравнения."""
    if not number:
        return ""
    return str(number).replace(" ", "").replace("-", "")

log = logging.getLogger("sber-monitor")

SCRIPT_DIR = Path(__file__).parent
TOKENS_FILE = SCRIPT_DIR / "sber_tokens.json"
FINAL_WARNING_IMAGE = SCRIPT_DIR / "assets" / "final_warning.jpg"

CONFIG = {
    "client_id": os.environ.get("SBER_CLIENT_ID", ""),
    "client_secret": os.environ.get("SBER_CLIENT_SECRET", ""),
    "oauth_base": os.environ.get("SBER_OAUTH_BASE", "https://fintech.sberbank.ru:9443"),
    "api_base": os.environ.get("SBER_API_BASE", "https://fintech.sberbank.ru:9443"),
    "telegram_bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
    "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    "balance_threshold": float(os.environ.get("SBER_RC_BALANCE_THRESHOLD", "5000000")),
    "alert_timezone": os.environ.get("SBER_ALERT_TIMEZONE", "Europe/Moscow"),
    "alert_hour_start": int(os.environ.get("SBER_ALERT_HOUR_START", "15")),
    "alert_hour_end": int(os.environ.get("SBER_ALERT_HOUR_END", "23")),
    "account_labels": _parse_labels(os.environ.get("SBER_ACCOUNT_LABELS", "")),
    "client_cert_file": os.environ.get("SBER_CLIENT_CERT_FILE", ""),
    "client_key_file": os.environ.get("SBER_CLIENT_KEY_FILE", ""),
    "ca_bundle_file": os.environ.get("SBER_CA_BUNDLE_FILE", ""),
}


def _client_cert_tuple():
    """Путь до PEM-пары для mTLS или None."""
    cert = CONFIG["client_cert_file"]
    key = CONFIG["client_key_file"]
    if cert and key and Path(cert).exists() and Path(key).exists():
        return (cert, key)
    return None


def _verify_bundle():
    """Путь до CA-bundle для проверки сервера Сбера, иначе True (системный store)."""
    bundle = CONFIG["ca_bundle_file"]
    if bundle and Path(bundle).exists():
        return bundle
    return True


def account_label(number):
    """Вернёт человеческое имя счёта из CONFIG['account_labels'] или '...last4'."""
    normalized = _normalize_account_number(number)
    label = CONFIG["account_labels"].get(normalized)
    if label:
        return label
    if normalized:
        return f"...{normalized[-4:]}"
    return "?"


def _alert(message):
    return send_telegram_alert(
        message, CONFIG["telegram_bot_token"], CONFIG["telegram_chat_id"]
    )


def load_tokens():
    if not TOKENS_FILE.exists():
        raise SystemExit(
            f"{TOKENS_FILE} не найден. Запусти `python3 sber_auth.py` сначала."
        )
    import json
    with open(TOKENS_FILE) as f:
        return json.load(f)


def save_tokens(tokens):
    import json
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f, indent=2, ensure_ascii=False)


def refresh_access_token(tokens):
    """Обменять refresh_token на новую пару access/refresh."""
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise SystemExit("refresh_token отсутствует — нужна повторная авторизация")

    url = f"{CONFIG['oauth_base']}/ic/sso/api/v2/oauth/token"
    basic = base64.b64encode(
        f"{CONFIG['client_id']}:{CONFIG['client_secret']}".encode()
    ).decode()
    headers = {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}

    resp = requests.post(
        url, headers=headers, data=data, timeout=30, cert=_client_cert_tuple(), verify=_verify_bundle()
    )
    if resp.status_code != 200:
        raise SystemExit(
            f"Token refresh failed: HTTP {resp.status_code}\n{resp.text[:500]}"
        )
    payload = resp.json()
    now = int(time.time())
    new_tokens = {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token", refresh_token),
        "id_token": payload.get("id_token", tokens.get("id_token")),
        "expires_at": now + int(payload.get("expires_in", 3600)),
        "issued_at": now,
        "refresh_issued_at": now if "refresh_token" in payload else tokens.get(
            "refresh_issued_at", now
        ),
        "scope": payload.get("scope", tokens.get("scope")),
    }
    save_tokens(new_tokens)
    return new_tokens


def get_valid_access_token(force_refresh=False):
    tokens = load_tokens()
    now = int(time.time())
    expires_at = tokens.get("expires_at", 0)
    # Refresh if less than 5 min left
    if force_refresh or expires_at - now < 300:
        log.info("Access token expired or close to expiring, refreshing...")
        tokens = refresh_access_token(tokens)
    return tokens["access_token"], tokens


def check_refresh_token_expiry(tokens):
    """Warn if refresh_token is about to expire (180-day lifetime)."""
    issued = tokens.get("refresh_issued_at") or tokens.get("issued_at")
    if not issued:
        return
    days_left = 180 - (int(time.time()) - int(issued)) // 86400
    if 0 < days_left <= 14:
        _alert(
            f"⚠️ <b>Sber API — refresh_token скоро истечёт</b>\n\n"
            f"Осталось дней: <b>{days_left}</b>\n"
            f"Пора перезапустить <code>sber_auth.py</code> локально."
        )
    elif days_left <= 0:
        _alert(
            "⚠️ <b>Sber API — refresh_token истёк</b>\n\n"
            "Запусти <code>sber_auth.py</code> для новой авторизации."
        )


def get_client_accounts(access_token):
    """GET список счетов клиента через /v1/client-info (scope: GET_CLIENT_ACCOUNTS).

    Ответ содержит только метаданные — accountNumber, name, type, currencyCode,
    state. БАЛАНСА ЗДЕСЬ НЕТ. За балансом отдельно через get_account_balance().
    """
    url = f"{CONFIG['api_base']}/fintech/api/v1/client-info"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    resp = requests.get(url, headers=headers, timeout=30,
                        cert=_client_cert_tuple(), verify=_verify_bundle())
    if resp.status_code != 200:
        raise RuntimeError(
            f"client-info failed: HTTP {resp.status_code}\n{resp.text[:500]}"
        )
    return resp.json().get("accounts", [])


def get_account_balance(access_token, account_number, statement_date):
    """GET баланс счёта на дату через /v2/statement/summary.

    scope: GET_STATEMENT_ACCOUNT.
    Возвращает float (closingBalance в рублях) или None при ошибке.
    """
    url = f"{CONFIG['api_base']}/fintech/api/v2/statement/summary"
    params = {"accountNumber": account_number, "statementDate": statement_date}
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    resp = requests.get(url, headers=headers, params=params, timeout=30,
                        cert=_client_cert_tuple(), verify=_verify_bundle())
    if resp.status_code != 200:
        log.warning("statement/summary %s %s: HTTP %d",
                    account_number, statement_date, resp.status_code)
        return None
    data = resp.json()
    try:
        return float(data["closingBalance"]["amount"])
    except (KeyError, ValueError, TypeError) as e:
        log.warning("can't parse closingBalance: %s; payload: %s", e, data)
        return None


def filter_rub_current_accounts(accounts):
    """Возвращает список активных рублёвых расчётных счетов (без баланса).

    Формат каждого элемента: {"accountNumber": str, "name": str, "type": str}.
    Баланс достаётся отдельно через get_account_balance().

    Правила фильтрации (по факту ответа Sber API /v1/client-info):
      - currencyCode == "810" (RUB)
      - type == "calculated" (расчётный; депозиты = "deposit" — пропускаем)
      - state == "OPEN"
      - balance может быть в поле "balance" (legacy тесты) — тогда пробрасывается.
    """
    result = []
    for acc in accounts:
        currency = acc.get("currencyCode") or acc.get("currency")
        account_type = (acc.get("accountType") or acc.get("type") or "").upper()
        state_val = (acc.get("state") or acc.get("status") or "").upper()

        is_rub = currency in ("RUB", "810", "643")
        is_current = (
            account_type in ("CALCULATED", "CURRENT")
            or "РАСЧЁТНЫЙ" in account_type
            or "РАСЧЕТНЫЙ" in account_type
        )
        is_active = state_val in ("", "OPEN", "ACTIVE", "ОТКРЫТ")

        if is_rub and is_current and is_active:
            raw_number = acc.get("accountNumber") or acc.get("number")
            entry = {
                "accountNumber": _normalize_account_number(raw_number),
                "name": acc.get("name") or "",
                "type": account_type,
            }
            # legacy-поле balance (для тестов, где balance уже в ответе)
            raw_balance = acc.get("balance") or acc.get("availableBalance")
            if raw_balance is not None:
                try:
                    entry["balance"] = round(float(raw_balance), 2)
                except (TypeError, ValueError):
                    pass
            result.append(entry)
    return result


def enrich_with_balances(access_token, accounts, statement_date):
    """Для каждого счёта дёргает get_account_balance и добавляет поле 'balance'.

    Счета, для которых баланс не получен, получают balance=None.
    """
    for a in accounts:
        if "balance" in a:
            continue  # уже проставлен (например, в тестах)
        bal = get_account_balance(access_token, a["accountNumber"], statement_date)
        a["balance"] = bal if bal is not None else 0.0
        a["balance_ok"] = bal is not None
    return accounts


def get_sber_state():
    state = load_state()
    return state.get("sber", {})


def save_sber_state(sber_state):
    state = load_state()
    state["sber"] = sber_state
    save_state(state)


def format_accounts(accounts, highlight_threshold=None):
    """Форматирует список счетов для Telegram. Помечает превышения жирным."""
    if not accounts:
        return "(нет рублёвых р/с)"
    lines = []
    for a in accounts:
        label = account_label(a.get("accountNumber"))
        balance_str = f"{a['balance']:,.2f}".replace(",", " ")
        if highlight_threshold is not None and a["balance"] > highlight_threshold:
            lines.append(f"  {label}: <b>{balance_str} ₽</b> ⚠️")
        else:
            lines.append(f"  {label}: {balance_str} ₽")
    return "\n".join(lines)


def now_in_alert_tz():
    return datetime.now(ZoneInfo(CONFIG["alert_timezone"]))


def in_alert_window(now=None):
    """True если текущий час (в alert_timezone) в пределах [start, end]."""
    now = now or now_in_alert_tz()
    start = CONFIG["alert_hour_start"]
    end = CONFIG["alert_hour_end"]
    return start <= now.hour <= end


def build_status_message(accounts, above, sber_state):
    threshold = CONFIG["balance_threshold"]
    now = now_in_alert_tz()
    threshold_str = f"{threshold:,.0f}".replace(",", " ")
    lines = ["<b>СберБизнес — Статус р/с</b>\n"]
    lines.append(f"Порог на одном счёте: {threshold_str} ₽")
    lines.append(
        f"Окно алертов: {CONFIG['alert_hour_start']:02d}:00–"
        f"{CONFIG['alert_hour_end']:02d}:59 ({CONFIG['alert_timezone']})"
    )
    if above:
        lines.append(f"Статус: <b>превышение на {len(above)} сч.</b>")
        if in_alert_window(now):
            lines.append("Сейчас в окне — алерт отправлен.")
        else:
            lines.append(
                f"Сейчас вне окна ({now.strftime('%H:%M')}) — "
                f"тишина до {CONFIG['alert_hour_start']:02d}:00."
            )
    else:
        lines.append("Статус: ни на одном счёте нет превышения — OK")
    last_alert_hour = sber_state.get("last_alert_hour")
    if last_alert_hour:
        lines.append(f"Последний алерт: {last_alert_hour}")
    lines.append("\nСчета:")
    lines.append(format_accounts(accounts, highlight_threshold=threshold))
    return "\n".join(lines)


def check_and_alert(force_refresh=False):
    # client_secret проверяется отдельно в refresh_access_token — без него скрипт
    # работает пока access_token не истёк.
    missing = [
        k for k in ("client_id", "telegram_bot_token", "telegram_chat_id")
        if not CONFIG[k]
    ]
    if missing:
        print("Missing env vars: " + ", ".join(missing))
        return

    now = now_in_alert_tz()

    # Weekend: биржа закрыта, деньги положить нельзя → не шлём ничего.
    # Даже не ходим в API за балансами.
    if now.weekday() >= 5:
        print(f"Weekend ({now.strftime('%A')}) — skip check_and_alert entirely")
        return

    access_token, tokens = get_valid_access_token(force_refresh=force_refresh)
    check_refresh_token_expiry(tokens)

    raw_accounts = get_client_accounts(access_token)
    accounts = filter_rub_current_accounts(raw_accounts)

    statement_date = now.strftime("%Y-%m-%d")
    accounts = enrich_with_balances(access_token, accounts, statement_date)

    threshold = CONFIG["balance_threshold"]
    above = [a for a in accounts if a.get("balance", 0) > threshold]

    current_hour_key = now.strftime("%Y-%m-%dT%H")

    sber_state = get_sber_state()
    last_alert_hour = sber_state.get("last_alert_hour")

    threshold_str = f"{threshold:,.0f}".replace(",", " ")
    print(f"RUB current accounts: {len(accounts)}, above threshold: {len(above)}")
    print(f"Threshold per account: {threshold_str} ₽")
    print(f"Now in {CONFIG['alert_timezone']}: {now.strftime('%Y-%m-%d %H:%M')}")

    if not above:
        if last_alert_hour:
            print("Ни на одном счёте нет превышения, clearing alert state")
        sber_state["last_alert_hour"] = None
    elif not in_alert_window(now):
        print(
            f"Outside alert window ({CONFIG['alert_hour_start']:02d}:00–"
            f"{CONFIG['alert_hour_end']:02d}:59), skip"
        )
    elif last_alert_hour == current_hour_key:
        print(f"Already alerted this hour ({current_hour_key}), skip to avoid duplicate")
    else:
        above_lines = []
        for a in above:
            label = account_label(a.get("accountNumber"))
            balance_str = f"{a['balance']:,.2f}".replace(",", " ")
            above_lines.append(f"  {label}: <b>{balance_str} ₽</b>")
        message = (
            f"🚨 <b>СберБизнес — положи деньги на депозит</b>\n\n"
            f"Превышение порога {threshold_str} ₽ на счетах:\n"
            + "\n".join(above_lines)
            + f"\n\nВремя: {now.strftime('%d.%m.%Y %H:%M')} "
            f"({CONFIG['alert_timezone']})\n"
            f"Алерты будут приходить каждый час до "
            f"{CONFIG['alert_hour_end']:02d}:59, "
            f"пока остаток не опустится под порог."
        )
        if _alert(message):
            print(f"Alert sent for hour {current_hour_key}")
            sber_state["last_alert_hour"] = current_hour_key
        else:
            print("Failed to send alert")

    sber_state["last_accounts"] = accounts
    sber_state["threshold_rub"] = threshold
    save_sber_state(sber_state)


FINAL_WARNING_ART = r"""
                    _
                   | |
                   | |
               +---+-+---+
                   | |
             ______|_|______
            /               \
           /                 \
          /                   \
         |     ___________     |
         |    /           \    |
         |   |    .---.    |   |
         |   |   / o o \   |   |
         |   |  |   V   |  |   |
         |   |   \ --- /   |   |
         |   |   /|||||\   |   |
         |    \_/|||||\_/      |
         |      '-----'        |
         |                     |
         |    R .  I .  P .    |
         |                     |
         |  ~ ~ ~ ~ ~ ~ ~ ~ ~  |
         |                     |
         |    Твой  Депозит    |
         |      2026 — ???     |
         |                     |
         |    почти  успел     |
         |_____________________|
    ____/|                     |\____
   /     |_____________________|     \
  /_________________________________\
 . * . * . * . * . * . * . * . * . * .
"""


def send_final_warning():
    """Send a dramatic last-call ping if any RC account is still above threshold.

    Fires at ~19:55 MSK on weekdays only. At most once per day; subsequent runs
    that day stay silent. Weekend = silent (exchange closed, can't move money
    to a deposit anyway). Silent if balances are already fine.
    """
    missing = [
        k for k in ("client_id", "telegram_bot_token", "telegram_chat_id")
        if not CONFIG[k]
    ]
    if missing:
        print("Missing env vars: " + ", ".join(missing))
        return

    now = now_in_alert_tz()

    # weekend: exchange is closed, nothing to do
    if now.weekday() >= 5:
        print(f"Final warning skipped — weekend ({now.strftime('%A')})")
        return

    today_str = now.strftime("%Y-%m-%d")
    sber_state = get_sber_state()
    if sber_state.get("last_final_warning_date") == today_str:
        print(f"Final warning already sent today ({today_str}), skip")
        return

    access_token, tokens = get_valid_access_token()
    check_refresh_token_expiry(tokens)
    raw_accounts = get_client_accounts(access_token)
    accounts = filter_rub_current_accounts(raw_accounts)
    statement_date = today_str
    accounts = enrich_with_balances(access_token, accounts, statement_date)

    threshold = CONFIG["balance_threshold"]
    above = [a for a in accounts if a.get("balance", 0) > threshold]

    if not above:
        print("Final warning skipped — no accounts above threshold")
        return

    threshold_str = f"{threshold:,.0f}".replace(",", " ")
    above_lines = []
    for a in above:
        label = account_label(a.get("accountNumber"))
        balance_str = f"{a['balance']:,.2f}".replace(",", " ")
        above_lines.append(f"  • {label}: <b>{balance_str} ₽</b>")

    hours_left = max(0, CONFIG["alert_hour_end"] + 1 - now.hour)
    is_friday = now.weekday() == 4

    if is_friday:
        header = (
            f"🔴🔴🔴 <b>ПЯТНИЦА — ПОСЛЕДНИЙ ШАНС</b> 🔴🔴🔴\n"
            f"{now.strftime('%H:%M')} {CONFIG['alert_timezone']}\n\n"
            f"Не положишь → <b>3 дня простоя</b> (сб + вс + пн утром).\n"
        )
        footer = "<i>Пятница — ключевой день. Положи сейчас.</i>"
    else:
        header = (
            f"☠️ <b>ПОСЛЕДНИЙ ЗВОНОК — {now.strftime('%H:%M')} "
            f"{CONFIG['alert_timezone']}</b>\n\n"
            f"До закрытия окна: <b>{hours_left} ч.</b>\n"
        )
        footer = "<i>Положи сейчас — или ночь деньги лежат мёртвым грузом.</i>"

    # Telegram caption limit is 1024 chars — keep it lean, only essentials.
    caption = (
        header + "\n"
        + f"<b>Выше порога {threshold_str} ₽:</b>\n"
        + "\n".join(above_lines) + "\n\n"
        + footer
    )

    sent = False
    if FINAL_WARNING_IMAGE.exists():
        try:
            photo_bytes = FINAL_WARNING_IMAGE.read_bytes()
            sent = send_telegram_photo(
                photo_bytes,
                CONFIG["telegram_bot_token"],
                CONFIG["telegram_chat_id"],
                caption=caption,
                filename=FINAL_WARNING_IMAGE.name,
            )
        except OSError as e:
            log.error("Failed to read final warning image: %s", e)

    if not sent:
        # Fallback — send text-only so the final warning never silently drops
        sent = _alert(caption)

    if sent:
        print(f"Final warning sent at {now.strftime('%Y-%m-%dT%H:%M')}")
        sber_state["last_final_warning_date"] = today_str
        save_sber_state(sber_state)
    else:
        print("Failed to send final warning")


def send_friday_morning_reminder():
    """Friday 10:00 MSK heads-up: if RC balance is above threshold, nudge
    early so there's time to move it before close. If not acted on, the
    weekend eats 3 days of interest.

    - Weekday gate: only on Fridays (weekday()==4). Other days = silent.
    - Once per Friday: sber.last_friday_reminder_date in state.
    - Silent if nothing is above threshold.
    """
    missing = [
        k for k in ("client_id", "telegram_bot_token", "telegram_chat_id")
        if not CONFIG[k]
    ]
    if missing:
        print("Missing env vars: " + ", ".join(missing))
        return

    now = now_in_alert_tz()
    if now.weekday() != 4:
        print(f"Not Friday ({now.strftime('%A')}) — skip friday reminder")
        return

    today_str = now.strftime("%Y-%m-%d")
    sber_state = get_sber_state()
    if sber_state.get("last_friday_reminder_date") == today_str:
        print(f"Friday reminder already sent today ({today_str}), skip")
        return

    access_token, tokens = get_valid_access_token()
    check_refresh_token_expiry(tokens)
    raw_accounts = get_client_accounts(access_token)
    accounts = filter_rub_current_accounts(raw_accounts)
    accounts = enrich_with_balances(access_token, accounts, today_str)

    threshold = CONFIG["balance_threshold"]
    above = [a for a in accounts if a.get("balance", 0) > threshold]
    if not above:
        print("Friday reminder skipped — no accounts above threshold")
        return

    threshold_str = f"{threshold:,.0f}".replace(",", " ")
    above_lines = []
    for a in above:
        label = account_label(a.get("accountNumber"))
        balance_str = f"{a['balance']:,.2f}".replace(",", " ")
        above_lines.append(f"  • {label}: <b>{balance_str} ₽</b>")

    message = (
        f"☕ <b>Пятница — напоминание</b>\n"
        f"{now.strftime('%H:%M')} {CONFIG['alert_timezone']}\n\n"
        f"Если не положишь депозит сегодня — <b>3 дня простоя</b> "
        f"(сб + вс + пн утром).\n"
        f"Окно для перевода: до 20:00 MSK. У тебя есть "
        f"{max(0, 20 - now.hour)} ч, успеваешь в банк/звонок.\n\n"
        f"<b>Выше порога {threshold_str} ₽:</b>\n"
        + "\n".join(above_lines)
        + "\n\n<i>Это утренний пинг — вечером придёт последний звонок.</i>"
    )

    if _alert(message):
        print(f"Friday reminder sent at {now.strftime('%Y-%m-%dT%H:%M')}")
        sber_state["last_friday_reminder_date"] = today_str
        save_sber_state(sber_state)
    else:
        print("Failed to send friday reminder")


def send_status():
    access_token, tokens = get_valid_access_token()
    check_refresh_token_expiry(tokens)
    raw_accounts = get_client_accounts(access_token)
    accounts = filter_rub_current_accounts(raw_accounts)
    statement_date = now_in_alert_tz().strftime("%Y-%m-%d")
    accounts = enrich_with_balances(access_token, accounts, statement_date)
    threshold = CONFIG["balance_threshold"]
    above = [a for a in accounts if a.get("balance", 0) > threshold]
    sber_state = get_sber_state()
    message = build_status_message(accounts, above, sber_state)
    if _alert(message):
        print("Status sent")
    else:
        print("Failed to send status")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    args = sys.argv[1:]
    if "--status" in args:
        send_status()
    elif "--final-warning" in args:
        send_final_warning()
    elif "--friday-reminder" in args:
        send_friday_morning_reminder()
    elif "--force-refresh" in args:
        check_and_alert(force_refresh=True)
    else:
        check_and_alert()


if __name__ == "__main__":
    main()
