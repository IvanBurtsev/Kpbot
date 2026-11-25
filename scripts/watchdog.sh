#!/bin/bash
# Сторож: поднимает сервис, если healthz не отвечает. Ставится в systemd-таймер.
CODE=$(curl -s -m 10 -o /dev/null -w '%{http_code}' http://127.0.0.1:8100/healthz || echo 000)
if [ "$CODE" != "200" ]; then
  echo "$(date -Is) healthz=$CODE — перезапускаю kpbot"
  systemctl restart kpbot
  sleep 5
  CODE2=$(curl -s -m 10 -o /dev/null -w '%{http_code}' http://127.0.0.1:8100/healthz || echo 000)
  echo "$(date -Is) после перезапуска healthz=$CODE2"
else
  echo "$(date -Is) ok"
fi
