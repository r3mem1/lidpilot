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
import mimetypes
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.exceptions import HTTPException as StarletteHTTPException

from config import settings
from database import Base, SessionLocal, engine
from models import LogLevel, User, UserRole, UserStatus
from monitoring import init_monitoring
from routes import admin as admin_routes
from routes import admin_pages as admin_pages_routes
from routes import auth as auth_routes
from routes import businesses as businesses_routes
from routes import cabinet as cabinet_routes
from routes import integrations as integrations_routes
from routes import leads as leads_routes
from routes import messages as messages_routes
from routes import team as team_routes
from services import audit_service, message_service, rate_limit_service
from services.auth_service import hash_password
from templating import templates

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# URL запросов к Telegram содержат токен бота — библиотеки HTTP не должны
# печатать их в логи (раздел 16).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("leadpilot")
# Sentry — до создания приложения, чтобы интеграция FastAPI подхватила его (раздел 17).
MONITORING_ENABLED = init_monitoring()

BASE_DIR = Path(__file__).resolve().parent

# Windows не знает тип .woff2, а строгий nosniff не позволит браузеру угадать его.
mimetypes.add_type("font/woff2", ".woff2")


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


async def _log_retention_loop() -> None:
    """Раздел 17: system_logs не растёт бесконечно — старые события удаляются
    раз в system_logs_purge_interval_hours (первый запуск — при старте).
    Этап 9: заодно удаляются закрытые окна счётчиков rate limit."""
    while True:
        try:
            deleted = await run_in_threadpool(
                audit_service.purge_old_logs, settings.system_logs_retention_days
            )
            if deleted:
                logger.info("Очистка system_logs: удалено событий — %s", deleted)
            await run_in_threadpool(rate_limit_service.purge_expired)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - цикл не должен умирать из-за одного сбоя
            logger.exception("Сбой очистки system_logs")
        await asyncio.sleep(settings.system_logs_purge_interval_hours * 3600)


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
    logger.info("Внешний трекер ошибок Sentry: %s", "включён" if MONITORING_ENABLED else "выключен")
    tasks: list[asyncio.Task] = []
    if settings.reprocess_interval_seconds > 0:
        tasks.append(asyncio.create_task(_reprocess_loop()))
    if settings.system_logs_purge_interval_hours > 0:
        tasks.append(asyncio.create_task(_log_retention_loop()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


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

# Статика кабинета (раздел 9: HTML/CSS/JS + Jinja2, шаблоны — templating.py).
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# --- Этап 1 ---
app.include_router(auth_routes.router)
app.include_router(auth_routes.me_router)
app.include_router(businesses_routes.router)
# --- Этап 3 ---
app.include_router(integrations_routes.router)
# --- Этап 5: сотрудники и приглашения (раздел 13) ---
app.include_router(team_routes.router)
app.include_router(messages_routes.router)  # webhook и диалоги (ручной ответ — этап 4)
# --- Зарегистрированы заранее, наполняются на своих этапах ---
app.include_router(leads_routes.router)  # этап 4
app.include_router(admin_routes.router)  # этап 6: JSON API (раздел 11)
app.include_router(admin_pages_routes.router)  # этап 6: страницы панели (раздел 15)
# --- Этап 5: кабинет бизнеса (страницы) ---
app.include_router(cabinet_routes.router)


# Страницы кабинета (этап 5): им задаётся строгая политика содержимого. Swagger (/docs)
# использует внешние скрипты и сюда не входит — в production он закрыт.
_CABINET_PREFIXES = ("/cabinet", "/login", "/register", "/invite")
# Панель администратора (этап 6): страницы /admin[/companies|/events], а /admin/businesses,
# /admin/logs, /admin/metrics — JSON API, ошибки которых остаются JSON.
_ADMIN_PAGE_PREFIXES = ("/admin/companies", "/admin/events")


def _is_html_page(path: str) -> bool:
    return (
        path.startswith(_CABINET_PREFIXES)
        or path == "/admin"
        or path.startswith(_ADMIN_PAGE_PREFIXES)
    )


_CABINET_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Раздел 16: заголовки безопасности для всех ответов, CSP и запрет кэширования
    для страниц кабинета (в них данные клиентов)."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if settings.force_https:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    path = request.url.path
    if _is_html_page(path):
        response.headers["Content-Security-Policy"] = _CABINET_CSP
    if _is_html_page(path) or path.startswith("/admin"):
        response.headers["Cache-Control"] = "no-store"  # данные всех компаний не кэшируются
    return response


@app.exception_handler(cabinet_routes.LoginRequired)
async def login_required_handler(request: Request, exc: cabinet_routes.LoginRequired):
    """Страница кабинета без входа: на форму входа, с возвратом после авторизации."""
    return cabinet_routes.login_redirect(exc)


_ERROR_TEXT = {
    401: ("Нужно войти", "Войдите в кабинет, чтобы продолжить."),
    403: ("Недостаточно прав", "Этот раздел вам недоступен."),
    404: ("Страница не найдена", "Такой страницы нет, или у вас нет к ней доступа."),
}


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    """Ошибки страниц кабинета показываются как HTML, ошибки API — как JSON."""
    if _is_html_page(request.url.path):
        title, text = _ERROR_TEXT.get(
            exc.status_code, ("Что-то пошло не так", "Попробуйте ещё раз чуть позже.")
        )
        return templates.TemplateResponse(
            request,
            "error.html",
            {"title": title, "text": text, "code": exc.status_code},
            status_code=exc.status_code,
        )
    return await http_exception_handler(request, exc)


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
