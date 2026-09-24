"""
Внешний трекер ошибок Sentry (этап 7, разделы 16–17 ТЗ).

Дополняет system_logs, а не заменяет их: аудит «что случилось с сообщением»
остаётся в БД (audit_service), Sentry лишь оповещает о необработанных
исключениях и ERROR-логах, чтобы сбой на пилоте не остался незамеченным.

Выключен, пока SENTRY_DSN пуст (локальная разработка и тесты).

В Sentry не уходят персональные данные клиентов и секреты (раздел 16):
- тела запросов (текст сообщений клиентов в webhook), заголовки
  (cookie сессии, X-Telegram-Bot-Api-Secret-Token) и query-строки;
- значения локальных переменных в стектрейсах;
- токены ботов из URL Bot API (httpx-breadcrumbs, спаны, тексты исключений),
  Bearer-токены и адреса email — вычищаются из всего события.
"""

from __future__ import annotations

import re
from typing import Any

from config import settings

# Токен бота в URL Bot API: https://api.telegram.org/bot<id>:<secret>/sendMessage
_BOT_TOKEN_RE = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_REDACTED = "[скрыто]"
# Из данных запроса оставляем только то, что нужно для поиска маршрута.
_REQUEST_KEEP = ("method", "url")


def scrub_text(value: str) -> str:
    """Убрать из строки токены ботов, Bearer-токены и email."""
    value = _BOT_TOKEN_RE.sub(_REDACTED, value)
    value = _BEARER_RE.sub(f"Bearer {_REDACTED}", value)
    return _EMAIL_RE.sub(_REDACTED, value)


def _scrub(obj: Any) -> Any:
    if isinstance(obj, str):
        return scrub_text(obj)
    if isinstance(obj, dict):
        return {key: _scrub(val) for key, val in obj.items()}
    if isinstance(obj, list | tuple):
        return [_scrub(item) for item in obj]
    return obj


def before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
    """Фильтр события перед отправкой (ошибки и транзакции)."""
    request = event.get("request")
    if isinstance(request, dict):
        event["request"] = {key: request[key] for key in _REQUEST_KEEP if key in request}
    return _scrub(event)


def before_breadcrumb(crumb: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
    """httpx-breadcrumb содержит URL Bot API с токеном — вычищаем."""
    return _scrub(crumb)


def init_monitoring() -> bool:
    """Подключить Sentry, если задан SENTRY_DSN. Возвращает, включён ли трекер."""
    if not settings.sentry_dsn:
        return False

    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        release=f"leadpilot@{settings.app_version}",
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        traces_sample_rate=settings.sentry_traces_sample_rate,
        before_send=before_send,  # pyright: ignore[reportArgumentType]
        before_send_transaction=before_send,  # pyright: ignore[reportArgumentType]
        before_breadcrumb=before_breadcrumb,  # pyright: ignore[reportArgumentType]
    )
    return True
