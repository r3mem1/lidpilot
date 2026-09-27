"""
Бизнес-логика компаний, услуг и сотрудников — разделы 6.2, 6.3, 6.1 ТЗ.

Слой сервисов владеет транзакцией запроса (commit/rollback) и пишет аудит;
маршруты остаются тонкими (раздел 18: разделение по слоям).

Все выборки по услугам и сотрудникам фильтруются по business_id из
проверенного BusinessContext — сервис не принимает business_id из запроса.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai.context import BusinessKnowledge, ServiceInfo
from models import (
    Business,
    BusinessMember,
    BusinessStatus,
    Master,
    MemberRole,
    Service,
    User,
)
from schemas import (
    BusinessCreate,
    BusinessMemberCreate,
    BusinessUpdate,
    ServiceCreate,
    ServiceUpdate,
)
from services import admin_service, audit_service, master_service, schedule_service
from services.access_service import BusinessContext
from services.auth_service import get_user_by_email


# --------------------------------------------------------------------------- #
# Компания (раздел 6.2)
# --------------------------------------------------------------------------- #
def create_business(db: Session, user: User, payload: BusinessCreate) -> Business:
    """Создание компании. Создатель сразу становится OWNER в business_members —
    иначе он не прошёл бы собственную проверку доступа (раздел 16)."""
    business = Business(
        owner_id=user.id,
        status=BusinessStatus.TRIAL,  # тариф/trial управляет ADMIN (раздел 15)
        **payload.model_dump(),
    )
    db.add(business)
    db.flush()

    db.add(BusinessMember(business_id=business.id, user_id=user.id, role=MemberRole.OWNER))
    # Пробный период (раздел 15): срок и тариф дальше меняет ADMIN.
    db.add(admin_service.new_trial_subscription(business.id))

    audit_service.log_event(
        db,
        event_type=audit_service.EventType.BUSINESS_CREATED,
        message=f"Создана компания «{business.name}»",
        business_id=business.id,
        actor_user_id=user.id,
    )
    db.commit()
    db.refresh(business)
    return business


def update_business(db: Session, ctx: BusinessContext, payload: BusinessUpdate) -> Business:
    """Изменение данных компании (раздел 6.2). Статус/тариф здесь не меняются."""
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Нет полей для изменения"
        )

    for field, value in changes.items():
        setattr(ctx.business, field, value)

    audit_service.log_event(
        db,
        event_type=audit_service.EventType.BUSINESS_UPDATED,
        message="Изменены настройки компании",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={"fields": sorted(changes)},  # значения не логируем
    )
    db.commit()
    db.refresh(ctx.business)
    return ctx.business


# --------------------------------------------------------------------------- #
# Услуги (раздел 6.3)
# --------------------------------------------------------------------------- #
def list_services(db: Session, ctx: BusinessContext, active: bool | None = None) -> list[Service]:
    stmt = select(Service).where(Service.business_id == ctx.business_id)
    if active is not None:
        stmt = stmt.where(Service.active.is_(active))
    return list(db.scalars(stmt.order_by(Service.name)))


def create_service(db: Session, ctx: BusinessContext, payload: ServiceCreate) -> Service:
    service = Service(business_id=ctx.business_id, **payload.model_dump())
    db.add(service)
    db.flush()

    audit_service.log_event(
        db,
        event_type=audit_service.EventType.SERVICE_CREATED,
        message=f"Добавлена услуга «{service.name}»",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={"service_id": service.id},
    )
    db.commit()
    db.refresh(service)
    return service


def update_service(
    db: Session, ctx: BusinessContext, service: Service, payload: ServiceUpdate
) -> Service:
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Нет полей для изменения"
        )

    for field, value in changes.items():
        setattr(service, field, value)

    audit_service.log_event(
        db,
        event_type=audit_service.EventType.SERVICE_UPDATED,
        message=f"Изменена услуга «{service.name}»",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={"service_id": service.id, "fields": sorted(changes)},
    )
    db.commit()
    db.refresh(service)
    return service


def delete_service(db: Session, ctx: BusinessContext, service: Service) -> None:
    service_id, service_name = service.id, service.name
    db.delete(service)

    audit_service.log_event(
        db,
        event_type=audit_service.EventType.SERVICE_DELETED,
        message=f"Удалена услуга «{service_name}»",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={"service_id": service_id},
    )
    db.commit()


# --------------------------------------------------------------------------- #
# Сотрудники компании (раздел 6.1: привязка пользователя к компаниям)
# --------------------------------------------------------------------------- #
def list_members(db: Session, ctx: BusinessContext) -> list[tuple[BusinessMember, User]]:
    stmt = (
        select(BusinessMember, User)
        .join(User, User.id == BusinessMember.user_id)
        .where(BusinessMember.business_id == ctx.business_id)
        .order_by(BusinessMember.id)
    )
    return list(db.execute(stmt).all())  # type: ignore[arg-type]


def add_member(
    db: Session, ctx: BusinessContext, payload: BusinessMemberCreate
) -> tuple[BusinessMember, User]:
    """Привязка существующего пользователя к компании с ролью OWNER/MANAGER.

    Роль внутри компании определяется только этой записью: users.role остаётся
    платформенной характеристикой (раздел 5).
    """
    user = get_user_by_email(db, payload.email)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Пользователь с таким email не зарегистрирован",
        )

    existing = db.scalar(
        select(BusinessMember).where(
            BusinessMember.business_id == ctx.business_id,
            BusinessMember.user_id == user.id,
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Пользователь уже добавлен в компанию",
        )

    member = BusinessMember(business_id=ctx.business_id, user_id=user.id, role=payload.role)
    db.add(member)
    try:
        db.flush()
    except IntegrityError as exc:  # гонка двух одновременных добавлений
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Пользователь уже добавлен в компанию"
        ) from exc
    if payload.role is MemberRole.MASTER:
        # Вне ТЗ (§22): у мастера сразу есть профиль для расписания и записей.
        master_service.ensure_master_for_member(db, ctx.business_id, user)

    audit_service.log_event(
        db,
        event_type=audit_service.EventType.BUSINESS_MEMBER_ADDED,
        message=f"Пользователь {user.email} добавлен с ролью {payload.role.value}",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={"member_user_id": user.id, "role": payload.role.value},
    )
    db.commit()
    db.refresh(member)
    return member, user


# --------------------------------------------------------------------------- #
# Контекст бизнеса для AI (этап 2, шаг «Retrieve business context» раздела 12.1)
# --------------------------------------------------------------------------- #
def build_ai_context(db: Session, business: Business) -> BusinessKnowledge:
    """Достоверные данные компании для AI-модуля.

    Принимает уже проверенный объект Business (а не business_id): контекст
    нельзя собрать, не пройдя проверку доступа в access_service (раздел 16).

    В прайс попадают только активные услуги: снятая с продажи услуга не должна
    попасть в ответ клиенту (раздел 6.3).
    """
    services = db.scalars(
        select(Service)
        .where(Service.business_id == business.id, Service.active.is_(True))
        .order_by(Service.name)
    )
    masters = list(
        db.scalars(
            select(Master.display_name)
            .where(Master.business_id == business.id, Master.active.is_(True))
            .order_by(Master.display_name)
        )
    )
    return BusinessKnowledge(
        business_id=business.id,
        name=business.name,
        category=business.category,
        address=business.address,
        phone=business.phone,
        working_hours=business.working_hours,
        description=business.description,
        ai_rules=business.ai_rules,
        escalation_contact=business.escalation_contact,
        services=tuple(
            ServiceInfo(
                name=s.name,
                price=Decimal(str(s.price)),
                description=s.description,
                duration=s.duration,
            )
            for s in services
        ),
        # Вне ТЗ (§22): запись по расписанию мастеров, если владелец её включил и
        # есть активные мастера. Иначе, как в MVP (раздел 19), AI время не обещает.
        has_schedule_integration=business.booking_enabled and bool(masters),
        masters=tuple(masters),
        tone=business.ai_tone.value,
        auto_reply=business.ai_auto_reply,
        today=datetime.now(schedule_service.business_tz(business)).date(),
    )
