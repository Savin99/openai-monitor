#!/bin/bash
# monitor-watchdog.sh — two-layer liveness check:
#
#   1) systemctl is-active for every critical unit (catches disabled/failed)
#   2) heartbeat freshness — each unit writes a timestamp on success; if that
#      timestamp is older than the unit's expected cadence + a slack margin,
#      something is stuck between runs even though the timer fires. Catches:
#        • OpenAI/Sber API hangs where Python blocks silently in requests
#        • Unit runs but Telegram delivery keeps failing (heartbeat skipped
#          because we sys.exit(1) on delivery failure in phase 1)
#        • Human error: unit got disabled without anyone noticing
#
# If any layer reports an issue — send Telegram alert and exit 0 (we don't
# want to fail the watchdog itself, which would ironically OnFailure us).
#
# Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (from /etc/default/monitor-telegram)

set -o pipefail

# When run from systemd, TELEGRAM_* come from EnvironmentFile=. When run
# manually for debugging, source them so the alert still has somewhere to go.
if [[ -z "${TELEGRAM_BOT_TOKEN:-}" && -f /etc/default/monitor-telegram ]]; then
  # shellcheck disable=SC1091
  source /etc/default/monitor-telegram
fi

# ── layer 1: systemctl is-active ──────────────────────────────────────────────
ACTIVE_UNITS=(
  "openai-monitor.service"
  "openai-monitor-check.timer"
  "openai-monitor-status.timer"
  "openai-monitor-backup.timer"
  "sber-monitor-check.timer"
  "sber-monitor-friday-reminder.timer"
  "selectel-monitor-check.timer"
  "vdska-monitor-check.timer"
  "actions.runner.Savin99-openai-monitor.vds-openai-monitor.service"
)

inactive=()
for unit in "${ACTIVE_UNITS[@]}"; do
  state="$(systemctl is-active "$unit" 2>&1 || true)"
  if [[ "$state" != "active" ]]; then
    inactive+=("${unit} → ${state}")
  fi
done

# ── layer 2: heartbeat freshness ──────────────────────────────────────────────
# unit → timer. Compare heartbeat with the timer's actual LastTriggerUSec instead
# of hard-coding cadence in two places. If a timer schedule changes from hourly to
# daily, watchdog follows systemd and will not create false stale alerts.
declare -A HEARTBEAT_TIMERS=(
  ["openai-monitor-check"]="openai-monitor-check.timer"
  ["openai-monitor-status"]="openai-monitor-status.timer"
  ["openai-monitor-backup"]="openai-monitor-backup.timer"
  ["sber-monitor-check"]="sber-monitor-check.timer"
  ["sber-monitor-friday-reminder"]="sber-monitor-friday-reminder.timer"
  ["selectel-monitor-check"]="selectel-monitor-check.timer"
  ["vdska-monitor-check"]="vdska-monitor-check.timer"
)

declare -A HEARTBEAT_GRACE=(
  ["sber-monitor-friday-reminder"]=3600
)
DEFAULT_HEARTBEAT_GRACE=1800

timer_last_trigger_ts() {
  local timer="$1"
  local raw
  raw="$(systemctl show "$timer" --property=LastTriggerUSec --value 2>/dev/null || true)"
  if [[ -z "$raw" || "$raw" == "n/a" || "$raw" == "0" ]]; then
    return 1
  fi
  date -d "$raw" +%s 2>/dev/null
}

HEARTBEAT_DIR=/var/lib/monitor/heartbeat
now=$(date +%s)
stale=()
for unit in "${!HEARTBEAT_TIMERS[@]}"; do
  hb="$HEARTBEAT_DIR/${unit}.ts"
  timer="${HEARTBEAT_TIMERS[$unit]}"
  grace="${HEARTBEAT_GRACE[$unit]:-$DEFAULT_HEARTBEAT_GRACE}"
  last_trigger="$(timer_last_trigger_ts "$timer" || true)"

  if [[ ! -f $hb ]]; then
    if [[ -n "$last_trigger" ]]; then
      lag=$(( now - last_trigger ))
      if (( lag > grace )); then
        stale+=("${unit}: heartbeat file missing after ${timer} fired $(( lag / 60 ))m ago")
      fi
    else
      stale+=("${unit}: heartbeat file missing and ${timer} has no trigger history")
    fi
    continue
  fi

  ts=$(cat "$hb" 2>/dev/null || echo 0)
  if [[ -z "$last_trigger" ]]; then
    continue
  fi

  lag=$(( last_trigger - ts ))
  if (( lag > grace )); then
    trigger_age=$(( now - last_trigger ))
    if (( trigger_age > grace )); then
      stale+=("${unit}: heartbeat older than ${timer} last trigger by $(( lag / 60 ))m (>$(( grace / 60 ))m grace)")
    fi
  fi
done

# ── healthchecks.io ping ──────────────────────────────────────────────────────
# External liveness signal — survives full-VPS outages (when watchdog itself
# can't deliver anything). HC alerts via its own channels (email/Telegram).
# Set HEALTHCHECKS_URL=https://hc-ping.com/<uuid> in /etc/default/monitor-telegram.
hc_ping() {
  local suffix="$1"   # empty for success, "/fail" for failure
  [[ -z "${HEALTHCHECKS_URL:-}" ]] && return 0
  curl -fsS -m 10 --retry 3 --retry-connrefused \
    "${HEALTHCHECKS_URL}${suffix}" >/dev/null 2>&1 || true
}

# ── report ────────────────────────────────────────────────────────────────────
if [[ ${#inactive[@]} -eq 0 && ${#stale[@]} -eq 0 ]]; then
  hc_ping ""
  exit 0
fi
hc_ping "/fail"

host="$(hostname)"
ts_str="$(date -u +'%Y-%m-%d %H:%M:%S UTC')"
msg="⚠️ Watchdog on ${host} at ${ts_str}"
if [[ ${#inactive[@]} -gt 0 ]]; then
  msg+=$'\n\n'"Units not active:"
  for line in "${inactive[@]}"; do msg+=$'\n'" - $line"; done
fi
if [[ ${#stale[@]} -gt 0 ]]; then
  msg+=$'\n\n'"Stale heartbeats:"
  for line in "${stale[@]}"; do msg+=$'\n'" - $line"; done
fi

curl -fsS -m 15 --retry 3 --retry-connrefused \
  --data-urlencode chat_id="${TELEGRAM_CHAT_ID}" \
  --data-urlencode text="$msg" \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" >/dev/null
