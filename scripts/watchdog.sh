#!/bin/bash
# Сторож: проверяет healthz и журнал ошибок, сообщает в Telegram/MAX.
# Ставится в systemd-таймер (kpbot-watchdog.timer), раз в 5 минут.
#
# Что делает:
#   1) healthz не 200 → сообщение и перезапуск, потом отчёт о результате;
#   2) серия ошибок в журнале (по умолчанию 20 за 10 минут) → одно сообщение
#      в час, чтобы не залить чат одинаковыми строками;
#   3) всё в порядке → тишина.
#
# Настройки через окружение (можно задать в /etc/default/kpbot-watchdog):
#   WATCHDOG_ERROR_LIMIT   порог ошибок за окно (по умолчанию 20)
#   WATCHDOG_WINDOW_MIN    окно наблюдения в минутах (по умолчанию 10)
#   WATCHDOG_ALERT_WINDOW  не чаще одного сообщения за столько секунд (3600)
#   WATCHDOG_HEALTHZ       адрес проверки (http://127.0.0.1:8100/healthz)

set -u

cd /opt/kpbot || exit 0

HEALTHZ="${WATCHDOG_HEALTHZ:-http://127.0.0.1:8100/healthz}"
ERROR_LIMIT="${WATCHDOG_ERROR_LIMIT:-20}"
WINDOW_MIN="${WATCHDOG_WINDOW_MIN:-10}"
ALERT_WINDOW="${WATCHDOG_ALERT_WINDOW:-3600}"

STATE_DIR=/var/lib/kpbot-watchdog
mkdir -p "$STATE_DIR" 2>/dev/null || STATE_DIR=/tmp
ERROR_STAMP="$STATE_DIR/last-error-alert"

alert() {
  # не роняем сторожа, если уведомление не ушло
  su -s /bin/bash kpbot -c "cd /opt/kpbot && .venv/bin/python -m kpbot.cli alert --text \"$1\"" >/dev/null 2>&1 || true
}

now() { date +%s; }

# ---------- 1. сервис отвечает? ----------
CODE=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$HEALTHZ" || echo 000)

if [ "$CODE" != "200" ]; then
  echo "$(date -Is) healthz=$CODE — перезапускаю kpbot"
  alert "сервис не отвечает (healthz=$CODE), перезапускаю"
  systemctl restart kpbot
  sleep 5
  CODE2=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$HEALTHZ" || echo 000)
  echo "$(date -Is) после перезапуска healthz=$CODE2"
  if [ "$CODE2" = "200" ]; then
    alert "перезапуск помог, сервис снова отвечает"
  else
    alert "ПОСЛЕ ПЕРЕЗАПУСКА НЕ ОТВЕЧАЕТ (healthz=$CODE2) — нужен человек"
  fi
  exit 0
fi

echo "$(date -Is) healthz=ok"

# ---------- 2. серия ошибок в журнале ----------
if command -v journalctl >/dev/null 2>&1; then
  ERRORS=$(journalctl -u kpbot --since "-${WINDOW_MIN} min" -p err --no-pager 2>/dev/null | wc -l | tr -d ' ')
  ERRORS=${ERRORS:-0}

  if [ "$ERRORS" -ge "$ERROR_LIMIT" ]; then
    LAST=0
    if [ -f "$ERROR_STAMP" ]; then
      LAST=$(cat "$ERROR_STAMP" 2>/dev/null || echo 0)
    fi
    LAST=${LAST:-0}
    if [ $(( $(now) - LAST )) -ge "$ALERT_WINDOW" ]; then
      SAMPLE=$(journalctl -u kpbot --since "-${WINDOW_MIN} min" -p err --no-pager 2>/dev/null \
        | tail -n 3 | cut -c1-160 | tr '\n' ' ')
      echo "$(date -Is) ошибок за ${WINDOW_MIN} мин: $ERRORS — сообщаю"
      alert "ошибки в журнале: $ERRORS за ${WINDOW_MIN} мин. Последние: $SAMPLE"
      now > "$ERROR_STAMP"
    else
      echo "$(date -Is) ошибок $ERRORS, но о таком уже сообщали меньше часа назад — молчу"
    fi
  fi
fi
