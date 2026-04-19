#!/usr/bin/env bash
# Idempotent installer for selectel-monitor + vdska-monitor + watchdog updates
# + Sber alert window narrowing + healthchecks.io ping URL.
#
# Designed to run ON THE VPS via the self-hosted GitHub Actions runner —
# no inbound SSH required, sidesteps fail2ban entirely.
#
# Inputs (env):
#   HEALTHCHECKS_URL   — optional, written to /etc/default/monitor-telegram.

set -euo pipefail

REPO_DIR=/opt/openai_monitor
ENV_FILE=/etc/default/monitor-telegram
SBER_UNIT=/etc/systemd/system/sber-monitor-check.service

cd "$REPO_DIR"

echo "==> git pull"
git pull origin master

# ── 1) read TELEGRAM tokens from existing env-file ────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
  echo "FATAL: $ENV_FILE missing — bootstrap monitor-telegram first" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$ENV_FILE"
: "${TELEGRAM_BOT_TOKEN:?missing in $ENV_FILE}"
: "${TELEGRAM_CHAT_ID:?missing in $ENV_FILE}"

# ── 2) install selectel + vdska systemd units (with token substitution) ───────
for inst in selectel vdska; do
  src="systemd/${inst}-monitor-check.service"
  dst="/etc/systemd/system/${inst}-monitor-check.service"
  echo "==> install $dst"
  sed \
    -e "s|TELEGRAM_BOT_TOKEN=REPLACE_ME|TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}|" \
    -e "s|TELEGRAM_CHAT_ID=REPLACE_ME|TELEGRAM_CHAT_ID=${TELEGRAM_CHAT_ID}|" \
    "$src" > "$dst"
  chmod 644 "$dst"

  install -m 644 "systemd/${inst}-monitor-check.timer" \
    "/etc/systemd/system/${inst}-monitor-check.timer"
done

# ── 3) refresh watchdog script (picked up new units + healthchecks ping) ──────
echo "==> install watchdog"
install -m 755 systemd/monitor-watchdog.sh /usr/local/bin/monitor-watchdog.sh

# ── 4) narrow Sber alert window 23 → 19 (no-op if already 19) ─────────────────
if [[ -f "$SBER_UNIT" ]] && grep -q '^Environment=SBER_ALERT_HOUR_END=23$' "$SBER_UNIT"; then
  echo "==> tighten SBER_ALERT_HOUR_END 23 → 19"
  sed -i 's|^Environment=SBER_ALERT_HOUR_END=23$|Environment=SBER_ALERT_HOUR_END=19|' "$SBER_UNIT"
fi

# ── 5) optional: register HEALTHCHECKS_URL into env-file (idempotent) ─────────
if [[ -n "${HEALTHCHECKS_URL:-}" ]]; then
  echo "==> register HEALTHCHECKS_URL into $ENV_FILE"
  if grep -q '^HEALTHCHECKS_URL=' "$ENV_FILE"; then
    sed -i "s|^HEALTHCHECKS_URL=.*|HEALTHCHECKS_URL=${HEALTHCHECKS_URL}|" "$ENV_FILE"
  else
    echo "HEALTHCHECKS_URL=${HEALTHCHECKS_URL}" >> "$ENV_FILE"
  fi
  chmod 600 "$ENV_FILE"
fi

# ── 6) pre-populate selectel state with currently-visible message-ids ─────────
# Prevents a burst of 4 duplicate "Пора пополнить баланс" emails on first run.
# Idempotent: only writes ids that aren't already in the bucket.
echo "==> pre-populate selectel processed_message_ids"
python3 <<'PYEOF'
import json
import sys
from pathlib import Path

sys.path.insert(0, "/opt/openai_monitor")
import selectel_monitor as s

state_file = Path("/opt/openai_monitor/monitor_state.json")
state = json.loads(state_file.read_text()) if state_file.exists() else {}
bucket = state.setdefault("selectel", {})
existing = set(bucket.get("processed_message_ids", []))

creds = s._load_credentials()
service = s._build_gmail_service(creds)
ids = s.list_message_ids(service, s.CONFIG["sender_filter"], s.CONFIG["lookback"])
new_ids = [i for i in ids if i not in existing]
combined = list(existing) + new_ids
bucket["processed_message_ids"] = combined
state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False))
print(f"  Gmail ids found: {len(ids)}; newly marked processed: {len(new_ids)}; total in bucket: {len(combined)}")
PYEOF

# ── 7) reload + enable + restart what changed ─────────────────────────────────
echo "==> systemd daemon-reload"
systemctl daemon-reload

echo "==> enable + start selectel/vdska timers"
systemctl enable --now selectel-monitor-check.timer vdska-monitor-check.timer

# Restart sber to pick up new SBER_ALERT_HOUR_END (timer fires the service,
# which is oneshot — restarting the .service is a no-op, but next timer fire
# will read the new env. Force-start once to verify.)
echo "==> trigger sber-monitor-check once (validates new env + tests connectivity)"
systemctl start sber-monitor-check.service || true

# ── 8) smoke ──────────────────────────────────────────────────────────────────
echo "==> smoke: trigger selectel + vdska oneshots"
systemctl start selectel-monitor-check.service vdska-monitor-check.service

sleep 5

echo "==> recent logs"
for unit in selectel-monitor-check vdska-monitor-check sber-monitor-check; do
  echo "---- $unit ----"
  journalctl -u "$unit.service" -n 10 --no-pager || true
done

echo "==> heartbeat files"
ls -la /var/lib/monitor/heartbeat/ 2>/dev/null || true

echo "==> active timers"
systemctl list-timers '*monitor*' --no-pager

echo "==> done"
