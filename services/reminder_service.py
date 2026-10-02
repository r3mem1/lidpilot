"""
Напоминания клиенту о записи — вне ТЗ (§22), решение заказчика 2026-10-01:
за сутки («завтра в 12:00 вы записаны…») и за 2 часа («ждём вас сегодня…»).

* Только записи из диалога (клиент писал боту — есть куда напомнить), активные
  (ждёт подтверждения или подтверждена), у компании включены напоминания,
  подписка действует, компания не приостановлена, клиент не запретил сообщения.
* Свежая запись не напоминается: записался меньше чем за 12 ч — без «за сутки»,
  меньше чем за 3 ч — без «за 2 часа» (клиент и так помнит).
* Без дублей при нескольких воркерах: отметка ставится атомарным
  UPDATE … WHERE reminded_*_at IS NULL; сообщение сохраняется в той же
  транзакции и отправляется после коммита (сначала сохранить — раздел 18).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ai.booking import format_when
from models import (
    Booking,
    BookingStatus,
    Business,
    BusinessStatus,
    Conversation,
    Customer,
    DeliveryStatus,
    Master,
    Message,
    SenderType,
    Service,
    utcnow,
)
from services import audit_service, schedule_service, subscription_service
from services.message_service import deliver_outgoing

logger = logging.getLogger("leadpilot.reminders")

DAY_BEFORE = timedelta(hours=24)
DAY_LATEST = timedelta(hours=3)  # «за сутки» не позже чем за 3 ч до визита
DAY_MIN_NOTICE = timedelta(hours=12)  # запись сделана хотя бы за 12 ч
SOON_BEFORE = timedelta(hours=2)
SOON_MIN_NOTICE = timedelta(hours=3)

ACTIVE = (BookingStatus.PENDING, BookingStatus.CONFIRMED)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def reminder_text(db: Session, booking: Booking, kind: str, now: datetime) -> str:
    business = db.get_one(Business, booking.business_id)
    tz = schedule_service.business_tz(business)
    local = _aware(booking.starts_at).astimezone(tz)
    when = format_when(local, now.astimezone(tz).date())
    service = db.get(Service, booking.service_id) if booking.service_id else None
    master = db.get(Master, booking.master_id)
    what = f"«{service.name if service else 'услуга'}» у мастера {master.display_name if master else ''}"
    address = f" Адрес: {business.address}." if business.address else ""
    if kind == "day":
        return (
            f"Напоминаем: {when} вы записаны — {what.strip()}.{address} "
            "Если планы изменились — напишите сюда, перенесём."
        )
    return f"Ждём вас {when}: {what.strip()}.{address} До встречи!"


def _due(db: Session, now: datetime, kind: str) -> list[int]:
    if kind == "day":
        window = (Booking.starts_at > now + DAY_LATEST, Booking.starts_at <= now + DAY_BEFORE)
        flag = Booking.reminded_day_at
        notice = DAY_MIN_NOTICE
    else:
        window = (Booking.starts_at > now, Booking.starts_at <= now + SOON_BEFORE)
        flag = Booking.reminded_soon_at
        notice = SOON_MIN_NOTICE
    rows = db.execute(
        select(Booking.id, Booking.starts_at, Booking.created_at)
        .join(Business, Business.id == Booking.business_id)
        .join(Conversation, Conversation.id == Booking.conversation_id)
        .join(Customer, Customer.id == Conversation.customer_id)
        .where(
            *window,
            flag.is_(None),
            Booking.status.in_(ACTIVE),
            Business.reminders_enabled.is_(True),
            Business.status != BusinessStatus.SUSPENDED,
            Customer.channel_blocked.is_(False),
        )
        .order_by(Booking.starts_at)
        .limit(200)
    ).all()
    return [bid for bid, starts, created in rows if _aware(starts) - _aware(created) >= notice]


def send_due(db: Session, now: datetime | None = None) -> int:
    """Отправить напоминания, которым пришло время. Возвращает число отправленных."""
    now = now or utcnow()
    sent = 0
    for kind in ("day", "soon"):
        for booking_id in _due(db, now, kind):
            flag = Booking.reminded_day_at if kind == "day" else Booking.reminded_soon_at
            claimed = db.execute(
                update(Booking).where(Booking.id == booking_id, flag.is_(None)).values({flag: now})
            )
            if not claimed.rowcount:  # type: ignore[attr-defined]
                db.rollback()
                continue  # другой воркер успел раньше
            booking = db.get_one(Booking, booking_id)
            if not subscription_service.ai_allowed(db, booking.business_id, now):
                db.commit()  # отметка остаётся: после продления старое не догоняем
                continue
            if kind == "soon" and booking.reminded_day_at is None:
                booking.reminded_day_at = now  # «за сутки» уже не актуально
            conversation = db.get_one(Conversation, booking.conversation_id)
            message = Message(
                business_id=booking.business_id,
                conversation_id=conversation.id,
                sender_type=SenderType.AI,
                text=reminder_text(db, booking, kind, now),
                content_type="text",
                delivery_status=DeliveryStatus.PENDING,
            )
            db.add(message)
            db.flush()
            audit_service.log_event(
                db,
                event_type=audit_service.EventType.BOOKING_REMINDER_SENT,
                message=f"Напоминание клиенту о записи #{booking.id} ({kind})",
                business_id=booking.business_id,
                payload={"booking_id": booking.id, "kind": kind, "outgoing_message_id": message.id},
            )
            message_id = message.id
            db.commit()
            try:
                deliver_outgoing(db, message_id)
            except Exception:  # noqa: BLE001 - сбой канала не должен останавливать остальные
                logger.exception("Не удалось отправить напоминание #%s", booking_id)
            sent += 1
    return sent
