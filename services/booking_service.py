"""
Записи клиентов к мастерам — вне ТЗ (§22 «автоматическая запись»), по решению заказчика.

* free_slots — свободное время: смены мастеров минус активные записи (PENDING,
  CONFIRMED), с учётом длительности услуги, шага сетки компании, услуг мастера и
  «не раньше чем через час». Единственный источник времени, которое может
  назвать AI (инвариант «AI не выдумывает свободные слоты»).
* create_booking — атомарно: сначала блокируется строка мастера (UPDATE — в
  PostgreSQL блокировка строки, в SQLite — блокировка записи), затем под
  блокировкой проверяется пересечение. Две брони на один интервал невозможны.
* confirm / reject / cancel — решение человека по брони; клиент получает
  сообщение в своём канале, мастер — уведомление (master_notify_service).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from models import (
    Booking,
    BookingSource,
    BookingStatus,
    Business,
    Conversation,
    ConversationStatus,
    Master,
    MasterShift,
    MemberRole,
    Service,
    utcnow,
)
from services import audit_service, master_service, schedule_service
from services.access_service import BusinessContext

logger = logging.getLogger("leadpilot.booking")

ACTIVE = (BookingStatus.PENDING, BookingStatus.CONFIRMED)
DEFAULT_DURATION_MINUTES = 60  # у услуги без длительности
MIN_LEAD_MINUTES = 60  # не предлагать время раньше чем через час
HORIZON_DAYS = 14  # насколько вперёд ищет AI

# Хуки побочных эффектов (сообщение клиенту, уведомление мастеру): подключаются
# модулями каналов, чтобы booking_service от них не зависел. Хук вызывается ДО
# коммита (сохраняет исходящее сообщение в той же транзакции) и может вернуть
# действие, которое выполнится ПОСЛЕ коммита (отправка) — раздел 18.
BookingHook = Callable[[Session, Booking, str], Callable[[], None] | None]
on_booking_event: list[BookingHook] = []


class BookingConflict(Exception):
    """Интервал уже занят или вне смены мастера."""


@dataclass(frozen=True)
class Slot:
    master_id: int
    master_name: str
    starts_at: datetime  # UTC
    ends_at: datetime  # UTC
    local_start: datetime  # в зоне компании


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def duration_minutes(service: Service | None) -> int:
    return service.duration if service and service.duration else DEFAULT_DURATION_MINUTES


def _active_bookings(
    db: Session, master_ids: list[int], start: datetime, end: datetime
) -> dict[int, list[tuple[datetime, datetime]]]:
    busy: dict[int, list[tuple[datetime, datetime]]] = {mid: [] for mid in master_ids}
    if not master_ids:
        return busy
    rows = db.execute(
        select(Booking.master_id, Booking.starts_at, Booking.ends_at).where(
            Booking.master_id.in_(master_ids),
            Booking.status.in_(ACTIVE),
            Booking.starts_at < end,
            Booking.ends_at > start,
        )
    )
    for master_id, s, e in rows:
        busy[master_id].append((_aware(s), _aware(e)))
    return busy


def free_slots(
    db: Session,
    business: Business,
    service: Service,
    *,
    day_from: date,
    day_to: date,
    master_id: int | None = None,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[Slot]:
    """Свободные интервалы под услугу за период (включительно), по времени начала."""
    tz = schedule_service.business_tz(business)
    masters = master_service.masters_for_service(db, business.id, service.id)
    if master_id is not None:
        masters = [m for m in masters if m.id == master_id]
    if not masters:
        return []
    names = {m.id: m.display_name for m in masters}
    duration = timedelta(minutes=duration_minutes(service))
    step = timedelta(minutes=max(5, business.slot_step_minutes or 30))
    earliest = (now or utcnow()) + timedelta(minutes=MIN_LEAD_MINUTES)

    shifts = db.scalars(
        select(MasterShift).where(
            MasterShift.master_id.in_(list(names)),
            MasterShift.day >= day_from,
            MasterShift.day <= day_to,
        )
    ).all()
    if not shifts:
        return []
    range_start = datetime.combine(day_from, datetime.min.time(), tzinfo=tz).astimezone(UTC)
    range_end = datetime.combine(
        day_to + timedelta(days=1), datetime.min.time(), tzinfo=tz
    ).astimezone(UTC)
    busy = _active_bookings(db, list(names), range_start, range_end)

    slots: list[Slot] = []
    for shift in shifts:
        cursor = datetime.combine(shift.day, shift.start_time, tzinfo=tz)
        shift_end = datetime.combine(shift.day, shift.end_time, tzinfo=tz)
        while cursor + duration <= shift_end:
            start_utc = cursor.astimezone(UTC)
            end_utc = (cursor + duration).astimezone(UTC)
            if start_utc >= earliest and not any(
                s < end_utc and e > start_utc for s, e in busy[shift.master_id]
            ):
                slots.append(
                    Slot(shift.master_id, names[shift.master_id], start_utc, end_utc, cursor)
                )
            cursor += step
    slots.sort(key=lambda slot: (slot.starts_at, slot.master_name))
    return slots[:limit] if limit else slots


@dataclass(frozen=True)
class MasterWindows:
    master_id: int
    master_name: str
    windows: list[tuple[datetime, datetime]]  # свободные интервалы, время компании


def free_windows(
    db: Session,
    business: Business,
    day: date,
    *,
    service: Service | None = None,
    master_id: int | None = None,
    time_from: time | None = None,
    time_to: time | None = None,
    now: datetime | None = None,
) -> list[MasterWindows]:
    """Свободные интервалы мастеров за день: смены минус активные записи и время
    раньше «через час» (решение заказчика 2026-10-01: на «какие окна на завтра» —
    «Иван 10:00–16:00, Пётр 13:00–20:00»). Интервал короче услуги не показывается
    (услуга не названа — короче самой короткой услуги компании). time_from/time_to —
    часть дня («вечером»): интервалы обрезаются по этим границам."""
    tz = schedule_service.business_tz(business)
    if service is not None:
        masters = master_service.masters_for_service(db, business.id, service.id)
        min_len = timedelta(minutes=duration_minutes(service))
    else:
        masters = list(
            db.scalars(
                select(Master)
                .where(Master.business_id == business.id, Master.active.is_(True))
                .order_by(Master.display_name, Master.id)
            )
        )
        durations = db.scalars(
            select(Service.duration).where(
                Service.business_id == business.id, Service.active.is_(True)
            )
        ).all()
        min_len = timedelta(
            minutes=min((d for d in durations if d), default=DEFAULT_DURATION_MINUTES)
        )
    if master_id is not None:
        masters = [m for m in masters if m.id == master_id]
    if not masters:
        return []
    shifts = db.scalars(
        select(MasterShift)
        .where(MasterShift.master_id.in_([m.id for m in masters]), MasterShift.day == day)
        .order_by(MasterShift.start_time)
    ).all()
    if not shifts:
        return []
    day_start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    busy = _active_bookings(
        db,
        [m.id for m in masters],
        day_start.astimezone(UTC),
        (day_start + timedelta(days=1)).astimezone(UTC),
    )
    # «Не раньше чем через час», с округлением вверх до шага сетки компании.
    step = max(5, business.slot_step_minutes or 30)
    earliest = ((now or utcnow()) + timedelta(minutes=MIN_LEAD_MINUTES)).astimezone(tz)
    over = (earliest.hour * 60 + earliest.minute) % step
    if over or earliest.second or earliest.microsecond:
        earliest += timedelta(minutes=step - over)
    earliest = earliest.replace(second=0, microsecond=0)
    if time_from is not None:
        earliest = max(earliest, datetime.combine(day, time_from, tzinfo=tz))
    latest = datetime.combine(day, time_to, tzinfo=tz) if time_to is not None else None

    result: list[MasterWindows] = []
    for master in masters:
        windows: list[tuple[datetime, datetime]] = []
        taken = sorted((s.astimezone(tz), e.astimezone(tz)) for s, e in busy[master.id])
        for shift in (s for s in shifts if s.master_id == master.id):
            cursor = max(datetime.combine(day, shift.start_time, tzinfo=tz), earliest)
            shift_end = datetime.combine(day, shift.end_time, tzinfo=tz)
            if latest is not None:
                shift_end = min(shift_end, latest)
            for b_start, b_end in [*taken, (shift_end, shift_end)]:
                end = min(b_start, shift_end)
                if end - cursor >= min_len:
                    windows.append((cursor, end))
                cursor = max(cursor, b_end)
                if cursor >= shift_end:
                    break
        if windows:
            result.append(MasterWindows(master.id, master.display_name, windows))
    return result


def _inside_shift(
    db: Session, business: Business, master_id: int, start: datetime, end: datetime
) -> bool:
    tz = schedule_service.business_tz(business)
    local_start, local_end = start.astimezone(tz), end.astimezone(tz)
    if local_start.date() != local_end.date() and local_end.time() != datetime.min.time():
        return False
    shift = db.scalar(
        select(MasterShift).where(
            MasterShift.master_id == master_id,
            MasterShift.day == local_start.date(),
            MasterShift.start_time <= local_start.time(),
            MasterShift.end_time >= local_end.time(),
        )
    )
    return shift is not None


def create_booking(
    db: Session,
    business: Business,
    *,
    master: Master,
    service: Service,
    starts_at: datetime,
    client_name: str,
    source: BookingSource,
    customer_id: int | None = None,
    conversation_id: int | None = None,
    created_by_user_id: int | None = None,
    comment: str | None = None,
    now: datetime | None = None,
) -> Booking:
    """Создать запись (AI — бронь PENDING, сотрудник — сразу CONFIRMED).
    BookingConflict — время занято, вне смены или в прошлом. Коммитит транзакцию."""
    starts_at = _aware(starts_at).astimezone(UTC)
    ends_at = starts_at + timedelta(minutes=duration_minutes(service))
    if master.business_id != business.id or service.business_id != business.id:
        raise BookingConflict("Мастер или услуга не принадлежат компании")
    if not master.active:
        raise BookingConflict("Мастер не принимает записи")
    if service.id not in _allowed_service_ids(db, master, business.id):
        raise BookingConflict("Мастер не выполняет эту услугу")
    if starts_at < (now or utcnow()):
        raise BookingConflict("Время уже прошло")

    # Блокировка мастера до конца транзакции: параллельная бронь того же мастера
    # ждёт здесь и затем увидит нашу запись при проверке пересечения.
    db.execute(update(Master).where(Master.id == master.id).values(active=Master.active))
    if not _inside_shift(db, business, master.id, starts_at, ends_at):
        db.rollback()
        raise BookingConflict("Время вне смены мастера")
    clash = db.scalar(
        select(Booking.id).where(
            Booking.master_id == master.id,
            Booking.status.in_(ACTIVE),
            Booking.starts_at < ends_at,
            Booking.ends_at > starts_at,
        )
    )
    if clash is not None:
        db.rollback()
        raise BookingConflict("Это время уже занято")

    booking = Booking(
        business_id=business.id,
        master_id=master.id,
        service_id=service.id,
        customer_id=customer_id,
        conversation_id=conversation_id,
        client_name=client_name.strip()[:255] or "Клиент",
        starts_at=starts_at,
        ends_at=ends_at,
        status=BookingStatus.PENDING if source is BookingSource.AI else BookingStatus.CONFIRMED,
        source=source,
        comment=comment,
        created_by_user_id=created_by_user_id,
    )
    db.add(booking)
    db.flush()
    local = starts_at.astimezone(schedule_service.business_tz(business))
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.BOOKING_HELD
        if source is BookingSource.AI
        else audit_service.EventType.BOOKING_CREATED,
        message=(
            f"{'Бронь' if source is BookingSource.AI else 'Запись'}: «{service.name}» у "
            f"«{master.display_name}» {local:%d.%m %H:%M}"
        ),
        business_id=business.id,
        actor_user_id=created_by_user_id,
        payload={
            "booking_id": booking.id,
            "master_id": master.id,
            "service_id": service.id,
            "conversation_id": conversation_id,
            "starts_at": starts_at.isoformat(),
        },
    )
    after = _fire(db, booking, "held" if source is BookingSource.AI else "created")
    db.commit()
    _run_after_commit(after)
    db.refresh(booking)
    return booking


def _allowed_service_ids(db: Session, master: Master, business_id: int) -> set[int]:
    links = master_service.service_ids_by_master(db, [master.id])[master.id]
    if links:
        return set(links)
    return set(db.scalars(select(Service.id).where(Service.business_id == business_id)))


def _fire(db: Session, booking: Booking, event: str) -> list[Callable[[], None]]:
    after: list[Callable[[], None]] = []
    for hook in on_booking_event:
        action = hook(db, booking, event)
        if action is not None:
            after.append(action)
    return after


def _run_after_commit(actions: list[Callable[[], None]]) -> None:
    for action in actions:
        try:
            action()
        except Exception:  # noqa: BLE001 - отправка повторится фоновым циклом
            logger.exception("Побочное действие после записи не выполнено")


# --------------------------------------------------------------------------- #
# Чтение и решения сотрудников
# --------------------------------------------------------------------------- #
def list_bookings(
    db: Session,
    ctx: BusinessContext,
    *,
    day_from: date,
    day_to: date,
    master_id: int | None = None,
    statuses: tuple[BookingStatus, ...] | None = None,
) -> list[Booking]:
    tz = schedule_service.business_tz(ctx.business)
    start = datetime.combine(day_from, datetime.min.time(), tzinfo=tz).astimezone(UTC)
    end = datetime.combine(day_to + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(
        UTC
    )
    stmt = select(Booking).where(
        Booking.business_id == ctx.business_id, Booking.starts_at >= start, Booking.starts_at < end
    )
    if ctx.role is MemberRole.MASTER and not ctx.is_platform_admin:
        own = master_service.own_master(db, ctx)
        if own is None:
            return []
        stmt = stmt.where(Booking.master_id == own.id)
    elif master_id is not None:
        stmt = stmt.where(Booking.master_id == master_id)
    if statuses:
        stmt = stmt.where(Booking.status.in_(statuses))
    return list(db.scalars(stmt.order_by(Booking.starts_at, Booking.id)))


def assert_can_view(db: Session, ctx: BusinessContext, booking: Booking) -> None:
    if master_service.is_staff(ctx):
        return
    own = master_service.own_master(db, ctx)
    if own is None or own.id != booking.master_id:
        # Чужая запись для мастера неотличима от несуществующей.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Запись не найдена")


def _decide(
    db: Session,
    ctx: BusinessContext,
    booking: Booking,
    *,
    allowed_from: tuple[BookingStatus, ...],
    to: BookingStatus,
    event_type: str,
    verb: str,
    staff_only: bool = False,
) -> Booking:
    assert_can_view(db, ctx, booking)
    if staff_only and not master_service.is_staff(ctx):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав для этого действия"
        )
    if booking.status not in allowed_from:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Запись в статусе {booking.status.value} нельзя {verb}",
        )
    previous = booking.status
    booking.status = to
    booking.decided_by_user_id = ctx.user.id
    if booking.conversation_id is not None and to is not BookingStatus.CONFIRMED:
        conversation = db.get(Conversation, booking.conversation_id)
        if conversation is not None:
            conversation.status = ConversationStatus.NEEDS_ATTENTION
            conversation.attention_reason = "BOOKING_" + to.value
    elif booking.conversation_id is not None:
        conversation = db.get(Conversation, booking.conversation_id)
        if conversation is not None and conversation.attention_reason == "BOOKING_PENDING":
            conversation.status = ConversationStatus.OPEN
            conversation.attention_reason = None
    audit_service.log_event(
        db,
        event_type=event_type,
        message=f"Запись #{booking.id}: {previous.value} → {to.value}",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={"booking_id": booking.id, "from": previous.value, "to": to.value},
    )
    after = _fire(db, booking, to.value.lower())
    db.commit()
    _run_after_commit(after)
    db.refresh(booking)
    return booking


def confirm(db: Session, ctx: BusinessContext, booking: Booking) -> Booking:
    return _decide(
        db,
        ctx,
        booking,
        allowed_from=(BookingStatus.PENDING,),
        to=BookingStatus.CONFIRMED,
        event_type=audit_service.EventType.BOOKING_CONFIRMED,
        verb="подтвердить",
    )


def reject(db: Session, ctx: BusinessContext, booking: Booking) -> Booking:
    return _decide(
        db,
        ctx,
        booking,
        allowed_from=(BookingStatus.PENDING,),
        to=BookingStatus.REJECTED,
        event_type=audit_service.EventType.BOOKING_REJECTED,
        verb="отклонить",
    )


def cancel(db: Session, ctx: BusinessContext, booking: Booking) -> Booking:
    return _decide(
        db,
        ctx,
        booking,
        allowed_from=ACTIVE,
        to=BookingStatus.CANCELLED,
        event_type=audit_service.EventType.BOOKING_CANCELLED,
        verb="отменить",
        staff_only=True,
    )


def reschedule(
    db: Session,
    ctx: BusinessContext,
    booking: Booking,
    *,
    starts_at: datetime,
    master_id: int | None = None,
    now: datetime | None = None,
) -> Booking:
    """Перенести активную запись на другое время (и, по желанию, к другому мастеру).

    Проверка сайта 2026-10-01: раньше перенос был «отменить + записать заново» —
    клиент получал «запись отменена» сразу после «перенесли вас». Теперь одно
    сообщение «Ваша запись перенесена». Те же проверки, что при создании; запись
    сама с собой не пересекается. Решение сотрудника — запись подтверждена."""
    assert_can_view(db, ctx, booking)
    if not master_service.is_staff(ctx):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав для этого действия"
        )
    if booking.status not in ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Перенести можно только запись, которая ждёт подтверждения или подтверждена",
        )
    master = db.get(Master, master_id or booking.master_id)
    if master is None or master.business_id != ctx.business_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Мастер не найден")
    try:
        return _move(
            db, ctx.business, booking, master, starts_at, actor_user_id=ctx.user.id, now=now
        )
    except BookingConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def _move(
    db: Session,
    business: Business,
    booking: Booking,
    master: Master,
    starts_at: datetime,
    *,
    actor_user_id: int | None,
    now: datetime | None = None,
) -> Booking:
    """Перенос записи. actor_user_id=None — перенёс сам клиент в чате (решение
    2026-10-01): статус сохраняется (бронь ассистента подтверждает человек),
    мастер получает «Клиент перенёс запись». BookingConflict — время нельзя."""
    if booking.status not in ACTIVE:
        raise BookingConflict("Запись уже не активна")
    old_start, old_master_id = _aware(booking.starts_at), booking.master_id
    starts_at = _aware(starts_at).astimezone(UTC)
    ends_at = starts_at + (_aware(booking.ends_at) - old_start)

    def conflict(reason: str) -> BookingConflict:
        db.rollback()
        return BookingConflict(reason)

    if not master.active:
        raise conflict("Мастер не принимает записи")
    if booking.service_id is not None and booking.service_id not in _allowed_service_ids(
        db, master, business.id
    ):
        raise conflict("Мастер не выполняет эту услугу")
    if starts_at < (now or utcnow()):
        raise conflict("Время уже прошло")
    db.execute(update(Master).where(Master.id == master.id).values(active=Master.active))
    if not _inside_shift(db, business, master.id, starts_at, ends_at):
        raise conflict("Время вне смены мастера")
    clash = db.scalar(
        select(Booking.id).where(
            Booking.id != booking.id,
            Booking.master_id == master.id,
            Booking.status.in_(ACTIVE),
            Booking.starts_at < ends_at,
            Booking.ends_at > starts_at,
        )
    )
    if clash is not None:
        raise conflict("Это время уже занято")

    by_client = actor_user_id is None
    # Прежний мастер узнаёт, что запись ушла к другому (уведомление по старым данным).
    after = _fire(db, booking, "moved_away") if master.id != old_master_id else []
    booking.master_id = master.id
    booking.starts_at = starts_at
    booking.ends_at = ends_at
    # Новое время — напоминания клиенту отправятся заново.
    booking.reminded_day_at = None
    booking.reminded_soon_at = None
    if not by_client:
        booking.status = BookingStatus.CONFIRMED
        booking.decided_by_user_id = actor_user_id
        if booking.conversation_id is not None:
            conversation = db.get(Conversation, booking.conversation_id)
            if conversation is not None and conversation.attention_reason == "BOOKING_PENDING":
                conversation.status = ConversationStatus.OPEN
                conversation.attention_reason = None
    tz = schedule_service.business_tz(business)
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.BOOKING_RESCHEDULED,
        message=(
            f"Запись #{booking.id} перенесена{' клиентом' if by_client else ''}: "
            f"{old_start.astimezone(tz):%d.%m %H:%M} → "
            f"{starts_at.astimezone(tz):%d.%m %H:%M}, мастер «{master.display_name}»"
        ),
        business_id=business.id,
        actor_user_id=actor_user_id,
        payload={
            "booking_id": booking.id,
            "from": old_start.isoformat(),
            "to": starts_at.isoformat(),
            "from_master_id": old_master_id,
            "to_master_id": master.id,
            "by_client": by_client,
        },
    )
    after += _fire(db, booking, "rescheduled_by_client" if by_client else "rescheduled")
    db.commit()
    _run_after_commit(after)
    db.refresh(booking)
    return booking


def move_by_client(
    db: Session, business: Business, booking: Booking, master: Master, starts_at: datetime
) -> Booking:
    """Клиент сам перенёс свою запись в чате (решение 2026-10-01)."""
    return _move(db, business, booking, master, starts_at, actor_user_id=None)


def cancel_by_client(db: Session, business: Business, booking: Booking) -> Booking:
    """Клиент сам отменил свою запись в чате (решение 2026-10-01): ответ ему даёт
    бот, мастер получает «Клиент отменил запись»."""
    if booking.business_id != business.id or booking.status not in ACTIVE:
        raise BookingConflict("Запись уже не активна")
    previous = booking.status
    booking.status = BookingStatus.CANCELLED
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.BOOKING_CANCELLED,
        message=f"Запись #{booking.id}: {previous.value} → CANCELLED (отменил клиент)",
        business_id=business.id,
        payload={
            "booking_id": booking.id,
            "from": previous.value,
            "to": "CANCELLED",
            "by_client": True,
        },
    )
    after = _fire(db, booking, "cancelled_by_client")
    db.commit()
    _run_after_commit(after)
    db.refresh(booking)
    return booking


def create_by_staff(
    db: Session,
    ctx: BusinessContext,
    *,
    master_id: int,
    service_id: int,
    starts_at: datetime,
    client_name: str,
    comment: str | None,
    conversation_id: int | None = None,
    customer_id: int | None = None,
) -> Booking:
    master = db.get(Master, master_id)
    service = db.get(Service, service_id)
    if master is None or master.business_id != ctx.business_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Мастер не найден")
    if service is None or service.business_id != ctx.business_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Услуга не найдена")
    try:
        return create_booking(
            db,
            ctx.business,
            master=master,
            service=service,
            starts_at=starts_at,
            client_name=client_name,
            source=BookingSource.STAFF,
            created_by_user_id=ctx.user.id,
            comment=comment,
            conversation_id=conversation_id,
            customer_id=customer_id,
        )
    except BookingConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
