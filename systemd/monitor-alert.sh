#!/bin/bash
# monitor-alert.sh — отправляет Telegram-уведомление об упавшем systemd-юните.
#
# Вызывается из monitor-alert@.service при OnFailure=monitor-alert@%n.service.
# Принимает имя упавшего юнита как $1, собирает последние 30 строк journalctl,
# шлёт в Telegram plain-text (без parse_mode, чтобы не споткнуться на < > &).
#
# Требует env-переменные: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
# (загружаются из /etc/default/monitor-telegram).

set -uo pipefail

unit="${1:-unknown}"
host="$(hostname)"
ts="$(date -u +'%Y-%m-%d %H:%M:%S UTC')"

# Последние 30 строк лога, обрезаем чтобы влезло в Telegram message limit (~4096)
logs="$(journalctl -u "$unit" -n 30 --no-pager -o short-iso 2>&1 | tail -c 2500)"

msg="🔴 Service failed on ${host}

Unit: ${unit}
Time: ${ts}

Last logs:
${logs}"

curl -fsS -m 15 --retry 3 --retry-connrefused \
  --data-urlencode chat_id="${TELEGRAM_CHAT_ID}" \
  --data-urlencode text="$msg" \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" >/dev/null
