"""Проверка окружения перед демонстрацией, деплоем и передачей проекта.

Запуск: python scripts/doctor.py
Возвращает 1, если есть ошибки (WARN — только предупреждения).

Что проверяется:
* .env на месте, ключи не дефолтные и достаточной длины;
* боевой профиль не остался с демо-значениями;
* включённые внешние каналы укомплектованы ключами и адресами;
* в папке продукта нет чужих секретов: приватных ключей и лишних .env;
* база открывается и проходит quick_check;
* .tmp не забит каталогами прошлых тестовых прогонов.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):  # консоль Windows не знает часть символов
    sys.stdout.reconfigure(errors="replace")

from kpbot.config import DEFAULT_SECRETS, settings  # noqa: E402

OK, WARN, FAIL = "OK", "WARN", "FAIL"
_results: list[tuple[str, str, str]] = []


def check(level: str, name: str, detail: str = "") -> None:
    _results.append((level, name, detail))


def _looks_private_key(path: Path) -> bool:
    """Приватный ключ узнаём по содержимому, а не по расширению.

    Сертификаты Минцифры в kpbot/certs — публичные, их трогать не нужно.
    """
    try:
        return b"PRIVATE KEY" in path.read_bytes()[:200]
    except OSError:
        return False


def scan_secrets() -> None:
    """Лишние .env и приватные ключи внутри папки продукта."""
    skip = {"node_modules", ".git", "vendor", "__pycache__", ".venv", "venv"}
    env_copies: list[str] = []
    keys: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in skip for part in path.parts):
            continue
        relative = path.relative_to(ROOT).as_posix()
        if path.name == ".env" and relative != ".env":
            env_copies.append(relative)
        if path.name.startswith("id_") and path.suffix != ".pub":
            keys.append(relative)
        elif path.suffix in {".pem", ".key"} and _looks_private_key(path):
            keys.append(relative)

    check(OK if not env_copies else WARN, "лишние .env в проекте",
          ", ".join(env_copies[:5]) or "нет")
    check(OK if not keys else WARN, "файлы, похожие на приватные ключи",
          ", ".join(keys[:5]) or "нет")
    check(OK if not (ROOT / ".ssh").exists() else WARN, ".ssh в папке продукта",
          "вынесен" if not (ROOT / ".ssh").exists() else "лежит рядом с .env")


def check_secrets() -> None:
    env_file = ROOT / ".env"
    check(OK if env_file.exists() else FAIL, ".env на месте", str(env_file))
    if not env_file.exists():
        return

    for name, value, minimum in (
        ("KPBOT_API_KEY", settings.api_key, 24),
        ("KPBOT_SESSION_SECRET", settings.session_secret, 32),
        ("KPBOT_UI_PASSWORD", settings.ui_password, 10),
    ):
        if value in DEFAULT_SECRETS:
            check(FAIL, f"{name} не дефолтный", "остался демо-пароль")
        elif len(value) < minimum:
            check(FAIL, f"{name} длиной ≥{minimum}", f"сейчас {len(value)}")
        else:
            check(OK, f"{name} длиной ≥{minimum}", f"{len(value)} символов")

    if settings.offline:
        check(WARN, "KPBOT_OFFLINE не включён в .env",
              "включён: сервис не сможет ходить в сеть")
    else:
        check(OK, "KPBOT_OFFLINE не включён в .env", "выключен")


def check_channels() -> None:
    if settings.llm_enabled:
        if settings.llm_api_key and settings.llm_base_url and settings.llm_model:
            check(OK, "LLM включён и настроен", settings.llm_model)
        else:
            check(WARN, "LLM включён, но нет base_url/model/api_key", "разбор пойдёт правилами")
    else:
        check(OK, "LLM выключен", "разбор идёт правилами")

    if settings.telegram_enabled:
        ok = bool(settings.telegram_bot_token and settings.telegram_chat_id)
        check(OK if ok else WARN, "Telegram включён, токен и chat_id есть", "")
    if settings.max_enabled:
        ok = bool(settings.max_bot_token and (settings.max_user_id or settings.max_chat_id))
        check(OK if ok else WARN, "MAX включён, токен и адресат есть", "")

    if settings.smtp_enabled:
        ok = bool(settings.smtp_host and settings.smtp_from)
        check(OK if ok else WARN, "SMTP включён, host и from есть", "")
    else:
        check(OK, "SMTP выключен", "отправка только вручную")

    if settings.telegram_enabled or settings.max_enabled:
        local = any(host in settings.public_base_url.lower()
                    for host in ("127.0.0.1", "localhost", "0.0.0.0"))
        level = OK if not local else (FAIL if settings.is_production else WARN)
        check(level, "KPBOT_PUBLIC_BASE_URL не локальный", settings.public_base_url)

    if not (settings.oauth_client_id and settings.oauth_client_secret) and not settings.imap_password:
        check(WARN, "почта подключена", "нет ни OAuth-клиента, ни пароля приложения")
    else:
        check(OK, "почта подключена", settings.imap_user or "oauth")


def check_production() -> None:
    if not settings.is_production:
        check(OK, "KPBOT_ENV", "dev")
        return
    problems = settings.production_problems()
    check(FAIL if problems else OK, "готовность к prod",
          "; ".join(problems) if problems else "замечаний нет")


def check_database() -> None:
    path = settings.db_path
    if not path.exists():
        check(WARN, "база", "файла ещё нет — создастся при первом запуске")
        return
    try:
        with sqlite3.connect(path) as conn:
            result = conn.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.Error as exc:
        check(FAIL, "база открывается", str(exc))
        return
    check(OK if result == "ok" else FAIL, "база проходит quick_check", result)


def check_workspace() -> None:
    tmp_runs = list((ROOT / ".tmp").glob("run-*")) if (ROOT / ".tmp").exists() else []
    check(OK if not tmp_runs else WARN, "мусор тестовых прогонов",
          f"{len(tmp_runs)} каталогов run-*" if tmp_runs else "нет")
    check(OK if (ROOT / "tests" / "support.py").exists() else FAIL,
          "тестовый стенд на месте", "tests/support.py")
    check(OK if (ROOT / "crm").exists() is False else WARN, "посторонние проекты",
          "crm/ нет" if not (ROOT / "crm").exists() else "crm/ лежит внутри продукта")


def main() -> int:
    scan_secrets()
    check_secrets()
    check_channels()
    check_production()
    check_database()
    check_workspace()

    width = max(len(name) for _, name, _ in _results)
    for level, name, detail in _results:
        mark = {"OK": "[ ok ]", "WARN": "[warn]", "FAIL": "[fail]"}[level]
        print(f"{mark} {name.ljust(width)}  {detail}")

    failures = sum(1 for level, _, _ in _results if level == FAIL)
    warnings = sum(1 for level, _, _ in _results if level == WARN)
    print(f"\nитого: ошибок {failures}, предупреждений {warnings}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
