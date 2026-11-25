#!/bin/bash
# Сторож: проверяет healthz и сообщает в Telegram/MAX, если сервис упал.
# Ставится в systemd-таймер (kpbot-watchdog.timer), раз в 5 минут.

cd /opt/kpbot || exit 0

alert() {
  # не роняем сторожа, если уведомление не ушло
  su -s /bin/bash kpbot -c "cd /opt/kpbot && .venv/bin/python -m kpbot.cli alert --text \"$1\"" >/dev/null 2>&1 || true
}

CODE=$(curl -s -m 10 -o /dev/null -w '%{http_code}' http://127.0.0.1:8100/healthz || echo 000)

if [ "$CODE" != "200" ]; then
  echo "$(date -Is) healthz=$CODE — перезапускаю kpbot"
  alert "сервис не отвечает (healthz=$CODE), перезапускаю"
  systemctl restart kpbot
  sleep 5
  CODE2=$(curl -s -m 10 -o /dev/null -w '%{http_code}' http://127.0.0.1:8100/healthz || echo 000)
  echo "$(date -Is) после перезапуска healthz=$CODE2"
  if [ "$CODE2" = "200" ]; then
    alert "перезапуск помог, сервис снова отвечает"
  else
    alert "ПОСЛЕ ПЕРЕЗАПУСКА НЕ ОТВЕЧАЕТ (healthz=$CODE2) — нужен человек"
  fi
else
  echo "$(date -Is) ok"
fi
