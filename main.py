"""
Точка входа LeadPilot — FastAPI-приложение.

Этап 1 («Локальное ядро»): конфигурация, БД, аутентификация, RBAC, компании
и услуги.
Этап 2 («AI pipeline»): классификация, контекст бизнеса, генерация и проверка
ответа — модуль ai/ + services/ai_service.py.
Этап 3 («Telegram»): webhook, сохранение и обработка сообщений, отправка ответов,
подключение бота компании, фоновая повторная обработка (раздел 18).

Лиды, кабинет и админ-панель подключаются на своих этапах — их роутеры уже
зарегистрированы пустыми, чтобы структура (Приложение A ТЗ) и слои
(раздел 18) не менялись при добавлении функций.

Запуск:  uvicorn main:app --reload
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from config import settings
from database import Base, SessionLocal, engine
from models import LogLevel, User, UserRole, UserStatus
from routes import admin as admin_routes
from routes import auth as auth_routes
from routes import businesses as businesses_routes
from routes import integrations as integrations_routes
from routes import leads as leads_routes
from routes import messages as messages_routes
from services import audit_service, message_service
from services.auth_service import hash_password

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# URL запросов к Telegram содержат токен бота — библиотеки HTTP не должны
# печатать их в логи (раздел 16).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("leadpilot")

BASE_DIR = Path(__file__).resolve().parent


def _check_production_safety() -> None:
    """Раздел 16 ТЗ: небезопасная конфигурация не должна попасть в production."""
    if not settings.is_production:
        return
    problems: list[str] = []
    if settings.auto_create_tables:
        problems.append("AUTO_CREATE_TABLES=true (в production схему меняют миграции)")
    if not settings.auth_cookie_secure:
        problems.append("AUTH_COOKIE_SECURE=false (cookie уйдёт по HTTP)")
    if settings.debug:
        problems.append("DEBUG=true")
    if settings.ai_provider == "stub":
        # Офлайн-режим AI — только для разработки (раздел 12).
        problems.append("AI_PROVIDER=stub (в production нужен реальный LLM-провайдер)")
    if not settings.secrets_encryption_key:
        problems.append(
            "SECRETS_ENCRYPTION_KEY не задан (токены ботов шифруются ключом от JWT_SECRET)"
        )
    if not (settings.public_base_url or "").lower().startswith("https://"):
        problems.append("PUBLIC_BASE_URL должен быть https-адресом (webhook Telegram)")
    if problems:
        raise RuntimeError("Недопустимая конфигурация production: " + "; ".join(problems))


def _bootstrap_admin() -> None:
    """Создание первого ADMIN (раздел 5) из переменных окружения.

    Публичная регистрация всегда создаёт OWNER, поэтому владелец LeadPilot
    появляется только так — пароль в коде не хранится.
    """
    if not (settings.bootstrap_admin_email and settings.bootstrap_admin_password):
        return

    email = settings.bootstrap_admin_email.strip().lower()
    with SessionLocal() as db:
        existing = db.scalar(select(User).where(User.email == email))
        if existing is not None:
            logger.info("ADMIN %s уже существует, создание пропущено", email)
            return
        admin = User(
            email=email,
            password_hash=hash_password(settings.bootstrap_admin_password),
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
        )
        db.add(admin)
        db.flush()
        audit_service.log_event(
            db,
            event_type=audit_service.EventType.ADMIN_BOOTSTRAPPED,
            message=f"Создан администратор платформы {email}",
            actor_user_id=admin.id,
        )
        db.commit()
        logger.info("Создан администратор платформы %s", email)


async def _reprocess_loop() -> None:
    """Раздел 18: сообщения, застрявшие после сбоя AI/БД/Telegram, подхватываются
    и обрабатываются повторно. Защита от двойной обработки — атомарный захват
    в message_service, поэтому несколько воркеров не мешают друг другу."""
    while True:
        try:
            handled = await run_in_threadpool(message_service.reprocess_pending)
            if handled:
                logger.info("Повторная обработка: обработано записей — %s", handled)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - цикл не должен умирать из-за одного сбоя
            logger.exception("Сбой цикла повторной обработки сообщений")
        await asyncio.sleep(settings.reprocess_interval_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_production_safety()
    if settings.auto_create_tables:
        # Только локальная разработка: быстрый старт без Alembic.
        logger.warning("AUTO_CREATE_TABLES=true — создаю таблицы из моделей")
        Base.metadata.create_all(bind=engine)
    _bootstrap_admin()
    logger.info(
        "%s %s запущен (environment=%s, db=%s)",
        settings.app_name,
        settings.app_version,
        settings.environment,
        "sqlite" if settings.is_sqlite else "postgresql",
    )
    reprocess_task = None
    if settings.reprocess_interval_seconds > 0:
        reprocess_task = asyncio.create_task(_reprocess_loop())
    try:
        yield
    finally:
        if reprocess_task is not None:
            reprocess_task.cancel()
            with suppress(asyncio.CancelledError):
                await reprocess_task


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "Микро-SaaS обработки входящих заявок. Этапы 1–3: ядро, авторизация, "
        "компании, услуги, AI pipeline, Telegram."
    ),
    lifespan=lifespan,
    # Документация закрыта в production (раздел 16: не раскрывать лишнего).
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/openapi.json",
)

if settings.force_https:
    # Раздел 16: HTTPS в production.
    from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware

    app.add_middleware(HTTPSRedirectMiddleware)

# Статика и шаблоны кабинета (раздел 9: Jinja2). Страницы — этап 5.
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# --- Этап 1 ---
app.include_router(auth_routes.router)
app.include_router(auth_routes.me_router)
app.include_router(businesses_routes.router)
# --- Этап 3 ---
app.include_router(integrations_routes.router)
app.include_router(messages_routes.router)  # webhook и диалоги (ручной ответ — этап 4)
# --- Зарегистрированы заранее, наполняются на своих этапах ---
app.include_router(leads_routes.router)  # этап 4
app.include_router(admin_routes.router)  # этап 6


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Раздел 17: неожиданные ошибки попадают в system_logs.
    Клиенту не отдаются детали исключения."""
    logger.exception("Необработанная ошибка: %s %s", request.method, request.url.path)
    try:
        with SessionLocal() as db:
            audit_service.log_event(
                db,
                event_type=audit_service.EventType.UNHANDLED_ERROR,
                message=f"{type(exc).__name__}: {exc}",
                level=LogLevel.ERROR,
                payload={"path": request.url.path, "method": request.method},
                commit=True,
            )
    except Exception:  # noqa: BLE001 - БД может быть недоступна
        logger.exception("Не удалось записать ошибку в system_logs")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Внутренняя ошибка сервера"},
    )


@app.get("/health", tags=["service"])
def health() -> dict:
    """Проверка живости для хостинга (Render/Railway)."""
    return {"status": "ok", "version": settings.app_version}


@app.get("/", tags=["service"])
def root() -> dict:
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "stage": "3 — Telegram",
    }
