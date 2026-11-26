#!/usr/bin/env bash
# Деплой kpbot на сервер: обновление кода, зависимостей, .env и перезапуск.
#
#   sudo KPBOT_DIR=/opt/kpbot bash scripts/deploy.sh
#
# Идемпотентен: повторный запуск безопасен. Если после перезапуска не отвечает
# healthz, скрипт возвращает предыдущий коммит и поднимает сервис обратно.

set -euo pipefail

APP_DIR="${KPBOT_DIR:-/opt/kpbot}"
SERVICE="${KPBOT_SERVICE:-kpbot}"
BRANCH="${KPBOT_BRANCH:-main}"
HEALTH_URL="${KPBOT_HEALTH_URL:-http://127.0.0.1:8000/healthz}"
PYTHON="${PYTHON:-python3}"

log() { printf '\n== %s\n' "$*"; }
die() { printf '\nОШИБКА: %s\n' "$*" >&2; exit 1; }

set_env() {  # set_env KEY VALUE — меняет или добавляет строку в .env
    local key="$1" value="$2" file="$APP_DIR/.env"
    [ -f "$file" ] || die "нет $file — скопируйте .env.example и заполните (docs/DEPLOY.md)"
    if grep -q "^${key}=" "$file"; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$file"
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}

log "каталог: $APP_DIR"
[ -d "$APP_DIR/.git" ] || die "$APP_DIR — не git-репозиторий; сначала разверните код (docs/DEPLOY.md)"
cd "$APP_DIR"

log "бэкап базы перед обновлением"
if [ -x .venv/bin/python ]; then
    .venv/bin/python -m kpbot.cli backup --keep 14 || echo "бэкап пропущен — деплой продолжаем"
fi

PREVIOUS="$(git rev-parse HEAD)"
log "код: ветка $BRANCH, было $PREVIOUS"
git fetch --prune origin "$BRANCH"
git checkout -q "$BRANCH"
git pull --ff-only

if [ ! -x .venv/bin/python ]; then
    log "виртуальное окружение"
    "$PYTHON" -m venv .venv
fi
log "зависимости"
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

log ".env: НДС 22% и вход общим паролем выключен"
set_env KPBOT_VAT_RATE 0.22
set_env KPBOT_ALLOW_SHARED_LOGIN false

log "перезапуск $SERVICE"
systemctl restart "$SERVICE"
sleep 2

if curl -fsS --max-time 10 "$HEALTH_URL" >/dev/null; then
    log "healthz: $(curl -fsS --max-time 5 "$HEALTH_URL")"
    log "готово, версия $(git rev-parse --short HEAD)"
    exit 0
fi

log "healthz не ответил — откат на $PREVIOUS"
git reset --hard "$PREVIOUS"
systemctl restart "$SERVICE"
sleep 2
if curl -fsS --max-time 10 "$HEALTH_URL" >/dev/null; then
    die "деплой отменён: код возвращён на $PREVIOUS, сервис работает"
fi
die "сервис не поднялся; смотрите journalctl -u $SERVICE -n 100"
