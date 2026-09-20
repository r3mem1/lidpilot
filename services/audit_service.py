"""
Аудит и системные логи — разделы 16 и 17 ТЗ.

Логи пишутся в таблицу system_logs и должны отвечать на вопрос «что произошло
и почему система поступила так». На этапе 1 фиксируются события авторизации,
изменения настроек компании/услуг и отказы в доступе. События webhook, AI и
отправки сообщений добавят этапы 2–3 (набор типов расширяется в EventType).

Актор (кто выполнил действие) хранится в metadata: раздел 10 ТЗ не
предусматривает в system_logs отдельной колонки user_id.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from sqlalchemy.orm import Session

from models import LogLevel, SystemLog

logger = logging.getLogger("leadpilot.audit")


class EventType:
    """Типы событий (раздел 17). Строки, а не Enum: список расширяется
    на каждом этапе, а миграция БД для нового значения не нужна."""

    # Аутентификация
    AUTH_REGISTER: Final = "AUTH_REGISTER"
    AUTH_LOGIN_SUCCESS: Final = "AUTH_LOGIN_SUCCESS"
    AUTH_LOGIN_FAILED: Final = "AUTH_LOGIN_FAILED"
    AUTH_LOGOUT: Final = "AUTH_LOGOUT"
    AUTH_RATE_LIMITED: Final = "AUTH_RATE_LIMITED"

    # Компания и услуги (изменения настроек)
    BUSINESS_CREATED: Final = "BUSINESS_CREATED"
    BUSINESS_UPDATED: Final = "BUSINESS_UPDATED"
    BUSINESS_MEMBER_ADDED: Final = "BUSINESS_MEMBER_ADDED"
    SERVICE_CREATED: Final = "SERVICE_CREATED"
    SERVICE_UPDATED: Final = "SERVICE_UPDATED"
    SERVICE_DELETED: Final = "SERVICE_DELETED"

    # AI-модуль (этап 2, раздел 17: время ответа AI и ошибки AI)
    AI_RESPONSE_READY: Final = "AI_RESPONSE_READY"
    AI_ESCALATED: Final = "AI_ESCALATED"
    AI_VALIDATION_FAILED: Final = "AI_VALIDATION_FAILED"
    AI_ERROR: Final = "AI_ERROR"

    # Каналы и обработка сообщений (этап 3, раздел 17)
    INTEGRATION_CONNECTED: Final = "INTEGRATION_CONNECTED"
    INTEGRATION_DISCONNECTED: Final = "INTEGRATION_DISCONNECTED"
    INTEGRATION_ERROR: Final = "INTEGRATION_ERROR"
    WEBHOOK_RECEIVED: Final = "WEBHOOK_RECEIVED"
    WEBHOOK_REJECTED: Final = "WEBHOOK_REJECTED"
    WEBHOOK_IGNORED: Final = "WEBHOOK_IGNORED"
    MESSAGE_DUPLICATE: Final = "MESSAGE_DUPLICATE"
    MESSAGE_SKIPPED: Final = "MESSAGE_SKIPPED"
    MESSAGE_PROCESSING_FAILED: Final = "MESSAGE_PROCESSING_FAILED"
    MESSAGE_PROCESSED: Final = "MESSAGE_PROCESSED"
    MESSAGE_SENT: Final = "MESSAGE_SENT"
    MESSAGE_SEND_FAILED: Final = "MESSAGE_SEND_FAILED"

    # CRM-ядро (этап 4, разделы 14, 17)
    LEAD_CREATED: Final = "LEAD_CREATED"
    LEAD_UPDATED: Final = "LEAD_UPDATED"
    MANAGER_REPLY: Final = "MANAGER_REPLY"
    CONVERSATION_RESOLVED: Final = "CONVERSATION_RESOLVED"

    # Кабинет (этап 5, раздел 13): сотрудники и приглашения
    BUSINESS_MEMBER_UPDATED: Final = "BUSINESS_MEMBER_UPDATED"
    BUSINESS_MEMBER_REMOVED: Final = "BUSINESS_MEMBER_REMOVED"
    INVITATION_CREATED: Final = "INVITATION_CREATED"
    INVITATION_REVOKED: Final = "INVITATION_REVOKED"
    INVITATION_ACCEPTED: Final = "INVITATION_ACCEPTED"

    # Безопасность
    ACCESS_DENIED: Final = "ACCESS_DENIED"

    # Административная панель (этап 6, раздел 15): критические действия ADMIN (§16–17)
    ADMIN_BUSINESS_STATUS_CHANGED: Final = "ADMIN_BUSINESS_STATUS_CHANGED"
    ADMIN_SUBSCRIPTION_CHANGED: Final = "ADMIN_SUBSCRIPTION_CHANGED"

    # Платформа
    ADMIN_BOOTSTRAPPED: Final = "ADMIN_BOOTSTRAPPED"
    UNHANDLED_ERROR: Final = "UNHANDLED_ERROR"


def log_event(
    db: Session,
    *,
    event_type: str,
    message: str,
    level: LogLevel = LogLevel.INFO,
    business_id: int | None = None,
    actor_user_id: int | None = None,
    payload: dict[str, Any] | None = None,
    commit: bool = False,
) -> SystemLog:
    """Записать событие в system_logs.

    commit=True нужен там, где транзакция запроса завершится исключением
    (например, отказ в доступе): иначе запись о событии откатится вместе
    с запросом.
    """
    meta: dict[str, Any] = dict(payload or {})
    if actor_user_id is not None:
        meta["actor_user_id"] = actor_user_id

    entry = SystemLog(
        business_id=business_id,
        level=level,
        event_type=event_type,
        message=message,
        payload=meta or None,
    )
    db.add(entry)
    if commit:
        db.commit()
    else:
        db.flush()

    logger.log(
        getattr(logging, level.value, logging.INFO),
        "%s | business_id=%s | %s | %s",
        event_type,
        business_id,
        message,
        meta or {},
    )
    return entry
