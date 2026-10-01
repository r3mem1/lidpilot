"""
Мастера, смены и записи — ВНЕ ТЗ (§19 «автоматическая запись» вне MVP, §22 —
развитие), добавлено по решению заказчика.

    GET    /businesses/{business_id}/masters              все роли (мастер — только себя)
    POST   /businesses/{business_id}/masters              владелец
    PUT    /masters/{master_id}                           владелец
    DELETE /masters/{master_id}                           владелец
    PUT    /masters/{master_id}/services                  владелец
    GET    /businesses/{business_id}/shifts               все роли (мастер — только свои)
    POST   /masters/{master_id}/shifts                    владелец или сам мастер
    PUT    /shifts/{shift_id}                             владелец или сам мастер
    DELETE /shifts/{shift_id}                             владелец или сам мастер
    GET    /businesses/{business_id}/bookings             все роли (мастер — только свои)
    POST   /businesses/{business_id}/bookings             владелец, менеджер (request_id — по заявке)
    POST   /businesses/{business_id}/booking-requests/{id}/close   владелец, менеджер
    POST   /bookings/{booking_id}/confirm | /reject       владелец, менеджер, мастер (свои)
    POST   /bookings/{booking_id}/cancel                  владелец, менеджер
    POST   /bookings/{booking_id}/reschedule              владелец, менеджер (перенос)
    GET    /businesses/{business_id}/availability         владелец, менеджер
    POST   /masters/{master_id}/notify-link               сам мастер или владелец
    DELETE /masters/{master_id}/notify-link               сам мастер или владелец

Доступ — только через BusinessContext (access_service); чужое — 404 (раздел 16).
Менеджер видит расписание всех мастеров, но смены не правит.
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from database import get_db
from models import Booking, BookingStatus, Master, MasterShift, MemberRole, Service
from schemas import (
    BookingCreate,
    BookingOut,
    BookingReschedule,
    MasterCreate,
    MasterOut,
    MasterServicesUpdate,
    MasterUpdate,
    NotifyLinkOut,
    NotifyLinkRequest,
    ShiftCreate,
    ShiftOut,
    ShiftUpdate,
    SlotOut,
)
from services import (
    booking_request_service,
    booking_service,
    master_notify_service,
    master_service,
    schedule_service,
)
from services.access_service import (
    BusinessContext,
    require_booking_access,
    require_business_roles,
    require_master_access,
    require_shift_access,
)

router = APIRouter(tags=["bookings"])

ALL_ROLES = (MemberRole.OWNER, MemberRole.MANAGER, MemberRole.MASTER)
STAFF = (MemberRole.OWNER, MemberRole.MANAGER)
OWNER_ONLY = (MemberRole.OWNER,)
SCHEDULE_EDITORS = (MemberRole.OWNER, MemberRole.MASTER)


def _master_out(master: Master, service_ids: list[int]) -> MasterOut:
    return MasterOut(
        id=master.id,
        business_id=master.business_id,
        user_id=master.user_id,
        display_name=master.display_name,
        active=master.active,
        notify_channel=master.notify_channel,
        notify_linked=bool(master.notify_chat_id),
        service_ids=service_ids,
    )


def _period(date_from: date | None, date_to: date | None) -> tuple[date, date]:
    start = date_from or date.today()
    return start, date_to or start + timedelta(days=6)


# --------------------------------------------------------------------------- #
# Мастера
# --------------------------------------------------------------------------- #
@router.get("/businesses/{business_id}/masters", response_model=list[MasterOut])
def list_masters(
    ctx: BusinessContext = Depends(require_business_roles(*ALL_ROLES)),
    db: Session = Depends(get_db),
):
    masters = master_service.list_masters(db, ctx)
    links = master_service.service_ids_by_master(db, [m.id for m in masters])
    return [_master_out(m, links[m.id]) for m in masters]


@router.post(
    "/businesses/{business_id}/masters",
    response_model=MasterOut,
    status_code=status.HTTP_201_CREATED,
)
def create_master(
    payload: MasterCreate,
    ctx: BusinessContext = Depends(require_business_roles(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    master = master_service.create_master(db, ctx, payload.display_name, payload.user_id)
    return _master_out(master, [])


@router.put("/masters/{master_id}", response_model=MasterOut)
def update_master(
    payload: MasterUpdate,
    resolved: tuple[Master, BusinessContext] = Depends(require_master_access(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    master, ctx = resolved
    master = master_service.update_master(
        db, ctx, master, display_name=payload.display_name, active=payload.active
    )
    return _master_out(master, master_service.service_ids_by_master(db, [master.id])[master.id])


@router.delete("/masters/{master_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_master(
    resolved: tuple[Master, BusinessContext] = Depends(require_master_access(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    master, ctx = resolved
    master_service.delete_master(db, ctx, master)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/masters/{master_id}/services", response_model=MasterOut)
def set_master_services(
    payload: MasterServicesUpdate,
    resolved: tuple[Master, BusinessContext] = Depends(require_master_access(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    master, ctx = resolved
    ids = master_service.set_services(db, ctx, master, payload.service_ids)
    return _master_out(master, ids)


# --------------------------------------------------------------------------- #
# Смены
# --------------------------------------------------------------------------- #
@router.get("/businesses/{business_id}/shifts", response_model=list[ShiftOut])
def list_shifts(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    master_id: int | None = Query(default=None, ge=1),
    ctx: BusinessContext = Depends(require_business_roles(*ALL_ROLES)),
    db: Session = Depends(get_db),
):
    start, end = _period(date_from, date_to)
    return schedule_service.list_shifts(db, ctx, start, end, master_id)


@router.post(
    "/masters/{master_id}/shifts", response_model=ShiftOut, status_code=status.HTTP_201_CREATED
)
def create_shift(
    payload: ShiftCreate,
    resolved: tuple[Master, BusinessContext] = Depends(require_master_access(*SCHEDULE_EDITORS)),
    db: Session = Depends(get_db),
):
    master, ctx = resolved
    return schedule_service.create_shift(
        db, ctx, master, payload.day, payload.start_time, payload.end_time
    )


@router.put("/shifts/{shift_id}", response_model=ShiftOut)
def update_shift(
    payload: ShiftUpdate,
    resolved: tuple[MasterShift, BusinessContext] = Depends(
        require_shift_access(*SCHEDULE_EDITORS)
    ),
    db: Session = Depends(get_db),
):
    shift, ctx = resolved
    return schedule_service.update_shift(db, ctx, shift, payload.start_time, payload.end_time)


@router.delete("/shifts/{shift_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_shift(
    resolved: tuple[MasterShift, BusinessContext] = Depends(
        require_shift_access(*SCHEDULE_EDITORS)
    ),
    db: Session = Depends(get_db),
):
    shift, ctx = resolved
    schedule_service.delete_shift(db, ctx, shift)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Записи
# --------------------------------------------------------------------------- #
@router.get("/businesses/{business_id}/bookings", response_model=list[BookingOut])
def list_bookings(
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    master_id: int | None = Query(default=None, ge=1),
    booking_status: BookingStatus | None = Query(default=None, alias="status"),
    ctx: BusinessContext = Depends(require_business_roles(*ALL_ROLES)),
    db: Session = Depends(get_db),
):
    start, end = _period(date_from, date_to)
    if (end - start).days > schedule_service.MAX_RANGE_DAYS:
        raise HTTPException(status_code=422, detail="Слишком длинный период")
    return booking_service.list_bookings(
        db,
        ctx,
        day_from=start,
        day_to=end,
        master_id=master_id,
        statuses=(booking_status,) if booking_status else None,
    )


@router.post(
    "/businesses/{business_id}/bookings",
    response_model=BookingOut,
    status_code=status.HTTP_201_CREATED,
)
def create_booking(
    payload: BookingCreate,
    ctx: BusinessContext = Depends(require_business_roles(*STAFF)),
    db: Session = Depends(get_db),
):
    starts_at = schedule_service.local_to_utc(ctx.business, payload.day, payload.start_time)
    # Запись по заявке клиента (вне ТЗ, §22): заявка только своей компании (иначе 404).
    request = (
        booking_request_service.get_open(db, ctx, payload.request_id)
        if payload.request_id
        else None
    )
    booking = booking_service.create_by_staff(
        db,
        ctx,
        master_id=payload.master_id,
        service_id=payload.service_id,
        starts_at=starts_at,
        client_name=payload.client_name,
        comment=payload.comment,
        conversation_id=request.conversation_id if request else None,
        customer_id=request.customer_id if request else None,
    )
    if request is not None:
        booking_request_service.mark_done(db, ctx, request, booking.id)
        db.refresh(booking)
    return booking


@router.post(
    "/businesses/{business_id}/booking-requests/{request_id}/close",
    status_code=status.HTTP_204_NO_CONTENT,
)
def close_booking_request(
    request_id: int,
    ctx: BusinessContext = Depends(require_business_roles(*STAFF)),
    db: Session = Depends(get_db),
) -> Response:
    """Закрыть заявку без записи (вне ТЗ, §22): договорились с клиентом иначе."""
    booking_request_service.close(db, ctx, booking_request_service.get_open(db, ctx, request_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/bookings/{booking_id}/confirm", response_model=BookingOut)
def confirm_booking(
    resolved: tuple[Booking, BusinessContext] = Depends(require_booking_access(*ALL_ROLES)),
    db: Session = Depends(get_db),
):
    booking, ctx = resolved
    return booking_service.confirm(db, ctx, booking)


@router.post("/bookings/{booking_id}/reject", response_model=BookingOut)
def reject_booking(
    resolved: tuple[Booking, BusinessContext] = Depends(require_booking_access(*ALL_ROLES)),
    db: Session = Depends(get_db),
):
    booking, ctx = resolved
    return booking_service.reject(db, ctx, booking)


@router.post("/bookings/{booking_id}/cancel", response_model=BookingOut)
def cancel_booking(
    resolved: tuple[Booking, BusinessContext] = Depends(require_booking_access(*STAFF)),
    db: Session = Depends(get_db),
):
    booking, ctx = resolved
    return booking_service.cancel(db, ctx, booking)


@router.post("/bookings/{booking_id}/reschedule", response_model=BookingOut)
def reschedule_booking(
    payload: BookingReschedule,
    resolved: tuple[Booking, BusinessContext] = Depends(require_booking_access(*STAFF)),
    db: Session = Depends(get_db),
):
    """Перенос записи (вне ТЗ, §22): клиент получает одно сообщение «перенесена»."""
    booking, ctx = resolved
    starts_at = schedule_service.local_to_utc(ctx.business, payload.day, payload.start_time)
    return booking_service.reschedule(
        db, ctx, booking, starts_at=starts_at, master_id=payload.master_id
    )


@router.get("/businesses/{business_id}/availability", response_model=list[SlotOut])
def availability(
    service_id: int = Query(..., ge=1),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    master_id: int | None = Query(default=None, ge=1),
    ctx: BusinessContext = Depends(require_business_roles(*STAFF)),
    db: Session = Depends(get_db),
):
    service = db.get(Service, service_id)
    if service is None or service.business_id != ctx.business_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Услуга не найдена")
    start, end = _period(date_from, date_to)
    if (end - start).days > booking_service.HORIZON_DAYS * 2:
        raise HTTPException(status_code=422, detail="Слишком длинный период")
    slots = booking_service.free_slots(
        db, ctx.business, service, day_from=start, day_to=end, master_id=master_id, limit=200
    )
    return [
        SlotOut(
            master_id=s.master_id,
            master_name=s.master_name,
            starts_at=s.starts_at,
            ends_at=s.ends_at,
            local_start=s.local_start,
        )
        for s in slots
    ]


# --------------------------------------------------------------------------- #
# Уведомления мастеру (бот/сообщество компании)
# --------------------------------------------------------------------------- #
@router.post("/masters/{master_id}/notify-link", response_model=NotifyLinkOut)
def create_notify_link(
    payload: NotifyLinkRequest,
    resolved: tuple[Master, BusinessContext] = Depends(require_master_access(*SCHEDULE_EDITORS)),
    db: Session = Depends(get_db),
):
    master, ctx = resolved
    return master_notify_service.create_link_code(db, ctx, master, payload.channel)


@router.delete("/masters/{master_id}/notify-link", status_code=status.HTTP_204_NO_CONTENT)
def delete_notify_link(
    resolved: tuple[Master, BusinessContext] = Depends(require_master_access(*SCHEDULE_EDITORS)),
    db: Session = Depends(get_db),
):
    master, ctx = resolved
    if not master_service.can_edit_schedule(ctx, master):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав для этого действия"
        )
    master_notify_service.unlink(db, ctx, master)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
