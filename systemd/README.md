# systemd/ — конфиги для self-healing и alerting на VPS

> Эти файлы — **копия того, что лежит на VPS** в `/etc/systemd/system/` и `/usr/local/bin/`.
> Сам CI/CD их НЕ раскатывает автоматически (см. TODO). При изменениях здесь — синхронизировать руками на сервер.

## Раскладка на VPS

| Локально (в этой папке) | На VPS |
|-----|-----|
| `monitor-alert.sh` | `/usr/local/bin/monitor-alert.sh` (chmod +x) |
| `monitor-watchdog.sh` | `/usr/local/bin/monitor-watchdog.sh` (chmod +x) |
| `monitor-alert@.service` | `/etc/systemd/system/monitor-alert@.service` |
| `monitor-watchdog.service` | `/etc/systemd/system/monitor-watchdog.service` |
| `monitor-watchdog.timer` | `/etc/systemd/system/monitor-watchdog.timer` |
| `monitor-telegram.example` | (только пример) На VPS руками создать `/etc/default/monitor-telegram` с реальными значениями, `chmod 600`. |
| `drop-ins/<unit>.override.conf` | `/etc/systemd/system/<unit>.d/override.conf` |

## Логика

1. **`monitor-alert@<unit>.service`** — шаблон-сервис. Любой критичный юнит с `OnFailure=monitor-alert@%n.service` при падении автоматически стартует `monitor-alert@<своё-имя>.service`, который вызывает `monitor-alert.sh <имя>` → Telegram.
2. **`monitor-watchdog.timer`** — каждые 15 мин. Проверяет `systemctl is-active` для списка критичных юнитов. Если хоть один не `active` — алерт. Защищает от «тихих» inactive случаев.
3. **Drop-in overrides** — добавляют `OnFailure=` на critical юниты без правки оригинала:
   - `openai-monitor.service` (бот): + `Restart=always`, `StartLimitIntervalSec=0` (без штрафного лимита)
   - `openai-monitor-check.service`, `openai-monitor-status.service`, `sber-monitor-check.service`: только `OnFailure`
   - `actions.runner.*.service`: добавляем `Restart=on-failure`, RestartSec=30, OnFailure

## Установка на чистом VPS / после правки

```bash
# 1. env-файл с секретами
install -m 600 /dev/stdin /etc/default/monitor-telegram <<'EOF'
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
EOF

# 2. скрипты
install -m 755 monitor-alert.sh /usr/local/bin/
install -m 755 monitor-watchdog.sh /usr/local/bin/

# 3. unit-файлы
install -m 644 monitor-alert@.service /etc/systemd/system/
install -m 644 monitor-watchdog.service /etc/systemd/system/
install -m 644 monitor-watchdog.timer /etc/systemd/system/

# 4. drop-ins
for unit in openai-monitor openai-monitor-check openai-monitor-status sber-monitor-check; do
  mkdir -p /etc/systemd/system/${unit}.service.d
  install -m 644 drop-ins/${unit}.service.override.conf \
    /etc/systemd/system/${unit}.service.d/override.conf
done
mkdir -p /etc/systemd/system/actions.runner.Savin99-openai-monitor.vds-openai-monitor.service.d
install -m 644 drop-ins/actions.runner.override.conf \
  /etc/systemd/system/actions.runner.Savin99-openai-monitor.vds-openai-monitor.service.d/override.conf

# 5. activate
systemctl daemon-reload
systemctl enable --now monitor-watchdog.timer

# 6. smoke test — сымитировать падение
systemctl start monitor-alert@fake-unit.service   # должно прислать Telegram
```

## Smoke test

```bash
# Ручной тест alert-шаблона: в Telegram должно прийти сообщение с unit=fake.service
/usr/local/bin/monitor-alert.sh fake.service

# Ручной тест watchdog: подменить список и запустить
/usr/local/bin/monitor-watchdog.sh
# (если всё работает — молчит, иначе шлёт список в Telegram)
```

## TODO

- [ ] Добавить в CI `deploy` job шаг, который раскатывает изменённые файлы из `systemd/` на VPS (сейчас вручную)
- [ ] Внешний pinger (healthchecks.io) для случая, когда VPS вообще недоступен
