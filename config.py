"""
Конфигурация приложения LeadPilot.

Раздел 9 / 16 / 18 ТЗ: все настройки и секреты читаются ТОЛЬКО из переменных
окружения (.env), в исходном коде секретов нет. Значения по умолчанию заданы
лишь для несекретных параметров; отсутствие обязательного секрета приводит
к падению приложения на старте (fail fast), а не к работе с небезопасным
дефолтом.
"""

from functools import lru_cache
from typing import Literal

from pydantic import EmailStr, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Приложение ---
    app_name: str = "LeadPilot"
    app_version: str = "0.1.0"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False

    # --- База данных (раздел 9: PostgreSQL/Supabase, SQLite на этапе разработки) ---
    # Примеры:
    #   sqlite:///./leadpilot.db
    #   postgresql+psycopg2://user:password@host:5432/leadpilot
    database_url: str = "sqlite:///./leadpilot.db"
    db_echo: bool = False

    # Создавать таблицы из моделей при старте. Только для локальной разработки:
    # в staging/production схема управляется миграциями Alembic.
    auto_create_tables: bool = False

    # --- Аутентификация (раздел 6.1 / 16) ---
    # Обязательный секрет: без него приложение не стартует.
    jwt_secret: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 720  # 12 часов
    jwt_issuer: str = "leadpilot"

    # Кабинет на Jinja2 (раздел 9) работает через HttpOnly cookie,
    # внешние API-клиенты — через заголовок Authorization: Bearer.
    auth_cookie_name: str = "leadpilot_access_token"
    auth_cookie_secure: bool = True  # в production обязательно True (HTTPS)
    auth_cookie_samesite: Literal["lax", "strict", "none"] = "lax"

    password_min_length: int = 8

    # --- Ограничение частоты запросов (раздел 16) ---
    rate_limit_enabled: bool = True
    auth_rate_limit_attempts: int = 10
    auth_rate_limit_window_seconds: int = 300

    # --- HTTPS в production (раздел 16) ---
    force_https: bool = False

    # --- AI-модуль (разделы 9, 12) ---
    # ТЗ не фиксирует конкретного вендора («API выбранной LLM»). Основной путь —
    # OpenRouter (единый OpenAI-совместимый шлюз): достаточно AI_API_KEY и AI_MODEL.
    # openai_compatible — любой другой сервис с /chat/completions (нужен AI_API_BASE_URL).
    # stub — детерминированный офлайн-режим для локальной разработки и тестов,
    # в production запрещён (проверка при старте).
    ai_provider: Literal["stub", "openrouter", "openai_compatible"] = "stub"
    ai_api_base_url: str = "https://openrouter.ai/api/v1"
    ai_api_key: str | None = None
    # Идентификатор модели в OpenRouter, например "openai/gpt-4o-mini".
    ai_model: str | None = None
    # Для классификации можно использовать более дешёвую модель.
    ai_classifier_model: str | None = None
    ai_timeout_seconds: float = 20.0
    ai_max_retries: int = 2
    ai_temperature: float = 0.2
    # Ответ клиенту должен быть коротким (раздел 6.6).
    ai_max_response_chars: int = 700
    # Сколько последних сообщений диалога отдавать модели (раздел 6.6).
    ai_history_turns: int = 10
    # Проверка ответа AI в разделе «AI» кабинета (раздел 13). Расходует LLM-запросы,
    # поэтому ограничена по частоте на пользователя.
    ai_preview_enabled: bool = True
    ai_preview_rate_limit_per_minute: int = 20
    # Срок действия ссылки-приглашения сотрудника (раздел 13).
    invitation_ttl_days: int = 7

    # --- Первичный ADMIN (раздел 5: владелец LeadPilot) ---
    # Самостоятельная регистрация с ролью ADMIN запрещена, поэтому первый
    # администратор создаётся из переменных окружения при старте.
    # Тип EmailStr обязателен: адрес в служебном домене (.local, .test) создал бы
    # учётную запись, которой нельзя воспользоваться — вход её отклонит.
    bootstrap_admin_email: EmailStr | None = None
    bootstrap_admin_password: str | None = None

    # --- Тарифы и пробный период (этап 6, раздел 15) ---
    # ТЗ не задаёт тарифную сетку: значения — заглушки для расчёта MRR в панели ADMIN,
    # меняются переменными окружения. Платежей и автоматических подписок в MVP нет (§19).
    trial_days: int = Field(default=14, ge=1, le=365)
    plan_price_start_rub: int = Field(default=1990, ge=0)
    plan_price_pro_rub: int = Field(default=4990, ge=0)

    # --- Каналы и обработка сообщений (этап 3, разделы 11, 16, 18) ---
    # Публичный HTTPS-адрес приложения: по нему Telegram присылает webhook
    # (POST {PUBLIC_BASE_URL}/webhooks/telegram). Локально — cloudflared/ngrok.
    public_base_url: str | None = None
    telegram_api_base_url: str = "https://api.telegram.org"
    telegram_timeout_seconds: float = 10.0
    telegram_max_retries: int = 2
    # Ключ шифрования токенов ботов в integrations.credentials_ref (раздел 16).
    # Не задан — ключ выводится из JWT_SECRET; в production задавать явно.
    secrets_encryption_key: str | None = None
    # Повторная обработка (раздел 18): сообщения, застрявшие после сбоя AI/БД,
    # подхватываются фоновым циклом. 0 — цикл выключен (тесты).
    reprocess_interval_seconds: int = 60
    message_max_attempts: int = 3
    message_processing_timeout_seconds: int = 300
    # Число доверенных прокси перед приложением (Render/Cloudflare = 1..2).
    # 0 — заголовок X-Forwarded-For игнорируется: клиент может его подделать.
    trusted_proxy_count: int = 0
    webhook_rate_limit_attempts: int = 30
    webhook_rate_limit_window_seconds: int = 60

    @field_validator("database_url")
    @classmethod
    def _non_empty_database_url(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("DATABASE_URL не может быть пустым")
        return value

    @field_validator(
        "bootstrap_admin_email",
        "bootstrap_admin_password",
        "ai_api_key",
        "ai_model",
        "ai_classifier_model",
        "public_base_url",
        "secrets_encryption_key",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        """Пустая переменная в .env означает «не задано», а не пустое значение.

        В .env.example такие ключи присутствуют без значения (AI_API_KEY=,
        BOOTSTRAP_ADMIN_EMAIL=), и приложение должно стартовать как есть.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("jwt_secret")
    @classmethod
    def _reject_placeholder_secret(cls, value: str) -> str:
        """Заглушка из .env.example длиннее 32 символов и прошла бы min_length:
        приложение работало бы с публично известным секретом подписи JWT."""
        lowered = value.lower()
        if "замените" in lowered or "change_me" in lowered or "changeme" in lowered:
            raise ValueError("JWT_SECRET содержит значение-заглушку из .env.example")
        return value

    @field_validator("secrets_encryption_key")
    @classmethod
    def _check_encryption_key_length(cls, value: str | None) -> str | None:
        if value is not None and len(value) < 32:
            raise ValueError("SECRETS_ENCRYPTION_KEY должен содержать не менее 32 символов")
        return value

    @field_validator("public_base_url")
    @classmethod
    def _normalize_public_base_url(cls, value: str | None) -> str | None:
        return value.strip().rstrip("/") if value else value

    @model_validator(mode="after")
    def _check_ai_credentials(self) -> "Settings":
        """Реальный провайдер без ключа не должен молча деградировать
        в неработающий AI: ошибка видна при старте (раздел 16)."""
        if self.ai_provider != "stub":
            if not self.ai_api_key:
                raise ValueError(f"AI_PROVIDER={self.ai_provider} требует AI_API_KEY")
            if not self.ai_model:
                raise ValueError(f"AI_PROVIDER={self.ai_provider} требует AI_MODEL")
        return self

    @property
    def classifier_model(self) -> str:
        return self.ai_classifier_model or self.ai_model or ""

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    """Кэшированный доступ к настройкам (одно чтение .env за процесс)."""
    try:
        # jwt_secret читается из окружения, а не из аргументов конструктора.
        return Settings()  # pyright: ignore[reportCallIssue]
    except ValidationError as exc:
        # Понятное сообщение вместо трейсбека: чаще всего не задан JWT_SECRET.
        hint = ""
        if any(error["loc"] == ("jwt_secret",) for error in exc.errors()):
            hint = (
                "\nJWT_SECRET не задан. Создайте файл .env (скопируйте .env.example) и впишите в него\n"
                "JWT_SECRET=<результат команды>:\n"
                '  python -c "import secrets; print(secrets.token_urlsafe(48))"\n'
            )
        raise RuntimeError(
            "Некорректная конфигурация окружения. Проверьте .env (см. .env.example)."
            + hint
            + "Детали:\n"
            + str(exc)
        ) from exc


settings = get_settings()
