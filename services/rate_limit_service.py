"""
Ограничение частоты запросов — раздел 16 ТЗ («предусмотреть ограничение
частоты запросов»).

Этап 1: простой счётчик с фиксированным окном в памяти процесса, включён для
/auth/login и /auth/register (защита от подбора пароля и массовой регистрации).
Внешних зависимостей не требует.

Ограничение реализации: счётчик локален для процесса. При нескольких воркерах
или репликах лимит станет общим только после переноса счётчика в Redis либо
включения лимитов на уровне reverse proxy — это относится к этапу 9
(«Масштабирование»), в ТЗ отдельного требования к распределённому лимиту нет.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

from fastapi import HTTPException, Request, status

from config import settings

# ключ -> (начало окна, количество запросов)
_counters: dict[str, tuple[float, int]] = defaultdict(lambda: (0.0, 0))
_lock = threading.Lock()


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


def hit(scope: str, identity: str, *, limit: int, window_seconds: int) -> bool:
    """Зарегистрировать попытку. False — лимит исчерпан."""
    key = f"{scope}:{identity}"
    now = time.monotonic()
    with _lock:
        window_start, count = _counters[key]
        if now - window_start >= window_seconds:
            _counters[key] = (now, 1)
            return True
        if count >= limit:
            return False
        _counters[key] = (window_start, count + 1)
        return True


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
    with _lock:
        _counters.clear()


def enforce_webhook_rejection_limit(request: Request) -> None:
    """Лимит на ОТКЛОНЁННЫЕ webhook-запросы с одного IP (подбор secret_token).

    Успешные запросы Telegram не ограничиваются: он легитимно присылает
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
