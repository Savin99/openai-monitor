#!/usr/bin/env python3
"""
Selectel monitor.

По умолчанию работает как email-forwarder для старых инстансов. Для Selectel
может работать через Billing API: проверяет баланс по таймеру и шлёт Telegram
не чаще одного раза в день, если баланс ниже порога.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

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
    "mode": os.environ.get("SELECTEL_MONITOR_MODE", "email"),
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
    "api_token": os.environ.get("SELECTEL_API_TOKEN", ""),
    "api_base_url": os.environ.get("SELECTEL_API_BASE_URL", "https://api.selectel.ru"),
    "balance_threshold": float(os.environ.get("SELECTEL_BALANCE_THRESHOLD", "1000")),
    "amount_scale": float(os.environ.get("SELECTEL_AMOUNT_SCALE", "100")),
    "transactions_days": int(os.environ.get("SELECTEL_TRANSACTIONS_DAYS", "90")),
    "topup_lookup_days": int(os.environ.get("SELECTEL_TOPUP_LOOKBACK_DAYS", "365")),
    "alert_timezone": os.environ.get("SELECTEL_ALERT_TIMEZONE", "Europe/Moscow"),
}


def _money(value, currency="RUB"):
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return f"{value} {currency}".strip()
    return f"{amount:,.2f} {currency}".replace(",", " ")


def _currency_label(currency):
    if not currency:
        return "RUB"
    cur = str(currency).upper()
    return "RUB" if cur == "RUB" else cur


def _billing_label(value):
    labels = {
        "primary": "Основной баланс",
        "storage": "Объектное хранилище",
        "vmware": "VMware",
        "vpc": "VPC",
    }
    return labels.get(str(value).lower(), str(value))


def _balance_label(value):
    labels = {
        "main": "основной",
        "vk_rub": "VK Cloud",
        "bonus": "бонусы",
    }
    return labels.get(str(value).lower(), str(value))


def _debt_status_label(value):
    labels = {
        "success": "нет задолженности",
        "fail": "есть задолженность",
        "blocked": "заблокировано",
    }
    return labels.get(str(value).lower(), str(value or "неизвестно"))


def _api_amount(value):
    return float(value or 0) / CONFIG["amount_scale"]


def _hours_to_days(hours):
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        return None
    if hours <= 0:
        return "0 ч"
    days = hours / 24
    if days >= 2:
        return f"{hours:.0f} ч (~{days:.0f} дн.)"
    return f"{hours:.0f} ч"


def _prediction_line(prediction):
    if not prediction:
        return None
    pred = prediction.get("data", {})
    primary = _hours_to_days(pred.get("primary"))
    if not primary:
        return None
    return f"Хватит примерно на <b>{primary}</b>"


def _alert_timezone():
    try:
        return ZoneInfo(CONFIG["alert_timezone"])
    except Exception:
        log.warning(
            "Invalid SELECTEL_ALERT_TIMEZONE=%r, falling back to UTC",
            CONFIG["alert_timezone"],
        )
        return timezone.utc


def _selectel_headers():
    if not CONFIG["api_token"]:
        raise RuntimeError("SELECTEL_API_TOKEN is missing")
    return {"X-Token": CONFIG["api_token"], "Accept": "application/json"}


def _selectel_get(path, timeout=30):
    url = CONFIG["api_base_url"].rstrip("/") + path
    try:
        resp = requests.get(url, headers=_selectel_headers(), timeout=timeout)
    except requests.RequestException as e:
        raise RuntimeError(f"Selectel API network error: {e}") from e
    if resp.status_code != 200:
        raise RuntimeError(f"Selectel API HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as e:
        raise RuntimeError("Selectel API returned invalid JSON") from e


def fetch_selectel_balances():
    """Return raw Selectel balance payload from Billing API."""
    return _selectel_get("/v3/balances")


def fetch_selectel_prediction():
    """Return raw Selectel prediction payload from Billing API."""
    return _selectel_get("/v2/billing/prediction", timeout=8)


def fetch_selectel_transactions(days=None, limit=500):
    """Return raw Selectel account transactions for the last N days."""
    days = days or CONFIG["transactions_days"]
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)
    params = {
        "created_from": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "created_to": now.strftime("%Y-%m-%dT%H:%M:%S"),
        "limit": limit,
        "offset": 0,
        "without_removed": "true",
    }
    all_items = []
    while True:
        query = urlencode(params)
        payload = _selectel_get(f"/v2/billing/transactions?{query}", timeout=20)
        items = payload.get("data", []) or []
        all_items.extend(items)
        if len(items) < limit:
            break
        params["offset"] += limit
    return {"status": "success", "data": all_items}


def _parse_tx_datetime(value):
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _outgoing_for_period(txs, start, end):
    total = 0.0
    for tx in txs:
        dt = _parse_tx_datetime(tx.get("ts"))
        if not dt:
            continue
        local_dt = dt.astimezone(start.tzinfo)
        if start <= local_dt < end and tx["amount"] < 0:
            total -= tx["amount"]
    return round(total, 2)


def summarize_transactions(payload, current_balance, currency, now=None):
    """Summarize transactions and reconstruct historical balance points."""
    tz = _alert_timezone()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local_now = now.astimezone(tz)
    today = local_now.date()
    yesterday_start = datetime.combine(
        today - timedelta(days=1),
        datetime.min.time(),
        tz,
    )
    today_start = datetime.combine(today, datetime.min.time(), tz)
    week_start = local_now - timedelta(days=7)

    txs = []
    for item in payload.get("data", []) or []:
        dt = _parse_tx_datetime(item.get("created"))
        if not dt:
            continue
        amount = _api_amount(item.get("price"))
        desc = (
            (item.get("public_description") or {}).get("ru")
            or (item.get("public_description") or {}).get("en")
            or (item.get("description") or {}).get("ru")
            or (item.get("description") or {}).get("en")
            or item.get("transaction_group")
            or item.get("transaction_type")
            or "операция"
        )
        txs.append(
            {
                "ts": dt.isoformat(),
                "amount": round(amount, 2),
                "description": str(desc),
                "dir": item.get("dir") or ("incoming" if amount > 0 else "outgoing"),
                "balance": item.get("balance"),
                "state": item.get("state"),
            }
        )
    txs.sort(key=lambda x: x["ts"])

    incoming = round(sum(t["amount"] for t in txs if t["amount"] > 0), 2)
    outgoing = round(-sum(t["amount"] for t in txs if t["amount"] < 0), 2)
    outgoing_yesterday = _outgoing_for_period(txs, yesterday_start, today_start)
    outgoing_week = _outgoing_for_period(txs, week_start, local_now)
    payments = [t for t in txs if t["amount"] > 0]
    last_payment = None
    if payments:
        payment = payments[-1]
        payment_dt = _parse_tx_datetime(payment["ts"])
        if payment_dt:
            local_payment_dt = payment_dt.astimezone(tz)
            last_payment = {
                "ts": payment["ts"],
                "amount": payment["amount"],
                "date": local_payment_dt.strftime("%d.%m.%Y"),
                "days_since": max(
                    (local_now.date() - local_payment_dt.date()).days,
                    0,
                ),
            }

    balance = float(current_balance)
    reversed_points = [{"ts": datetime.now(timezone.utc).isoformat(), "balance": balance}]
    for tx in reversed(txs):
        balance -= tx["amount"]
        reversed_points.append({"ts": tx["ts"], "balance": round(balance, 2)})
    history = list(reversed(reversed_points))

    return {
        "currency": currency,
        "incoming": incoming,
        "outgoing": outgoing,
        "outgoing_yesterday": outgoing_yesterday,
        "outgoing_week": outgoing_week,
        "last_payment": last_payment,
        "payments": payments[-5:],
        "transactions": txs,
        "history": history,
    }


def summarize_balances(payload):
    """Normalize /v3/balances response for alerting and Telegram output."""
    data = payload.get("data", {})
    settings = data.get("settings", {})
    currency = _currency_label(settings.get("currency") or "RUB")
    billings = data.get("billings", []) or []

    billing_summaries = []
    total_final = 0.0
    total_debt = 0.0
    total_balances = 0.0
    for billing in billings:
        balances = billing.get("balances", []) or []
        balance_sum = _api_amount(billing.get("balances_values_sum"))
        debt_sum = _api_amount(billing.get("debt_sum"))
        final_sum = _api_amount(
            billing.get("final_sum")
            if billing.get("final_sum") is not None
            else (float(billing.get("balances_values_sum") or 0)
                  - float(billing.get("debt_sum") or 0))
        )
        total_balances += balance_sum
        total_debt += debt_sum
        total_final += final_sum
        billing_summaries.append(
            {
                "type": billing.get("billing_type") or "unknown",
                "balance_sum": balance_sum,
                "debt_sum": debt_sum,
                "final_sum": final_sum,
                "balances": [
                    {
                        "type": b.get("balance_type") or str(b.get("balance_id")),
                        "value": _api_amount(b.get("value")),
                    }
                    for b in balances
                ],
            }
        )

    return {
        "currency": currency,
        "debt_status": data.get("debt_status") or "unknown",
        "total_balances": round(total_balances, 2),
        "total_debt": round(total_debt, 2),
        "total_final": round(total_final, 2),
        "billings": billing_summaries,
    }


def format_api_status(summary, prediction=None, force_alert=False):
    """Build HTML Telegram status for Selectel Billing API."""
    currency = summary["currency"]
    title = "Selectel — баланс"
    if force_alert:
        title = "⚠️ Selectel — баланс ниже порога"
    lines = [f"<b>{title}</b>", ""]
    lines.append(f"Сейчас: <b>{_money(summary['total_final'], currency)}</b>")
    lines.append(f"Порог: {_money(CONFIG['balance_threshold'], currency)}")
    if summary["total_debt"]:
        lines.append(f"Задолженность: <b>{_money(summary['total_debt'], currency)}</b>")
    else:
        lines.append(f"Задолженность: {_debt_status_label(summary['debt_status'])}")

    prediction_line = _prediction_line(prediction)
    if prediction_line:
        lines.append(prediction_line)

    if summary["billings"]:
        lines.append("")
        lines.append("<b>Детали:</b>")
        for billing in summary["billings"]:
            parts = [
                f"{_balance_label(b['type'])} {_money(b['value'], currency)}"
                for b in billing["balances"]
                if b["value"] != 0
            ]
            suffix = f" ({', '.join(parts)})" if parts else ""
            lines.append(
                f"• {html.escape(_billing_label(billing['type']))}: "
                f"{_money(billing['final_sum'], currency)}{suffix}"
            )
    return "\n".join(lines)


def format_api_report(summary, prediction=None, transactions=None, force_alert=False):
    """Build concise human-friendly Selectel report."""
    currency = summary["currency"]
    title = "Selectel — баланс"
    if force_alert:
        title = "⚠️ Selectel — баланс ниже порога"
    lines = [f"<b>{title}</b>", ""]
    lines.append(f"Сейчас: <b>{_money(summary['total_final'], currency)}</b>")
    lines.append(f"Порог: {_money(CONFIG['balance_threshold'], currency)}")
    if summary["total_debt"]:
        lines.append(f"Задолженность: <b>{_money(summary['total_debt'], currency)}</b>")

    prediction_line = _prediction_line(prediction)
    if prediction_line:
        lines.append(prediction_line)

    if transactions:
        days = CONFIG["transactions_days"]
        lines.append("")
        lines.append("<b>Списано:</b>")
        lines.append(
            f"Вчера: {_money(transactions.get('outgoing_yesterday', 0), currency)}"
        )
        lines.append(
            f"За 7 дней: {_money(transactions.get('outgoing_week', 0), currency)}"
        )

        lines.append("")
        last_payment = transactions.get("last_payment")
        if last_payment:
            lines.append(
                "Последнее пополнение: "
                f"{last_payment['date']}, "
                f"+{_money(last_payment['amount'], currency)} "
                f"({last_payment['days_since']} дн. назад)"
            )
        else:
            lines.append(
                "Последнее пополнение: "
                f"не найдено за {CONFIG['topup_lookup_days']} дней"
            )

        lines.append("")
        lines.append(f"<b>За {days} дней:</b>")
        lines.append(f"Списано: {_money(transactions['outgoing'], currency)}")

    return "\n".join(lines)


def _selectel_state_key():
    return f"{CONFIG['instance']}_api"


def _today_in_alert_timezone():
    return datetime.now(_alert_timezone()).date().isoformat()


def _last_low_alert_date():
    state = load_state()
    bucket = state.setdefault(_selectel_state_key(), {})
    return bucket.get("last_low_alert_date")


def _mark_low_alert_sent(alert_date):
    state = load_state()
    bucket = state.setdefault(_selectel_state_key(), {})
    bucket["last_low_alert_date"] = alert_date
    save_state(state)


def record_api_history(summary, max_points=180):
    """Store balance history for Selectel charts."""
    state = load_state()
    bucket = state.setdefault(_selectel_state_key(), {})
    history = list(bucket.get("balance_history", []))
    now = datetime.now(timezone.utc).isoformat()
    history.append(
        {
            "ts": now,
            "balance": summary["total_final"],
            "currency": summary["currency"],
        }
    )
    bucket["balance_history"] = history[-max_points:]
    save_state(state)
    return bucket["balance_history"]


def send_api_report(summary, prediction, transactions, history, force_alert=False):
    """Send Selectel status with a balance chart, falling back to text."""
    msg = format_api_report(summary, prediction, transactions, force_alert=force_alert)
    try:
        import charts

        png = charts.build_selectel_balance_chart(
            history=history,
            threshold=CONFIG["balance_threshold"],
            currency=summary["currency"],
            title_suffix=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )
    except Exception as e:
        log.error("Selectel chart build failed: %s", e)
        png = None

    if png:
        ok = send_telegram_photo(
            png,
            CONFIG["telegram_bot_token"],
            CONFIG["telegram_chat_id"],
            caption=msg,
            parse_mode="HTML",
            filename="selectel-balance.png",
        )
        if ok:
            return True
        log.warning("Selectel chart send failed, falling back to text-only")

    return send_telegram_alert(
        msg, CONFIG["telegram_bot_token"], CONFIG["telegram_chat_id"]
    )


def check_api_balance(send_ok_status=False):
    """Check Selectel balance via API. Alert only below threshold unless forced."""
    balances_payload = fetch_selectel_balances()
    summary = summarize_balances(balances_payload)
    try:
        prediction = fetch_selectel_prediction()
    except RuntimeError as e:
        log.warning("Selectel prediction unavailable: %s", e)
        prediction = None
    try:
        transactions = summarize_transactions(
            fetch_selectel_transactions(),
            current_balance=summary["total_final"],
            currency=summary["currency"],
        )
        if (
            not transactions.get("last_payment")
            and CONFIG["topup_lookup_days"] > CONFIG["transactions_days"]
        ):
            topup_transactions = summarize_transactions(
                fetch_selectel_transactions(days=CONFIG["topup_lookup_days"]),
                current_balance=summary["total_final"],
                currency=summary["currency"],
            )
            transactions["last_payment"] = topup_transactions.get("last_payment")
    except RuntimeError as e:
        log.warning("Selectel transactions unavailable: %s", e)
        transactions = None

    remaining = summary["total_final"]
    threshold = CONFIG["balance_threshold"]
    is_low = remaining <= threshold
    alert_date = _today_in_alert_timezone()
    low_alert_due = is_low and _last_low_alert_date() != alert_date
    history = record_api_history(summary)
    if transactions and transactions.get("history"):
        history = transactions["history"]
    if low_alert_due or send_ok_status:
        if not send_api_report(
            summary, prediction, transactions, history, force_alert=is_low
        ):
            print("Failed to send Selectel API status")
            sys.exit(1)
        if low_alert_due:
            _mark_low_alert_sent(alert_date)

    if is_low:
        print(f"Selectel balance LOW: {_money(remaining, summary['currency'])}")
        if not low_alert_due and not send_ok_status:
            print(f"Selectel low alert already sent for {alert_date}")
    else:
        print(f"Selectel balance OK: {_money(remaining, summary['currency'])}")
    return remaining


def _load_credentials():
    """Read token, refresh if expired. Returns google.oauth2.credentials.Credentials."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

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
    from googleapiclient.discovery import build

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
    if CONFIG["mode"] == "api":
        check_api_balance(send_ok_status=True)
        print("Status sent")
        return

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
    if CONFIG["mode"] == "api":
        check_api_balance(send_ok_status="--status" in args)
    elif "--status" in args:
        send_status()
    else:
        forward_new()
    touch_heartbeat(f"{CONFIG['instance']}-monitor-check")


if __name__ == "__main__":
    main()
