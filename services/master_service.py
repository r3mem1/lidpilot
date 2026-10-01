"""
Мастера компании — вне ТЗ (§22 «автоматическая запись»), по решению заказчика.

Мастер — сотрудник, к которому записываются клиенты. Он может быть участником
компании с ролью MASTER (тогда сам ведёт своё расписание и видит свои записи)
или «без аккаунта» — тогда его расписание ведёт владелец. Услуги мастера
задаёт владелец; пустой список означает «все услуги компании».
Все выборки ограничены business_id из BusinessContext (раздел 16).
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import (
    Booking,
    BookingStatus,
    BusinessMember,
    Master,
    MasterService,
    MemberRole,
    Service,
    User,
    utcnow,
)
from services import audit_service
from services.access_service import BusinessContext

ACTIVE_BOOKING = (BookingStatus.PENDING, BookingStatus.CONFIRMED)


def is_staff(ctx: BusinessContext) -> bool:
    """Владелец, менеджер или ADMIN платформы: видят расписание всех мастеров."""
    return ctx.is_platform_admin or ctx.role in (MemberRole.OWNER, MemberRole.MANAGER)


def can_manage_masters(ctx: BusinessContext) -> bool:
    return ctx.is_platform_admin or ctx.role is MemberRole.OWNER


def own_master(db: Session, ctx: BusinessContext) -> Master | None:
    """Профиль мастера текущего пользователя в этой компании."""
    return db.scalar(
        select(Master).where(Master.business_id == ctx.business_id, Master.user_id == ctx.user.id)
    )


def can_edit_schedule(ctx: BusinessContext, master: Master) -> bool:
    """Смены правит владелец (любого мастера) или сам мастер (свои). Менеджер — только смотрит."""
    return can_manage_masters(ctx) or (
        ctx.role is MemberRole.MASTER and master.user_id == ctx.user.id
    )


def list_masters(db: Session, ctx: BusinessContext, *, only_active: bool = False) -> list[Master]:
    stmt = select(Master).where(Master.business_id == ctx.business_id)
    if ctx.role is MemberRole.MASTER and not ctx.is_platform_admin:
        stmt = stmt.where(Master.user_id == ctx.user.id)
    if only_active:
        stmt = stmt.where(Master.active.is_(True))
    return list(db.scalars(stmt.order_by(Master.display_name, Master.id)))


def service_ids_by_master(db: Session, master_ids: list[int]) -> dict[int, list[int]]:
    result: dict[int, list[int]] = {mid: [] for mid in master_ids}
    if not master_ids:
        return result
    for master_id, service_id in db.execute(
        select(MasterService.master_id, MasterService.service_id).where(
            MasterService.master_id.in_(master_ids)
        )
    ):
        result[master_id].append(service_id)
    return result


def masters_for_service(db: Session, business_id: int, service_id: int) -> list[Master]:
    """Активные мастера, выполняющие услугу (без отмеченных услуг — выполняют все)."""
    masters = list(
        db.scalars(
            select(Master)
            .where(Master.business_id == business_id, Master.active.is_(True))
            .order_by(Master.display_name, Master.id)
        )
    )
    links = service_ids_by_master(db, [m.id for m in masters])
    return [m for m in masters if not links[m.id] or service_id in links[m.id]]


def _validate_member(db: Session, ctx: BusinessContext, user_id: int) -> User:
    member = db.scalar(
        select(BusinessMember).where(
            BusinessMember.business_id == ctx.business_id, BusinessMember.user_id == user_id
        )
    )
    user = db.get(User, user_id) if member else None
    if user is None:
        raise HTTPException(status_code=422, detail="Пользователь не состоит в компании")
    return user


def create_master(
    db: Session, ctx: BusinessContext, display_name: str, user_id: int | None = None
) -> Master:
    if user_id is not None:
        _validate_member(db, ctx, user_id)
    master = Master(business_id=ctx.business_id, user_id=user_id, display_name=display_name.strip())
    db.add(master)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="У этого сотрудника уже есть профиль мастера",
        ) from exc
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.MASTER_CREATED,
        message=f"Добавлен мастер «{master.display_name}»",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={"master_id": master.id, "user_id": user_id},
    )
    db.commit()
    db.refresh(master)
    return master


def ensure_master_for_member(
    db: Session, business_id: int, user: User, *, link_master_id: int | None = None
) -> Master:
    """Профиль мастера для участника с ролью MASTER (при принятии приглашения или
    смене роли). Вызывается внутри транзакции вызывающего кода.

    link_master_id — мастер без аккаунта из приглашения: сотрудник становится им,
    его смены и записи сохраняются (проверка сайта 2026-10-01: раньше создавался
    дубль с именем из email)."""
    master = db.scalar(
        select(Master).where(Master.business_id == business_id, Master.user_id == user.id)
    )
    if master is None and link_master_id is not None:
        linked = db.get(Master, link_master_id)
        if linked is not None and linked.business_id == business_id and linked.user_id is None:
            linked.user_id = user.id
            linked.active = True
            db.flush()
            return linked
    if master is None:
        name = user.email.split("@", 1)[0]
        master = Master(business_id=business_id, user_id=user.id, display_name=name[:120])
        db.add(master)
        db.flush()
    elif not master.active:
        master.active = True
    return master


def update_master(
    db: Session,
    ctx: BusinessContext,
    master: Master,
    *,
    display_name: str | None = None,
    active: bool | None = None,
) -> Master:
    changes: dict = {}
    if display_name is not None and display_name.strip() != master.display_name:
        changes["display_name"] = [master.display_name, display_name.strip()]
        master.display_name = display_name.strip()
    if active is not None and active != master.active:
        changes["active"] = [master.active, active]
        master.active = active
    if changes:
        audit_service.log_event(
            db,
            event_type=audit_service.EventType.MASTER_UPDATED,
            message=f"Изменён мастер «{master.display_name}»",
            business_id=ctx.business_id,
            actor_user_id=ctx.user.id,
            payload={"master_id": master.id, "changes": changes},
        )
    db.commit()
    db.refresh(master)
    return master


def set_services(
    db: Session, ctx: BusinessContext, master: Master, service_ids: list[int]
) -> list[int]:
    ids = sorted(set(service_ids))
    if ids:
        found = set(
            db.scalars(
                select(Service.id).where(
                    Service.business_id == ctx.business_id, Service.id.in_(ids)
                )
            )
        )
        if found != set(ids):
            # Чужая или несуществующая услуга — неотличимы (раздел 16).
            raise HTTPException(status_code=422, detail="Услуга не найдена")
    db.execute(delete(MasterService).where(MasterService.master_id == master.id))
    db.add_all([MasterService(master_id=master.id, service_id=sid) for sid in ids])
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.MASTER_UPDATED,
        message=f"Услуги мастера «{master.display_name}» изменены",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={"master_id": master.id, "service_ids": ids},
    )
    db.commit()
    return ids


def delete_master(db: Session, ctx: BusinessContext, master: Master) -> None:
    upcoming = db.scalar(
        select(Booking.id).where(
            Booking.master_id == master.id,
            Booking.status.in_(ACTIVE_BOOKING),
            Booking.ends_at > utcnow(),
        )
    )
    if upcoming is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="У мастера есть предстоящие записи: отмените их или отключите мастера",
        )
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.MASTER_DELETED,
        message=f"Удалён мастер «{master.display_name}»",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={"master_id": master.id},
    )
    db.delete(master)
    db.commit()
