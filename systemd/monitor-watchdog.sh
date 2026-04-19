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
  "sber-monitor-final-warning.timer"
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
# unit → max_age_seconds. Slack beyond nominal cadence handles clock jitter,
# boot-time backlog (OnBootSec=5min), and the weekday-only quirks where
# sber-* units skip weekends.
declare -A MAX_AGE=(
  ["openai-monitor-check"]=5400          # hourly  +  slack → 1.5h
  ["openai-monitor-status"]=93600        # daily   +  slack → 26h
  ["openai-monitor-backup"]=93600        # daily   +  slack → 26h
  ["sber-monitor-check"]=86400           # hourly during 15–19 MSK weekdays;
                                          #  heartbeat refreshed on every fire,
                                          #  generous 24h because weekends skip
  ["sber-monitor-final-warning"]=259200  # daily on weekdays → 3 days across weekend
  ["sber-monitor-friday-reminder"]=691200 # weekly → 8 days
  ["selectel-monitor-check"]=5400        # hourly  +  slack → 1.5h
  ["vdska-monitor-check"]=5400           # hourly  +  slack → 1.5h
)

HEARTBEAT_DIR=/var/lib/monitor/heartbeat
now=$(date +%s)
stale=()
for unit in "${!MAX_AGE[@]}"; do
  hb="$HEARTBEAT_DIR/${unit}.ts"
  if [[ ! -f $hb ]]; then
    # Missing file — might be brand-new deploy. Warn, but tag clearly.
    stale+=("${unit}: heartbeat file missing ($hb)")
    continue
  fi
  ts=$(cat "$hb" 2>/dev/null || echo 0)
  age=$(( now - ts ))
  max=${MAX_AGE[$unit]}
  if (( age > max )); then
    age_h=$(( age / 3600 ))
    max_h=$(( max / 3600 ))
    stale+=("${unit}: heartbeat ${age_h}h old (>${max_h}h threshold)")
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
