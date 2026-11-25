# Деплой kpbot на сервер

Документ описывает то, что можно сделать без доступа к серверу. Когда будут доступы —
останется выполнить шаги и сверить чек-лист.

## 1. Что нужно от тебя (без этого деплой не начать)

| Что | Зачем |
|---|---|
| ОС и версия сервера | Ubuntu 22.04/24.04 — инструкция ниже готова; Windows Server — другой путь (см. п. 6) |
| Домен или поддомен | например `kp.example.ru`, на него ставится HTTPS |
| Доступ: SSH-ключ или логин+пароль | **пароль в чат не присылай**, лучше ключ; либо запусти команды сам, я дам текстом |
| Решение по базе | на старте SQLite (один клиент), при 3+ клиентах — Postgres |
| Реквизиты продавца для КП | название, ИНН, КПП, адрес, телефон, подписант |

## 2. Требования к серверу

- 1 vCPU, 2 ГБ RAM, 10 ГБ диска — достаточно для одного клиента;
- Python 3.11+ (на 3.12/3.13 тоже работает);
- исходящий доступ к `imap.gmail.com:993` и `oauth2.googleapis.com:443`;
- входящий только 80/443 (приложение слушает 127.0.0.1, наружу — через reverse proxy).

## 3. Установка (Ubuntu, systemd + Caddy)

```bash
# 1. пользователь и каталог
sudo useradd --system --create-home --shell /usr/sbin/nologin kpbot
sudo mkdir -p /opt/kpbot && sudo chown kpbot:kpbot /opt/kpbot

# 2. код (rsync/scp/git — как удобнее)
sudo -u kpbot rsync -a --exclude data --exclude vendor --exclude .tmp ./ kpbot@server:/opt/kpbot/
#    локально:
#    rsync -a --exclude data --exclude vendor --exclude .tmp ./ root@SERVER:/opt/kpbot/

# 3. окружение
cd /opt/kpbot
sudo -u kpbot python3 -m venv .venv
sudo -u kpbot .venv/bin/pip install -U pip
sudo -u kpbot .venv/bin/pip install -r requirements.txt

# 4. конфигурация
sudo -u kpbot cp .env.example .env
sudo -u kpbot nano .env          # см. п. 4

# 5. первый прогон и проверка
sudo -u kpbot .venv/bin/python -m kpbot.cli demo --fresh
sudo -u kpbot .venv/bin/python -m kpbot.cli mail-status

# 6. сервис
sudo cp deploy/kpbot.service /etc/systemd/system/kpbot.service
sudo systemctl daemon-reload
sudo systemctl enable --now kpbot
sudo systemctl status kpbot --no-pager

# 7. HTTPS
sudo apt install -y caddy
sudo tee /etc/caddy/Caddyfile >/dev/null <<'EOF'
kp.example.ru {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8000
}
EOF
sudo systemctl reload caddy
```

Caddy сам получит сертификат. Если домен ещё не направлен на сервер — сначала DNS, потом Caddy.

## 4. Обязательные изменения в `.env` для прода

```ini
KPBOT_ENV=prod
KPBOT_API_KEY=<длинный случайный>
KPBOT_UI_PASSWORD=<сильный пароль>
KPBOT_SESSION_SECRET=<длинная случайная строка>
KPBOT_COMPANY_*=<реальные реквизиты>
KPBOT_OAUTH_CLIENT_ID=...
KPBOT_OAUTH_CLIENT_SECRET=...
KPBOT_IMAP_USER=...
KPBOT_PDF_ENABLED=true

# LLM (необязательно, но с ней recall 100% против 83%)
KPBOT_LLM_ENABLED=true
KPBOT_LLM_BASE_URL=https://api.aitunnel.ru/v1
KPBOT_LLM_MODEL=deepseek-v4.1-flash-1017
KPBOT_LLM_API_KEY=<ключ провайдера>
```

С `KPBOT_ENV=prod` сервис **не стартует**, если остались дефолтные секреты, ключ короче
24 символов, секрет сессии короче 32 или почта не подключена. Это защита от «забыл поменять
пароль» — ошибка будет видна сразу в `journalctl -u kpbot`, а не через месяц.

Проверка живости для прокси и монитора: `GET /healthz` → `{"status":"ok"}` (без авторизации,
ничего не раскрывает).

Сгенерировать секреты:

```bash
python scripts/rotate_secrets.py --dry-run   # посмотреть, что будет записано
python scripts/rotate_secrets.py             # записать API-ключ и секрет сессии
```

Ключи почты, LLM и мессенджеров скрипт не трогает — их перевыпускают у провайдера
(@BotFather, кабинет MAX, кабинет LLM). Скрипт печатает, где именно.

### Гигиена секретов и папки

- `.env` один на окружение: стенд и прод — разные файлы, значения не переиспользуются.
- `.env` и приватные ключи не попадают в архив/git: перед передачей проекта проверь
  `git status --ignored` и **не** отправляй `.tmp`, `data`, `vendor`.
- SSH-ключ деплоя живёт в `~/.ssh`, а не в папке продукта: `ssh -i ~/.ssh/kpbot_deploy ...`.
- Перед показом клиенту и после переезда: `python scripts/doctor.py` — проверяет длину
  секретов, дефолтные пароли, укомплектованность каналов, целостность базы и мусор
  от тестов. Выход 1 — есть ошибки, которые надо починить до демо.

### Проверка перед стартом

```bash
python scripts/doctor.py
```

OAuth-токен получается **на сервере** (`python -m kpbot.cli oauth-login`). Для этого
браузер нужен только один раз — можно получить токен локально и скопировать
`data/oauth_token.json` на сервер, тогда redirect сработает на твоей машине.

## 5. Эксплуатация

### HTTPS без домена

Если домена нет, работает бесплатный quick-туннель Cloudflare: сертификат настоящий,
покупать ничего не нужно. Минус — адрес меняется при перезапуске туннеля.

```bash
cat > /etc/systemd/system/kpbot-tunnel.service <<'UNIT'
[Unit]
Description=kpbot HTTPS tunnel
After=network-online.target kpbot.service
Wants=kpbot.service

[Service]
Type=simple
ExecStart=/usr/local/bin/cloudflared tunnel --url http://127.0.0.1:8100 --no-autoupdate
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl enable --now kpbot-tunnel
```

Адрес берётся из журнала и записывается в `.env` скриптом `scripts/sync_tunnel_url.sh`
(проверяет, что адрес отвечает 200, и только потом обновляет `KPBOT_PUBLIC_BASE_URL`
и перезапускает сервис). Он же оформлен как `kpbot-tunnel-url.service`.
Приложение при этом слушает только `127.0.0.1` — наружу торчит лишь туннель.

### PDF на сервере без браузера

`WeasyPrint` как запасной движок: `pip install weasyprint` + системные библиотеки
`libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 fonts-dejavu-core`.
Сначала пробуется браузер, если его нет — WeasyPrint.

### Почта: отдельная папка

Опрос лучше нацелить на папку с заявками, а не на весь ящик:

```ini
KPBOT_IMAP_FOLDER=kpbot
```

Папка создаётся один раз (IMAP `CREATE`), письма в неё кладёт правило почтового сервиса.

### Cron и сторож

```bash
cat > /etc/cron.d/kpbot <<'CRON'
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
*/10 * * * * kpbot cd /opt/kpbot && .venv/bin/python -m kpbot.cli poll --limit 20 >> /opt/kpbot/data/poll.log 2>&1
0 3 * * * kpbot cd /opt/kpbot && .venv/bin/python -m kpbot.cli backup --keep 14 >> /opt/kpbot/data/backup.log 2>&1
CRON
```

`kpbot-watchdog.timer` каждые 5 минут проверяет `/healthz` и перезапускает сервис,
если он не отвечает.

### Основное

```bash
# бэкап базы (по cron, каждый день в 3:00)
crontab -u kpbot -e
0 3 * * * cd /opt/kpbot && .venv/bin/python -m kpbot.cli backup --keep 14 >> data/backups.log 2>&1

# забрать почту вручную
cd /opt/kpbot && sudo -u kpbot .venv/bin/python -m kpbot.cli poll --limit 5

# логи
journalctl -u kpbot -n 100 --no-pager
```

Опрос почты раз в N минут: пока нет планировщика внутри приложения, ставится cron:

```bash
*/10 * * * * cd /opt/kpbot && .venv/bin/python -m kpbot.cli poll --limit 20 >> data/poll.log 2>&1
```

## 6. Windows Server (если сервер не Linux)

- Python 3.12 с python.org, venv, `pip install -r requirements.txt`;
- запуск как служба: [NSSM](https://nssm.cc/) или `sc.exe create` с обёрткой;
- HTTPS: Caddy для Windows или IIS ARR как reverse proxy;
- бэкап: Планировщик задач Windows → `python -m kpbot.cli backup --keep 14`.

## 7. Чек-лист перед показом клиенту

- [ ] `KPBOT_UI_PASSWORD`, `KPBOT_API_KEY`, `KPBOT_SESSION_SECRET` — не дефолтные;
- [ ] HTTPS работает, `http://` редиректит на `https://`;
- [ ] `mail-check` отвечает `подключение ок (XOAUTH2)`;
- [ ] cron на `poll` и `backup` создан, лог не пустой;
- [ ] `.env` не в git (`.gitignore`), права `600`;
- [ ] проверено восстановление из бэкапа (скопировать файл и открыть `mail-status`);
- [ ] клиент подписал согласие на обработку ПДн, в КП верные реквизиты.

## 8. Чего в этой версии нет (осознанно)

- Postgres и мультитенантность — при 3+ клиентах;
- роли и права — сейчас один пароль на всех;
- биллинг и самостоятельная регистрация;
- мониторинг Prometheus и алерты;
- PDF на сервере включится сам, если есть Chromium/Chrome (на Windows-песочнице он недоступен),
  иначе остаются HTML и DOCX.