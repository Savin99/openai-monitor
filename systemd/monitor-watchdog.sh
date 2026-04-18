#!/bin/bash
# monitor-watchdog.sh — проверяет, что все критичные юниты и таймеры активны.
# Запускается monitor-watchdog.timer каждые 15 минут. Если хотя бы один unit
# не в состоянии `active` — шлёт Telegram-алерт со списком.
#
# Защищает от тихих «inactive/dead» ситуаций, когда OnFailure не стреляет
# (например, если юнит просто остановлен или таймер снят с enable).
#
# Требует env-переменные: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
# (из /etc/default/monitor-telegram).

set -uo pipefail

CRITICAL=(
  "openai-monitor.service"
  "openai-monitor-check.timer"
  "openai-monitor-status.timer"
  "sber-monitor-check.timer"
  "actions.runner.Savin99-openai-monitor.vds-openai-monitor.service"
)

fails=()
for unit in "${CRITICAL[@]}"; do
  state="$(systemctl is-active "$unit" 2>&1 || true)"
  if [[ "$state" != "active" ]]; then
    fails+=("${unit} → ${state}")
  fi
done

if [[ ${#fails[@]} -eq 0 ]]; then
  exit 0
fi

host="$(hostname)"
ts="$(date -u +'%Y-%m-%d %H:%M:%S UTC')"
printf -v body '%s\n' "${fails[@]}"

msg="⚠️ Watchdog on ${host} at ${ts}

Units not active:
${body}"

curl -fsS -m 15 --retry 3 --retry-connrefused \
  --data-urlencode chat_id="${TELEGRAM_CHAT_ID}" \
  --data-urlencode text="$msg" \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" >/dev/null
