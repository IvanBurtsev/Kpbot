"""Конфигурация. Всё — через переменные окружения с префиксом KPBOT_ или .env."""

from __future__ import annotations

import os

from decimal import Decimal
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


def _resolve_env_file() -> str | None:
    """Какой .env читать.

    По умолчанию — проектный. KPBOT_ENV_FILE позволяет указать другой файл или
    отключить чтение совсем (none/off/пусто): так тесты не зависят от боевых
    ключей и не могут случайно отправить письмо или запрос в LLM.
    """
    raw = os.environ.get("KPBOT_ENV_FILE")
    if raw is None:
        return str(BASE_DIR / ".env")
    raw = raw.strip()
    if raw.lower() in {"", "none", "off", "0", "no"}:
        return None
    return raw


_ENV_FILE = _resolve_env_file()

DEFAULT_SECRETS = frozenset(
    {"dev-api-key-change-me", "dev-password", "dev-session-secret-change-me", "change-me", ""}
)


# Российские сервисы (MAX, Госуслуги и др.) подписаны корневым сертификатом Минцифры,
# которого нет в certifi. Если системный бандл на месте — используем его.
_SYSTEM_CA = "/etc/ssl/certs/ca-certificates.crt"
if os.path.exists(_SYSTEM_CA):
    os.environ.setdefault("SSL_CERT_FILE", _SYSTEM_CA)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_prefix="KPBOT_",
        extra="ignore",
        case_sensitive=False,
    )

    # --- пути ---
    env: str = "dev"
    data_dir: Path = BASE_DIR / "data"
    # тестовый режим: реальные сетевые вызовы запрещены (см. kpbot/net.py)
    offline: bool = False
    templates_dir: Path = BASE_DIR / "kpbot" / "templates"
    static_dir: Path = BASE_DIR / "kpbot" / "static"

    # --- доступ ---
    api_key: str = "dev-api-key-change-me"
    ui_password: str = "dev-password"
    session_secret: str = "dev-session-secret-change-me"
    session_ttl_seconds: int = 8 * 3600
    # Вход общим паролем (без пользователя) — только для разработки/демо.
    # В проде он закрыт, пока явно не разрешён: иначе нельзя ни отозвать доступ,
    # ни сказать, кто именно одобрил КП.
    allow_shared_login: bool = False
    # Ключ шифрования секретов в базе (пароли ящиков клиентов).
    # Пусто — берём session_secret: отдельный ключ лучше, но дефолт не должен
    # требовать ручной настройки у существующих установок.
    secret_key: str = ""

    # --- продавец (реквизиты в КП) ---
    company_name: str = 'ООО «Ромашка»'
    company_inn: str = "7700000000"
    company_kpp: str = "770001001"
    company_address: str = "г. Москва, ул. Примерная, д. 1, офис 10"
    company_phone: str = "+7 (495) 000-00-00"
    company_email: str = "sales@example.ru"
    company_signer: str = "Иванов И. И., коммерческий директор"
    quote_valid_days: int = 7

    # --- расчёт ---
    # С 01.01.2026 основная ставка НДС в РФ — 22% (была 20%). Ставка задаётся
    # конфигом: у клиента может быть 10%, 7%, 5% или 0%.
    vat_rate: Decimal = Decimal("0.22")
    delivery_price: Decimal = Decimal("0.00")
    max_qty: Decimal = Decimal("1000000")
    # Прайс клиента уже с НДС (типичная выгрузка из 1С/розничного каталога):
    # тогда НДС выделяется из цены, а не начисляется сверху.
    prices_include_vat: bool = False

    # --- матчинг ---
    match_auto_threshold: float = 0.75
    match_ambiguous_threshold: float = 0.50

    # --- лимиты (защита от дурака и от DoS) ---
    max_upload_bytes: int = 2 * 1024 * 1024
    max_logo_bytes: int = 200 * 1024
    max_email_text_chars: int = 200_000
    max_rfq_lines: int = 200
    rate_limit_per_minute: int = 60
    # IP обратного прокси, которым можно верить в X-Forwarded-For (через запятую).
    # Пусто — заголовок игнорируется, лимит считается по адресу соединения.
    trusted_proxies: str = ""

    # --- PDF ---
    pdf_enabled: bool = True
    edge_path: Path = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    pdf_timeout_seconds: int = 45

    # --- внешние сервисы (по умолчанию выключены) ---
    llm_enabled: bool = False
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_timeout_seconds: float = 40.0
    llm_max_tokens: int = 4000
    # сколько символов письма отправлять в модель; остальное разберут правила
    llm_max_input_chars: int = 24000
    # тариф провайдера за 1 млн токенов, ₽. 0 = тариф не задан, считаем только токены
    llm_price_input_rub_per_1m: float = 0.0
    llm_price_output_rub_per_1m: float = 0.0
    # --- отправка КП клиенту (SMTP) ---
    smtp_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_security: str = "starttls"  # starttls (587) | ssl (465) | none (только для тестов)
    smtp_timeout_seconds: int = 30

    # --- уведомления (Telegram) ---
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_api_base: str = "https://api.telegram.org"
    # для сетей, где провайдер режет отдельные адреса Telegram:
    telegram_ip_override: str = ""
    public_base_url: str = "http://127.0.0.1:8000"

    # --- уведомления: MAX (мессенджер) ---
    max_enabled: bool = False
    max_bot_token: str = ""
    max_user_id: str = ""
    max_chat_id: str = ""
    max_api_base: str = "https://platform-api2.max.ru"

    # --- 1С (OData) ---
    one_c_enabled: bool = False
    one_c_base_url: str = ""
    one_c_user: str = ""
    one_c_password: str = ""
    one_c_timeout_seconds: float = 30.0
    # HTTP-сервис в 1С: им пишутся документы с табличными частями (OData их не принимает)
    one_c_service_url: str = ""
    # Общий секрет HTTP-сервиса: без него любой, кто видит адрес, создаёт счета
    one_c_service_token: str = ""

    # --- почта (IMAP) ---
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    imap_limit: int = 10
    imap_mark_seen: bool = True
    imap_mark_skipped: bool = True

    # --- OAuth2 (Gmail без «пароля приложения») ---
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_redirect_port: int = 8765

    @property
    def db_path(self) -> Path:
        return self.data_dir / "kpbot.sqlite3"

    @property
    def secrets_key(self) -> str:
        """Чем шифруем секреты в базе: KPBOT_SECRET_KEY или секрет сессии."""
        return self.secret_key or self.session_secret

    @property
    def trusted_proxy_set(self) -> set[str]:
        return {item.strip() for item in self.trusted_proxies.split(",") if item.strip()}

    @property
    def tenants_db_path(self) -> Path:
        """Реестр клиентов: отдельный файл рядом с основными данными."""
        return self.data_dir / "tenants.sqlite3"

    @property
    def tenants_dir(self) -> Path:
        """Каталог баз клиентов. Основная база остаётся в data/."""
        return self.data_dir / "tenants"

    @property
    def out_dir(self) -> Path:
        return self.data_dir / "out"

    @property
    def samples_dir(self) -> Path:
        return self.data_dir / "samples"

    @property
    def oauth_token_path(self) -> Path:
        return self.data_dir / "oauth_token.json"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.out_dir, self.samples_dir):
            path.mkdir(parents=True, exist_ok=True)

    @property
    def is_production(self) -> bool:
        return self.env.strip().lower() in {"prod", "production"}

    @property
    def shared_login_allowed(self) -> bool:
        """Общий пароль интерфейса — только если явно разрешён и это не прод."""
        return self.allow_shared_login and not self.is_production

    def production_problems(self) -> list[str]:
        """Что нельзя оставлять дефолтным на боевом сервере."""
        problems: list[str] = []
        if self.api_key in DEFAULT_SECRETS:
            problems.append("KPBOT_API_KEY не задан или остался дефолтным")
        if self.ui_password in DEFAULT_SECRETS:
            problems.append("KPBOT_UI_PASSWORD не задан или остался дефолтным")
        if self.session_secret in DEFAULT_SECRETS:
            problems.append("KPBOT_SESSION_SECRET не задан или остался дефолтным")
        if len(self.api_key) < 24:
            problems.append("KPBOT_API_KEY короче 24 символов")
        if len(self.session_secret) < 32:
            problems.append("KPBOT_SESSION_SECRET короче 32 символов")
        if not (self.oauth_client_id and self.oauth_client_secret) and not self.imap_password:
            problems.append("почта не подключена: нет ни OAuth-клиента, ни пароля приложения")
        notifications_on = self.telegram_enabled or self.max_enabled
        if notifications_on and any(host in (self.public_base_url or "").lower()
                                    for host in ("127.0.0.1", "localhost", "0.0.0.0")):
            problems.append(
                "уведомления включены, но KPBOT_PUBLIC_BASE_URL локальный: "
                "ссылки в мессенджерах не откроются у получателя"
            )
        if self.llm_enabled and not self.llm_api_key:
            problems.append("KPBOT_LLM_ENABLED=true, но KPBOT_LLM_API_KEY не задан")
        return problems


settings = Settings()

if settings.offline:  # тесты: ни одного настоящего пакета наружу
    from .net import install_offline_guard

    install_offline_guard()