"""
Ограничение частоты запросов — раздел 16 ТЗ («предусмотреть ограничение
частоты запросов»): /auth/login, /auth/register, отклонённые webhook-запросы,
проверка ответов AI.

Этап 9: счётчики с фиксированным окном хранятся в общей БД (таблица
rate_limit_counters), поэтому лимит един для всех воркеров и реплик.
Одна попытка — один атомарный UPSERT (INSERT … ON CONFLICT DO UPDATE …
RETURNING) в PostgreSQL и SQLite. Если БД недоступна, лимит пропускает
запрос (fail-open) и пишет ошибку в лог: сбой счётчика не должен закрывать
вход в кабинет. Старые окна удаляет фоновая очистка (purge_expired).
"""

from __future__ import annotations

import logging
import time

from fastapi import HTTPException, Request, status
from sqlalchemy import case, delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from config import settings
from database import SessionLocal
from models import RateLimitCounter

logger = logging.getLogger("leadpilot.rate_limit")

_MAX_KEY = 200
# Окна старше суток точно закрыты: самое длинное окно в настройках — минуты.
_EXPIRED_AFTER_SECONDS = 86400


def client_ip(request: Request) -> str:
    """IP клиента.

    X-Forwarded-For клиент может подделать: первый адрес в списке — это то, что
    он сам написал, поэтому по умолчанию заголовок игнорируется (лимит по нему
    обходился бы одним заголовком). Каждый доверенный прокси дописывает справа
    адрес, от которого получил запрос; при TRUSTED_PROXY_COUNT=N берётся
    N-й адрес справа — первый, добавленный нашей инфраструктурой."""
    hops = settings.trusted_proxy_count
    forwarded = request.headers.get("X-Forwarded-For")
    if hops > 0 and forwarded:
        addresses = [part.strip() for part in forwarded.split(",") if part.strip()]
        if addresses:
            return addresses[-min(hops, len(addresses))]
    return request.client.host if request.client else "unknown"


def _increment(db: Session, key: str, window_start: int) -> int:
    """Атомарно увеличить счётчик окна; новое окно начинает счёт заново."""
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        insert = pg_insert
    elif dialect == "sqlite":
        insert = sqlite_insert
    else:  # pragma: no cover - другие СУБД проект не поддерживает
        raise SQLAlchemyError(f"rate limit: СУБД {dialect} не поддерживается")
    stmt = insert(RateLimitCounter).values(key=key, window_start=window_start, count=1)
    stmt = stmt.on_conflict_do_update(
        index_elements=[RateLimitCounter.key],
        set_={
            "count": case(
                (
                    RateLimitCounter.window_start == stmt.excluded.window_start,
                    RateLimitCounter.count + 1,
                ),
                else_=1,
            ),
            "window_start": stmt.excluded.window_start,
        },
    ).returning(RateLimitCounter.count)
    return int(db.execute(stmt).scalar_one())


def hit(scope: str, identity: str, *, limit: int, window_seconds: int) -> bool:
    """Зарегистрировать попытку. False — лимит окна исчерпан."""
    key = f"{scope}:{identity}"[:_MAX_KEY]
    now = int(time.time())
    window_start = now - now % max(window_seconds, 1)
    try:
        with SessionLocal() as db:
            count = _increment(db, key, window_start)
            db.commit()
    except SQLAlchemyError:
        logger.exception("Счётчик rate limit недоступен: запрос %s пропущен без лимита", scope)
        return True
    return count <= limit


def purge_expired(now: float | None = None) -> int:
    """Удалить закрытые окна (вызывается фоновой очисткой раз в сутки)."""
    border = int(now if now is not None else time.time()) - _EXPIRED_AFTER_SECONDS
    with SessionLocal() as db:
        result = db.execute(delete(RateLimitCounter).where(RateLimitCounter.window_start < border))
        db.commit()
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


def enforce_auth_rate_limit(request: Request, scope: str) -> None:
    """Проверка лимита для эндпоинтов аутентификации. 429 при превышении."""
    if not settings.rate_limit_enabled:
        return
    allowed = hit(
        scope,
        client_ip(request),
        limit=settings.auth_rate_limit_attempts,
        window_seconds=settings.auth_rate_limit_window_seconds,
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Слишком много попыток, повторите позже",
            headers={"Retry-After": str(settings.auth_rate_limit_window_seconds)},
        )


def reset() -> None:
    """Сброс счётчиков (используется в тестах)."""
    with SessionLocal() as db:
        db.execute(delete(RateLimitCounter))
        db.commit()


def enforce_webhook_rejection_limit(request: Request) -> None:
    """Лимит на ОТКЛОНЁННЫЕ webhook-запросы с одного IP (подбор secret_token).

    Успешные запросы каналов не ограничиваются: Telegram и VK легитимно присылают
    пачки обновлений. 429 отдаётся до записи в system_logs.
    """
    if not settings.rate_limit_enabled:
        return
    allowed = hit(
        "webhook:rejected",
        client_ip(request),
        limit=settings.webhook_rate_limit_attempts,
        window_seconds=settings.webhook_rate_limit_window_seconds,
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too Many Requests",
            headers={"Retry-After": str(settings.webhook_rate_limit_window_seconds)},
        )
