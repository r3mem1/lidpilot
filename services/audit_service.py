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
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from database import SessionLocal
from models import LogLevel, SystemLog, utcnow

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
    # Решение 2026-09-27: ответ на каждое сообщение клиента
    MESSAGE_MERGED: Final = "MESSAGE_MERGED"  # серия сообщений — один ответ на последнее
    AUTO_REPLY_TEMPLATE: Final = "AUTO_REPLY_TEMPLATE"  # шаблонный ответ без AI
    REPLY_WATCHDOG: Final = "REPLY_WATCHDOG"  # сообщение осталось без ответа — шаблон
    # Вне ТЗ (§22): заявки на запись без брони (решение 2026-09-28)
    BOOKING_REQUEST_SAVED: Final = "BOOKING_REQUEST_SAVED"
    BOOKING_REQUEST_DONE: Final = "BOOKING_REQUEST_DONE"
    BOOKING_REQUEST_CLOSED: Final = "BOOKING_REQUEST_CLOSED"
    # Этап 9: клиент запретил/разрешил сообщения от сообщества (VK)
    CUSTOMER_CHANNEL_BLOCKED: Final = "CUSTOMER_CHANNEL_BLOCKED"
    CUSTOMER_CHANNEL_UNBLOCKED: Final = "CUSTOMER_CHANNEL_UNBLOCKED"

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

    # Мастера, смены и записи (вне ТЗ, §22 «автоматическая запись»)
    MASTER_CREATED: Final = "MASTER_CREATED"
    MASTER_UPDATED: Final = "MASTER_UPDATED"
    MASTER_DELETED: Final = "MASTER_DELETED"
    SHIFT_CREATED: Final = "SHIFT_CREATED"
    SHIFT_UPDATED: Final = "SHIFT_UPDATED"
    SHIFT_DELETED: Final = "SHIFT_DELETED"
    BOOKING_HELD: Final = "BOOKING_HELD"  # бронь от AI, ждёт подтверждения
    BOOKING_CREATED: Final = "BOOKING_CREATED"  # запись создал сотрудник
    BOOKING_CONFIRMED: Final = "BOOKING_CONFIRMED"
    BOOKING_REJECTED: Final = "BOOKING_REJECTED"
    BOOKING_CANCELLED: Final = "BOOKING_CANCELLED"
    BOOKING_RESCHEDULED: Final = "BOOKING_RESCHEDULED"  # сотрудник перенёс запись
    MASTER_NOTIFY_LINKED: Final = "MASTER_NOTIFY_LINKED"
    MASTER_NOTIFY_UNLINKED: Final = "MASTER_NOTIFY_UNLINKED"
    MASTER_NOTIFY_SENT: Final = "MASTER_NOTIFY_SENT"
    MASTER_NOTIFY_FAILED: Final = "MASTER_NOTIFY_FAILED"

    # Платформа
    ADMIN_BOOTSTRAPPED: Final = "ADMIN_BOOTSTRAPPED"
    UNHANDLED_ERROR: Final = "UNHANDLED_ERROR"
    SYSTEM_LOGS_PURGED: Final = "SYSTEM_LOGS_PURGED"


# Срок хранения system_logs (этап 7, раздел 17): критические действия ADMIN
# (§16 «аудит критических действий») не удаляются никогда — их единицы, а при
# разборе инцидента важна вся история. Остальное удаляется по сроку.
RETAINED_EVENT_TYPES: Final = frozenset(
    {
        EventType.ADMIN_BUSINESS_STATUS_CHANGED,
        EventType.ADMIN_SUBSCRIPTION_CHANGED,
        EventType.ADMIN_BOOTSTRAPPED,
        EventType.SYSTEM_LOGS_PURGED,
    }
)
# Удаление пачками с коммитом после каждой: короткие транзакции не блокируют
# запись новых событий на PostgreSQL и SQLite.
PURGE_BATCH_SIZE: Final = 5000


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


def purge_old_logs(
    retention_days: int,
    *,
    now: datetime | None = None,
    batch_size: int = PURGE_BATCH_SIZE,
) -> int:
    """Удалить события старше retention_days (кроме RETAINED_EVENT_TYPES).

    Вызывается фоновым циклом приложения раз в сутки; повторный или параллельный
    запуск безопасен. retention_days <= 0 — хранить бессрочно. Возвращает число
    удалённых записей; если что-то удалено, пишет событие SYSTEM_LOGS_PURGED.
    """
    if retention_days <= 0:
        return 0
    cutoff = (now or utcnow()) - timedelta(days=retention_days)
    conditions = (
        SystemLog.created_at < cutoff,
        SystemLog.event_type.not_in(RETAINED_EVENT_TYPES),
    )
    deleted = 0
    with SessionLocal() as db:
        while True:
            ids = list(
                db.scalars(
                    select(SystemLog.id).where(*conditions).order_by(SystemLog.id).limit(batch_size)
                )
            )
            if not ids:
                break
            db.execute(delete(SystemLog).where(SystemLog.id.in_(ids)))
            db.commit()
            deleted += len(ids)
        if deleted:
            log_event(
                db,
                event_type=EventType.SYSTEM_LOGS_PURGED,
                message=f"Удалено событий старше {retention_days} дн.: {deleted}",
                payload={
                    "deleted": deleted,
                    "retention_days": retention_days,
                    "cutoff": cutoff.isoformat(),
                },
                commit=True,
            )
    return deleted
