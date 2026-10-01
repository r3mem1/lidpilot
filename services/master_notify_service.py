"""
Уведомления мастеру о записях через бота/сообщество компании — вне ТЗ (§22),
по решению заказчика.

Привязка (мастер выбирает канал в кабинете):
1. create_link_code — одноразовый код LPxxxxxxxx на 30 минут; в БД только SHA-256;
2. мастер отправляет код боту компании (Telegram: ссылка t.me/<бот>?start=LP…) или
   сообществу VK; webhook передаёт сообщение в handle_incoming ДО создания клиента —
   чат мастера не становится «клиентом», AI ему не отвечает;
3. дальше мастер получает сообщения о бронях, записях, подтверждениях и отменах.

Доставка (раздел 18): уведомление сначала сохраняется в master_notifications
(та же транзакция, что и событие записи), отправляется после коммита; сбой —
повтор в фоновом цикле (retry_pending), после MESSAGE_MAX_ATTEMPTS — FAILED.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai.booking import format_when
from config import settings
from database import SessionLocal
from integrations.base import ChannelError, IncomingMessage
from models import (
    Booking,
    BookingSource,
    Business,
    Channel,
    Integration,
    LogLevel,
    Master,
    MasterNotification,
    NotificationStatus,
    Service,
    utcnow,
)
from services import (
    audit_service,
    booking_service,
    integration_service,
    master_service,
    schedule_service,
    secret_store,
)
from services.access_service import BusinessContext

logger = logging.getLogger("leadpilot.master_notify")

CODE_TTL = timedelta(minutes=30)
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без 0/O и 1/I
_LINK_RE = re.compile(r"^\s*(?:/start\s+)?(LP[A-Z0-9]{8})\s*$", re.IGNORECASE)


def _hash(code: str) -> str:
    return hashlib.sha256(code.upper().encode("utf-8")).hexdigest()


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


# --------------------------------------------------------------------------- #
# Привязка
# --------------------------------------------------------------------------- #
def create_link_code(db: Session, ctx: BusinessContext, master: Master, channel: Channel) -> dict:
    """Код привязки для самого мастера (или владельца — для мастера без аккаунта)."""
    is_self = master.user_id is not None and master.user_id == ctx.user.id
    if not (is_self or master_service.can_manage_masters(ctx)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав для этого действия"
        )
    integration = integration_service.get_active_integration(db, ctx.business_id, channel)
    if integration is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="У компании не подключён этот канал — выберите другой или попросите владельца подключить",
        )
    code = "LP" + "".join(secrets.choice(_ALPHABET) for _ in range(8))
    master.notify_code_hash = _hash(code)
    master.notify_code_expires_at = utcnow() + CODE_TTL
    master.notify_channel = channel
    db.commit()
    if channel is Channel.TELEGRAM:
        username = integration.external_account_name
        link = f"https://t.me/{username}?start={code}" if username else None
        instruction = "Откройте ссылку и нажмите «Старт» — или отправьте боту сообщение с кодом."
    else:
        link = f"https://vk.me/club{integration.external_account_id}"
        instruction = "Откройте сообщения сообщества и отправьте код одним сообщением."
    return {
        "code": code,
        "link": link,
        "channel": channel,
        "instruction": instruction,
        "expires_at": master.notify_code_expires_at,
    }


def unlink(db: Session, ctx: BusinessContext, master: Master) -> None:
    master.notify_chat_id = None
    master.notify_channel = None
    master.notify_code_hash = None
    master.notify_code_expires_at = None
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.MASTER_NOTIFY_UNLINKED,
        message=f"Уведомления мастера «{master.display_name}» отключены",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={"master_id": master.id},
    )
    db.commit()


def handle_incoming(db: Session, integration: Integration, incoming: IncomingMessage) -> bool:
    """Сообщение в бот/сообщество компании от мастера. True — сообщение обработано
    здесь и в диалоги клиентов не попадает."""
    match = _LINK_RE.match(incoming.text or "")
    if match:
        _link(db, integration, incoming, match.group(1).upper())
        return True
    # Чат уже привязанного мастера — служебный: AI ему не отвечает, клиентом он не становится.
    linked = db.scalar(
        select(Master.id).where(
            Master.business_id == integration.business_id,
            Master.notify_channel == integration.channel,
            Master.notify_chat_id == incoming.external_chat_id,
        )
    )
    return linked is not None


def _link(db: Session, integration: Integration, incoming: IncomingMessage, code: str) -> None:
    master = db.scalar(
        select(Master).where(
            Master.business_id == integration.business_id,
            Master.notify_code_hash == _hash(code),
        )
    )
    expires = _aware(master.notify_code_expires_at) if master else None
    already = db.scalar(
        select(Master.id).where(
            Master.business_id == integration.business_id,
            Master.notify_channel == integration.channel,
            Master.notify_chat_id == incoming.external_chat_id,
        )
    )
    if master is None and already is not None:
        return  # повторная доставка того же кода — чат уже привязан
    if (
        master is None
        or master.notify_channel != integration.channel
        or expires is None
        or expires < utcnow()
    ):
        audit_service.log_event(
            db,
            event_type=audit_service.EventType.WEBHOOK_IGNORED,
            message="Код привязки уведомлений не найден или устарел",
            level=LogLevel.WARNING,
            business_id=integration.business_id,
            payload={"channel": integration.channel.value},
            commit=True,
        )
        _send_direct(
            integration,
            incoming.external_chat_id,
            "Код не найден или устарел. Получите новый в кабинете LeadPilot.",
        )
        return
    master.notify_chat_id = incoming.external_chat_id
    master.notify_code_hash = None
    master.notify_code_expires_at = None
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.MASTER_NOTIFY_LINKED,
        message=f"Мастер «{master.display_name}» подключил уведомления ({integration.channel.value})",
        business_id=integration.business_id,
        payload={"master_id": master.id, "channel": integration.channel.value},
    )
    notification_id = _enqueue(
        db, master, "Готово! Сюда будут приходить уведомления о записях к вам."
    )
    db.commit()
    if notification_id:
        deliver(db, notification_id)


def _send_direct(integration: Integration, chat_id: str, text: str) -> None:
    try:
        integration_service.get_channel_client(integration).send_message(chat_id, text)
    except (ChannelError, secret_store.SecretStoreError):
        logger.warning("Не удалось ответить на код привязки (канал %s)", integration.channel.value)


# --------------------------------------------------------------------------- #
# Outbox
# --------------------------------------------------------------------------- #
def _enqueue(db: Session, master: Master, text: str) -> int | None:
    if not master.notify_chat_id or master.notify_channel is None:
        return None
    notification = MasterNotification(
        business_id=master.business_id,
        master_id=master.id,
        channel=master.notify_channel,
        chat_id=master.notify_chat_id,
        text=text,
    )
    db.add(notification)
    db.flush()
    return notification.id


def deliver(db: Session, notification_id: int) -> NotificationStatus | None:
    notification = db.get(MasterNotification, notification_id)
    if notification is None or notification.status is not NotificationStatus.PENDING:
        return notification.status if notification else None
    integration = integration_service.get_active_integration(
        db, notification.business_id, notification.channel
    )
    notification.attempts += 1
    try:
        if integration is None:
            raise ChannelError("Канал компании отключён")
        integration_service.get_channel_client(integration).send_message(
            notification.chat_id, notification.text
        )
    except (ChannelError, secret_store.SecretStoreError) as exc:
        notification.last_error = str(exc)[:500]
        final = integration is None or notification.attempts >= settings.message_max_attempts
        if final:
            notification.status = NotificationStatus.FAILED
        audit_service.log_event(
            db,
            event_type=audit_service.EventType.MASTER_NOTIFY_FAILED,
            message="Уведомление мастеру не отправлено"
            + (" (попытки исчерпаны)" if final else ", повторим"),
            level=LogLevel.WARNING,
            business_id=notification.business_id,
            payload={
                "notification_id": notification.id,
                "master_id": notification.master_id,
                "error": str(exc)[:300],
            },
        )
        db.commit()
        return notification.status
    notification.status = NotificationStatus.SENT
    notification.sent_at = utcnow()
    notification.last_error = None
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.MASTER_NOTIFY_SENT,
        message="Уведомление мастеру отправлено",
        business_id=notification.business_id,
        payload={"notification_id": notification.id, "master_id": notification.master_id},
    )
    db.commit()
    return notification.status


def retry_pending(limit: int = 20) -> int:
    """Повтор неотправленных уведомлений (вызывается фоновым циклом)."""
    border = utcnow() - timedelta(seconds=30)
    with SessionLocal() as db:
        ids = list(
            db.scalars(
                select(MasterNotification.id)
                .where(
                    MasterNotification.status == NotificationStatus.PENDING,
                    MasterNotification.created_at < border,
                )
                .order_by(MasterNotification.id)
                .limit(limit)
            )
        )
        for notification_id in ids:
            deliver(db, notification_id)
        return len(ids)


# --------------------------------------------------------------------------- #
# События записей → уведомления
# --------------------------------------------------------------------------- #
_TITLES = {
    "held": "Новая бронь от ассистента — подтвердите в кабинете",
    "created": "Новая запись",
    "confirmed": "Запись подтверждена",
    "rejected": "Бронь отклонена",
    "cancelled": "Запись отменена",
}


def _booking_notice(db: Session, booking: Booking, event: str) -> Callable[[], None] | None:
    # Решение заказчика 2026-09-29: при автозаписи ассистентом подтверждение ничего
    # не повторяет — клиент уже получил «Готово, вы записаны», мастер — «Новая бронь».
    if event == "confirmed" and booking.source is BookingSource.AI:
        return None
    title = _TITLES.get(event)
    master = db.get(Master, booking.master_id)
    if title is None or master is None:
        return None
    business = db.get_one(Business, booking.business_id)
    tz = schedule_service.business_tz(business)
    local = _aware(booking.starts_at).astimezone(tz)  # type: ignore[union-attr]
    service = db.get(Service, booking.service_id) if booking.service_id else None
    text = (
        f"{title}: «{service.name if service else 'услуга'}», {booking.client_name}, "
        f"{format_when(local, datetime.now(tz).date())}."
    )
    notification_id = _enqueue(db, master, text)
    if notification_id is None:
        return None

    def send() -> None:
        deliver(db, notification_id)

    return send


booking_service.on_booking_event.append(_booking_notice)
