"""
Смены мастеров («таблица рабочего времени») — вне ТЗ (§22), по решению заказчика.

Расписание задаётся по датам: мастер отмечает рабочие интервалы на конкретные дни
(несколько интервалов в день — перерыв). Время — в часовом поясе компании
(businesses.timezone). Права:
* владелец — правит смены любого мастера;
* мастер — только свои;
* менеджер — видит расписание всех мастеров, но не правит.
Смену, на которую есть активные записи, нельзя удалить или сузить так, чтобы
запись оказалась вне рабочего времени.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Booking, BookingStatus, Business, Master, MasterShift, MemberRole
from services import audit_service, master_service
from services.access_service import BusinessContext

MAX_RANGE_DAYS = 62  # не больше двух месяцев за запрос
ACTIVE_BOOKING = (BookingStatus.PENDING, BookingStatus.CONFIRMED)


def business_tz(business: Business) -> ZoneInfo:
    try:
        return ZoneInfo(business.timezone or "Europe/Moscow")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def local_to_utc(business: Business, day: date, moment: time) -> datetime:
    return datetime.combine(day, moment, tzinfo=business_tz(business)).astimezone(ZoneInfo("UTC"))


def _check_range(day_from: date, day_to: date) -> None:
    if day_to < day_from:
        raise HTTPException(status_code=422, detail="Конец периода раньше начала")
    if (day_to - day_from).days > MAX_RANGE_DAYS:
        raise HTTPException(status_code=422, detail="Слишком длинный период")


def list_shifts(
    db: Session,
    ctx: BusinessContext,
    day_from: date,
    day_to: date,
    master_id: int | None = None,
) -> list[MasterShift]:
    _check_range(day_from, day_to)
    stmt = select(MasterShift).where(
        MasterShift.business_id == ctx.business_id,
        MasterShift.day >= day_from,
        MasterShift.day <= day_to,
    )
    if ctx.role is MemberRole.MASTER and not ctx.is_platform_admin:
        own = master_service.own_master(db, ctx)
        if own is None:
            return []
        stmt = stmt.where(MasterShift.master_id == own.id)
    elif master_id is not None:
        stmt = stmt.where(MasterShift.master_id == master_id)
    return list(db.scalars(stmt.order_by(MasterShift.day, MasterShift.start_time)))


def _require_edit(ctx: BusinessContext, master: Master) -> None:
    if not master_service.can_edit_schedule(ctx, master):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав для этого действия"
        )


def _validate_interval(
    db: Session, master: Master, day: date, start: time, end: time, exclude_id: int | None = None
) -> None:
    if start >= end:
        raise HTTPException(status_code=422, detail="Начало смены должно быть раньше конца")
    stmt = select(MasterShift).where(
        MasterShift.master_id == master.id,
        MasterShift.day == day,
        MasterShift.start_time < end,
        MasterShift.end_time > start,
    )
    if exclude_id is not None:
        stmt = stmt.where(MasterShift.id != exclude_id)
    if db.scalar(stmt) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Смена пересекается с другой сменой мастера",
        )


def _bookings_outside(
    db: Session, business: Business, shift: MasterShift, new: tuple[time, time] | None
) -> bool:
    """Останутся ли активные записи смены вне рабочего времени после изменения
    (new=None — смена удаляется)."""
    start_utc = local_to_utc(business, shift.day, shift.start_time)
    end_utc = local_to_utc(business, shift.day, shift.end_time)
    bookings = db.scalars(
        select(Booking).where(
            Booking.master_id == shift.master_id,
            Booking.status.in_(ACTIVE_BOOKING),
            Booking.starts_at < end_utc,
            Booking.ends_at > start_utc,
        )
    ).all()
    if not bookings:
        return False
    if new is None:
        return True
    new_start = local_to_utc(business, shift.day, new[0])
    new_end = local_to_utc(business, shift.day, new[1])
    return any(_aware(b.starts_at) < new_start or _aware(b.ends_at) > new_end for b in bookings)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=ZoneInfo("UTC"))


def create_shift(
    db: Session, ctx: BusinessContext, master: Master, day: date, start: time, end: time
) -> MasterShift:
    _require_edit(ctx, master)
    _validate_interval(db, master, day, start, end)
    shift = MasterShift(
        business_id=ctx.business_id, master_id=master.id, day=day, start_time=start, end_time=end
    )
    db.add(shift)
    db.flush()
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.SHIFT_CREATED,
        message=f"Смена мастера «{master.display_name}»: {day:%d.%m} {start:%H:%M}–{end:%H:%M}",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={"shift_id": shift.id, "master_id": master.id},
    )
    db.commit()
    db.refresh(shift)
    return shift


def _overlaps(db: Session, master_id: int, day: date, start: time, end: time) -> bool:
    return (
        db.scalar(
            select(MasterShift.id).where(
                MasterShift.master_id == master_id,
                MasterShift.day == day,
                MasterShift.start_time < end,
                MasterShift.end_time > start,
            )
        )
        is not None
    )


def fill_shifts(
    db: Session,
    ctx: BusinessContext,
    master: Master,
    *,
    date_from: date,
    date_to: date,
    weekdays: set[int],
    start: time,
    end: time,
) -> tuple[int, int]:
    """Смены по дням недели за период — решение 2026-10-01 («пн–пт 10–20 на месяц»
    одним действием). Дни, где уже есть пересекающаяся смена, пропускаются.
    Возвращает (создано, пропущено)."""
    _require_edit(ctx, master)
    _check_range(date_from, date_to)
    if start >= end:
        raise HTTPException(status_code=422, detail="Начало смены должно быть раньше конца")
    if not weekdays:
        raise HTTPException(status_code=422, detail="Отметьте хотя бы один день недели")
    created = skipped = 0
    day = date_from
    while day <= date_to:
        if day.weekday() in weekdays:
            if _overlaps(db, master.id, day, start, end):
                skipped += 1
            else:
                db.add(
                    MasterShift(
                        business_id=ctx.business_id,
                        master_id=master.id,
                        day=day,
                        start_time=start,
                        end_time=end,
                    )
                )
                db.flush()
                created += 1
        day += timedelta(days=1)
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.SHIFTS_FILLED,
        message=(
            f"Смены мастера «{master.display_name}» по шаблону: {date_from:%d.%m}–"
            f"{date_to:%d.%m} {start:%H:%M}–{end:%H:%M}, создано {created}"
        ),
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={
            "master_id": master.id,
            "weekdays": sorted(weekdays),
            "created": created,
            "skipped": skipped,
        },
    )
    db.commit()
    return created, skipped


def copy_week(
    db: Session, ctx: BusinessContext, masters: list[Master], *, week: date, weeks: int
) -> tuple[int, int]:
    """Скопировать смены недели (с понедельника week) на следующие weeks недель
    для мастеров, чьё расписание пользователь может править. Возвращает
    (создано, пропущено из-за пересечений)."""
    source = week_start(week)
    editable = [m for m in masters if master_service.can_edit_schedule(ctx, m)]
    if not editable:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав для этого действия"
        )
    shifts = db.scalars(
        select(MasterShift).where(
            MasterShift.business_id == ctx.business_id,
            MasterShift.master_id.in_([m.id for m in editable]),
            MasterShift.day >= source,
            MasterShift.day <= source + timedelta(days=6),
        )
    ).all()
    created = skipped = 0
    for k in range(1, weeks + 1):
        for shift in shifts:
            day = shift.day + timedelta(days=7 * k)
            if _overlaps(db, shift.master_id, day, shift.start_time, shift.end_time):
                skipped += 1
                continue
            db.add(
                MasterShift(
                    business_id=ctx.business_id,
                    master_id=shift.master_id,
                    day=day,
                    start_time=shift.start_time,
                    end_time=shift.end_time,
                )
            )
            db.flush()
            created += 1
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.SHIFTS_COPIED,
        message=f"Смены недели с {source:%d.%m} скопированы на {weeks} нед., создано {created}",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={
            "week": source.isoformat(),
            "weeks": weeks,
            "created": created,
            "skipped": skipped,
        },
    )
    db.commit()
    return created, skipped


def update_shift(
    db: Session, ctx: BusinessContext, shift: MasterShift, start: time, end: time
) -> MasterShift:
    master = db.get_one(Master, shift.master_id)
    _require_edit(ctx, master)
    _validate_interval(db, master, shift.day, start, end, exclude_id=shift.id)
    if _bookings_outside(db, ctx.business, shift, (start, end)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="На смену есть записи, которые окажутся вне рабочего времени",
        )
    old = f"{shift.start_time:%H:%M}–{shift.end_time:%H:%M}"
    shift.start_time, shift.end_time = start, end
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.SHIFT_UPDATED,
        message=(
            f"Смена мастера «{master.display_name}» {shift.day:%d.%m}: "
            f"{old} → {start:%H:%M}–{end:%H:%M}"
        ),
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={"shift_id": shift.id, "master_id": master.id},
    )
    db.commit()
    db.refresh(shift)
    return shift


def delete_shift(db: Session, ctx: BusinessContext, shift: MasterShift) -> None:
    master = db.get_one(Master, shift.master_id)
    _require_edit(ctx, master)
    if _bookings_outside(db, ctx.business, shift, None):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="На смену есть записи: сначала отмените или перенесите их",
        )
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.SHIFT_DELETED,
        message=(
            f"Удалена смена мастера «{master.display_name}» "
            f"{shift.day:%d.%m} {shift.start_time:%H:%M}–{shift.end_time:%H:%M}"
        ),
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={"shift_id": shift.id, "master_id": master.id},
    )
    db.delete(shift)
    db.commit()


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())
