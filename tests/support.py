"""Общий тестовый стенд: изолированные данные, без .env и без сети.

Импортируется первым в каждом тестовом модуле — до любого `import kpbot`.
Так прогон не зависит от боевых ключей в .env, от прошлых запусков и от
порядка модулей: у каждого прогона свой каталог данных.
"""

from __future__ import annotations

import atexit
import os
import shutil
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
_TMP_ROOT = BASE_DIR / ".tmp"
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

# Свой каталог данных на прогон: прошлые запуски не влияют на результат.
# Не tempfile.mkdtemp: в песочнице Windows такой каталог получает ACL без доступа.
ROOT = _TMP_ROOT / f"run-{os.getpid()}-{time.time_ns()}"
ROOT.mkdir(parents=True)

# .env проекта не читаем: тесты не должны видеть боевые ключи.
os.environ["KPBOT_ENV_FILE"] = "none"
# Любой настоящий сетевой вызов — ошибка (см. kpbot/net.py).
os.environ["KPBOT_OFFLINE"] = "1"

os.environ["KPBOT_DATA_DIR"] = str(ROOT)
os.environ["KPBOT_ENV"] = "dev"
os.environ["KPBOT_API_KEY"] = "test-key"
os.environ["KPBOT_UI_PASSWORD"] = "test-pass"
os.environ["KPBOT_SESSION_SECRET"] = "test-secret"

os.environ["KPBOT_PDF_ENABLED"] = "false"
os.environ["KPBOT_LLM_ENABLED"] = "false"
# Пороги как в боевом .env: тесты не должны зависеть от настроек окружения.
os.environ["KPBOT_MATCH_AUTO_THRESHOLD"] = "0.75"
os.environ["KPBOT_MATCH_AMBIGUOUS_THRESHOLD"] = "0.50"
os.environ["KPBOT_TELEGRAM_ENABLED"] = "false"
os.environ["KPBOT_MAX_ENABLED"] = "false"
os.environ["KPBOT_SMTP_ENABLED"] = "false"
os.environ["KPBOT_IMAP_USER"] = ""
os.environ["KPBOT_IMAP_PASSWORD"] = ""
os.environ["KPBOT_OAUTH_CLIENT_ID"] = ""
os.environ["KPBOT_OAUTH_CLIENT_SECRET"] = ""

# Вход в интерфейс в тестах идёт с одного адреса: лимит не должен мешать.
os.environ["KPBOT_RATE_LIMIT_PER_MINUTE"] = "100000"


def subdir(name: str) -> Path:
    """Каталог внутри прогона: для файлов, которые тест пишет сам."""
    path = ROOT / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup() -> None:
    """Удаляет каталог прогона, если не просили сохранить его для отладки."""
    if os.environ.get("KPBOT_TESTS_KEEP") == "1":
        return
    shutil.rmtree(ROOT, ignore_errors=True)


atexit.register(cleanup)
