"""
Карточка клиента — вне ТЗ (§22), решение заказчика 2026-10-01: имя и телефон
(клиент оставляет их боту при записи или их вписывает администратор), заметка
администратора и записи клиента к мастерам.

Доступ — только через BusinessContext; клиент чужой компании — 404 (раздел 16).
Менять карточку может владелец или менеджер; мастер её не правит.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai.contacts import normalize_phone
from models import Booking, BookingStatus, Customer, utcnow
from services import audit_service, master_service
from services.access_service import BusinessContext

_UNSET = object()


def display_name(customer: Customer) -> str:
    """Имя для людей: как клиент представился, иначе имя или @ник из канала."""
    if customer.contact_name:
        return customer.contact_name
    if customer.name:
        return customer.name
    return f"@{customer.username}" if customer.username else "Клиент"


def update_customer(
    db: Session,
    ctx: BusinessContext,
    customer: Customer,
    *,
    contact_name: object = _UNSET,
    phone: object = _UNSET,
    notes: object = _UNSET,
) -> Customer:
    """Правка карточки сотрудником. Пустая строка очищает поле; телефон
    приводится к +7…; непохожий на номер — 422."""
    if not master_service.is_staff(ctx):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав для этого действия"
        )
    if customer.business_id != ctx.business_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Клиент не найден")
    changed: list[str] = []
    if contact_name is not _UNSET:
        value = (str(contact_name or "")).strip()[:255] or None
        if value != customer.contact_name:
            customer.contact_name = value
            changed.append("contact_name")
            for booking in db.scalars(
                select(Booking).where(
                    Booking.customer_id == customer.id,
                    Booking.status.in_((BookingStatus.PENDING, BookingStatus.CONFIRMED)),
                )
            ):
                booking.client_name = value or display_name(customer)
    if phone is not _UNSET:
        raw = str(phone or "").strip()
        value = normalize_phone(raw) if raw else None
        if raw and value is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Номер телефона в формате +7 900 123-45-67",
            )
        if value != customer.phone:
            customer.phone = value
            changed.append("phone")
    if notes is not _UNSET:
        value = (str(notes or "")).strip()[:2000] or None
        if value != customer.notes:
            customer.notes = value
            changed.append("notes")
    if changed:
        audit_service.log_event(
            db,
            event_type=audit_service.EventType.CUSTOMER_UPDATED,
            message="Карточка клиента изменена",
            business_id=ctx.business_id,
            actor_user_id=ctx.user.id,
            payload={"customer_id": customer.id, "fields": changed},
        )
    db.commit()
    db.refresh(customer)
    return customer


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def customer_bookings(
    db: Session, ctx: BusinessContext, customer: Customer, *, limit: int = 30
) -> tuple[list[Booking], list[Booking], int]:
    """(предстоящие активные записи — ближайшие первыми, прошедшие и отменённые —
    новые первыми, число визитов: подтверждённые записи в прошлом)."""
    rows = list(
        db.scalars(
            select(Booking)
            .where(Booking.business_id == ctx.business_id, Booking.customer_id == customer.id)
            .order_by(Booking.starts_at.desc())
            .limit(limit)
        )
    )
    now = utcnow()
    active = (BookingStatus.PENDING, BookingStatus.CONFIRMED)
    upcoming = sorted(
        (b for b in rows if b.status in active and _aware(b.starts_at) >= now),
        key=lambda b: _aware(b.starts_at),
    )
    past = [b for b in rows if b not in upcoming]
    visits = sum(
        1 for b in rows if b.status is BookingStatus.CONFIRMED and _aware(b.starts_at) < now
    )
    return upcoming, past, visits
