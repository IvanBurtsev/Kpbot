"""Конфигурация. Всё — через переменные окружения с префиксом KPBOT_ или .env."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_SECRETS = frozenset(
    {"dev-api-key-change-me", "dev-password", "dev-session-secret-change-me", "change-me", ""}
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_prefix="KPBOT_",
        extra="ignore",
        case_sensitive=False,
    )

    # --- пути ---
    env: str = "dev"
    data_dir: Path = BASE_DIR / "data"
    templates_dir: Path = BASE_DIR / "kpbot" / "templates"
    static_dir: Path = BASE_DIR / "kpbot" / "static"

    # --- доступ ---
    api_key: str = "dev-api-key-change-me"
    ui_password: str = "dev-password"
    session_secret: str = "dev-session-secret-change-me"
    session_ttl_seconds: int = 8 * 3600

    # --- продавец (реквизиты в КП) ---
    company_name: str = 'ООО «Ромашка»'
    company_inn: str = "7700000000"
    company_kpp: str = "770001001"
    company_address: str = "г. Москва, ул. Примерная, д. 1, офис 10"
    company_phone: str = "+7 (495) 000-00-00"
    company_email: str = "krzkrzkrzkrzkrzkrzkrz@gmail.com"
    company_signer: str = "Иванов И. И., коммерческий директор"
    quote_valid_days: int = 7

    # --- расчёт ---
    vat_rate: Decimal = Decimal("0.20")
    delivery_price: Decimal = Decimal("0.00")
    max_qty: Decimal = Decimal("1000000")

    # --- матчинг ---
    match_auto_threshold: float = 0.62
    match_ambiguous_threshold: float = 0.45

    # --- лимиты (защита от дурака и от DoS) ---
    max_upload_bytes: int = 2 * 1024 * 1024
    max_email_text_chars: int = 200_000
    max_rfq_lines: int = 200
    rate_limit_per_minute: int = 60

    # --- PDF ---
    pdf_enabled: bool = True
    edge_path: Path = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    pdf_timeout_seconds: int = 45

    # --- внешние сервисы (в прототипе выключены) ---
    llm_enabled: bool = False
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_timeout_seconds: float = 40.0
    llm_max_tokens: int = 1500
    smtp_enabled: bool = False

    # --- уведомления (Telegram) ---
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_api_base: str = "https://api.telegram.org"
    # для сетей, где провайдер режет отдельные адреса Telegram:
    telegram_ip_override: str = ""
    public_base_url: str = "http://127.0.0.1:8000"

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
        if self.llm_enabled and not self.llm_api_key:
            problems.append("KPBOT_LLM_ENABLED=true, но KPBOT_LLM_API_KEY не задан")
        return problems


settings = Settings()