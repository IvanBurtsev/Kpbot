"""Ротация секретов, которые сервис генерирует сам.

Меняет в .env только KPBOT_API_KEY и KPBOT_SESSION_SECRET (или UI-пароль с --password).
Ключи почты, LLM и мессенджеров перевыпускаются у провайдера — скрипт печатает,
где именно, но сам их не трогает.

    python scripts/rotate_secrets.py --dry-run     # показать, что будет записано
    python scripts/rotate_secrets.py               # записать новые значения

После запуска перезапустите сервис: старые сессии и API-клиенты перестанут работать.
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

if hasattr(sys.stdout, "reconfigure"):  # консоль Windows не знает часть символов
    sys.stdout.reconfigure(errors="replace")

MANUAL_STEPS = """\
Перевыпустите вручную (скрипт не делает это за вас):
  1. Telegram-бот: @BotFather → /mybots → ваш бот → API Token → Revoke current token.
  2. MAX-бот: кабинет партнёра MAX → перевыпуск токена бота.
  3. LLM-провайдер: кабинет → API keys → отозвать старый ключ, создать новый.
  4. Пароль приложения почты: настройки почты → пароли приложений → удалить старый.
После — обновите .env и перезапустите сервис (systemctl restart kpbot)."""


def new_value(name: str) -> str:
    if name == "KPBOT_API_KEY":
        return secrets.token_urlsafe(24)      # ~32 символа
    if name == "KPBOT_SESSION_SECRET":
        return secrets.token_urlsafe(36)      # ~48 символов
    return secrets.token_urlsafe(12)          # UI-пароль, ~16 символов


def rewrite(text: str, updates: dict[str, str]) -> tuple[str, dict[str, str]]:
    applied: dict[str, str] = {}
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(?P<key>[A-Z_]+)\s*=", line)
        if not match or match.group("key") not in updates:
            continue
        key = match.group("key")
        lines[index] = f"{key}={updates[key]}"
        applied[key] = updates[key]
    return "\n".join(lines) + "\n", applied


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="только показать новые значения")
    parser.add_argument("--password", action="store_true", help="ещё и новый пароль интерфейса")
    args = parser.parse_args()

    if not ENV_FILE.exists():
        print(f"нет файла {ENV_FILE}: сначала скопируйте .env.example в .env")
        return 1

    names = ["KPBOT_API_KEY", "KPBOT_SESSION_SECRET"] + (["KPBOT_UI_PASSWORD"] if args.password else [])
    text = ENV_FILE.read_text(encoding="utf-8")
    updates = {name: new_value(name) for name in names}
    new_text, applied = rewrite(text, updates)

    missing = [name for name in names if name not in applied]
    if missing:
        print("в .env нет переменных: " + ", ".join(missing))
    if not applied:
        return 1

    for name, value in applied.items():
        print(f"{name}: {value[:6]}… ({len(value)} символов)")

    if args.dry_run:
        print("\n--dry-run: файл не изменён")
        return 0

    ENV_FILE.write_text(new_text, encoding="utf-8")
    print(f"\nзаписано в {ENV_FILE.name}. Перезапустите сервис.")
    print(MANUAL_STEPS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
