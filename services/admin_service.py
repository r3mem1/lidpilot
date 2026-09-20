"""
Административная панель владельца SaaS — раздел 15 ТЗ (этап 6).

Единственное место платформы, где данные читаются БЕЗ фильтра по business_id: ADMIN видит
все компании (раздел 5). Поэтому весь такой код собран здесь и вызывается только из
маршрутов, закрытых `require_platform_admin` (routes/admin.py, routes/admin_pages.py);
кабинет бизнеса этот модуль не использует.

Секреты в выдачу не попадают: у интеграций отдаются канал, статус и текст ошибки, но не
credentials_ref и не хэш секрета webhook; в payload логов значения секретов маскируются.
Изменения статуса, тарифа и пробного периода пишутся в аудит (разделы 16–17).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from config import settings
from models import (
    Business,
    BusinessMember,
    BusinessStatus,
    Integration,
    IntegrationStatus,
    LogLevel,
    Message,
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
    SystemLog,
    User,
    utcnow,
)
from schemas import (
    AdminBusinessItem,
    AdminBusinessPage,
    AdminIntegrationOut,
    AdminLogItem,
    AdminLogPage,
    AdminMetrics,
    AdminSubscriptionUpdate,
)
from services import audit_service

ERROR_LEVELS = (LogLevel.ERROR, LogLevel.CRITICAL)
MAX_LAST_ERROR = 300


# --------------------------------------------------------------------------- #
# Тарифы и подписки (раздел 15)
# --------------------------------------------------------------------------- #
def plan_price(plan: SubscriptionPlan) -> int:
    """Месячная цена тарифа, ₽ (заглушки из настроек: ТЗ сетку не задаёт)."""
    return {
        SubscriptionPlan.TRIAL: 0,
        SubscriptionPlan.START: settings.plan_price_start_rub,
        SubscriptionPlan.PRO: settings.plan_price_pro_rub,
    }[plan]


def new_trial_subscription(business_id: int) -> Subscription:
    """Пробная подписка новой компании (вызывается при создании компании)."""
    now = utcnow()
    return Subscription(
        business_id=business_id,
        plan=SubscriptionPlan.TRIAL,
        status=SubscriptionStatus.ACTIVE,
        started_at=now,
        expires_at=now + timedelta(days=settings.trial_days),
    )


def ensure_subscription(db: Session, business: Business) -> Subscription:
    """Подписка компании; для компании без записи (импорт, ручная вставка) создаётся пробная."""
    subscription = db.scalar(select(Subscription).where(Subscription.business_id == business.id))
    if subscription is None:
        subscription = new_trial_subscription(business.id)
        db.add(subscription)
        db.flush()
    return subscription


def _aware(value: datetime | None) -> datetime | None:
    """SQLite отдаёт даты без часового пояса — приводим к UTC для сравнений."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def is_trial_expired(business: Business, subscription: Subscription, now: datetime) -> bool:
    expires_at = _aware(subscription.expires_at)
    return (
        business.status is BusinessStatus.TRIAL
        and subscription.plan is SubscriptionPlan.TRIAL
        and expires_at is not None
        and expires_at < now
    )


# --------------------------------------------------------------------------- #
# Список компаний (раздел 15: поиск, фильтры, показатели)
# --------------------------------------------------------------------------- #
def _filtered(
    stmt: Select, *, q: str | None, business_status: BusinessStatus | None, plan
) -> Select:
    if q:
        stmt = stmt.where(Business.name.icontains(q.strip(), autoescape=True))
    if business_status is not None:
        stmt = stmt.where(Business.status == business_status)
    if plan is not None:
        stmt = stmt.where(Subscription.plan == plan)
    return stmt


def _integrations_by_business(
    db: Session, business_ids: list[int]
) -> dict[int, list[AdminIntegrationOut]]:
    result: dict[int, list[AdminIntegrationOut]] = {bid: [] for bid in business_ids}
    if not business_ids:
        return result
    for integration in db.scalars(
        select(Integration)
        .where(Integration.business_id.in_(business_ids))
        .order_by(Integration.id)
    ):
        result[integration.business_id].append(_integration_out(integration))
    return result


def _integration_out(integration: Integration) -> AdminIntegrationOut:
    error = integration.last_error
    return AdminIntegrationOut(
        channel=integration.channel,
        status=integration.status,
        external_account_name=integration.external_account_name,
        last_error=mask_text(error[:MAX_LAST_ERROR]) if error else None,
        created_at=integration.created_at,
    )


def _items(db: Session, rows: list[tuple[Business, Subscription]]) -> list[AdminBusinessItem]:
    """Показатели считаются только для компаний текущей страницы, а не по всей таблице
    сообщений: список остаётся быстрым при росте данных."""
    ids = [business.id for business, _ in rows]
    users = {
        bid: count
        for bid, count in db.execute(
            select(BusinessMember.business_id, func.count())
            .where(BusinessMember.business_id.in_(ids))
            .group_by(BusinessMember.business_id)
        ).all()
    }
    messages = {
        bid: (count, last)
        for bid, count, last in db.execute(
            select(Message.business_id, func.count(Message.id), func.max(Message.created_at))
            .where(Message.business_id.in_(ids))
            .group_by(Message.business_id)
        ).all()
    }
    integrations = _integrations_by_business(db, ids)
    now = utcnow()
    return [
        AdminBusinessItem(
            id=business.id,
            name=business.name,
            category=business.category,
            status=business.status,
            plan=subscription.plan,
            subscription_status=subscription.status,
            expires_at=subscription.expires_at,
            trial_expired=is_trial_expired(business, subscription, now),
            created_at=business.created_at,
            users_count=users.get(business.id, 0),
            messages_count=messages.get(business.id, (0, None))[0],
            last_activity_at=messages.get(business.id, (0, None))[1],
            integrations=integrations[business.id],
        )
        for business, subscription in rows
    ]


def _backfill_subscriptions(db: Session) -> None:
    """Компании без подписки получают пробную (данные, созданные мимо create_business)."""
    missing = db.scalars(
        select(Business).where(
            ~select(Subscription.id).where(Subscription.business_id == Business.id).exists()
        )
    ).all()
    for business in missing:
        db.add(new_trial_subscription(business.id))
    if missing:
        db.commit()


def list_businesses(
    db: Session,
    *,
    q: str | None = None,
    business_status: BusinessStatus | None = None,
    plan: SubscriptionPlan | None = None,
    limit: int = 50,
    offset: int = 0,
) -> AdminBusinessPage:
    _backfill_subscriptions(db)
    base = select(Business, Subscription).join(
        Subscription, Subscription.business_id == Business.id
    )
    base = _filtered(base, q=q, business_status=business_status, plan=plan)
    count = (
        select(func.count(Business.id))
        .select_from(Business)
        .join(Subscription, Subscription.business_id == Business.id)
    )
    total = db.scalar(_filtered(count, q=q, business_status=business_status, plan=plan)) or 0
    rows = db.execute(
        base.order_by(Business.created_at.desc(), Business.id.desc()).limit(limit).offset(offset)
    ).all()
    return AdminBusinessPage(
        total=total, limit=limit, offset=offset, items=_items(db, [(b, s) for b, s in rows])
    )


def get_business_item(db: Session, business: Business) -> AdminBusinessItem:
    subscription = ensure_subscription(db, business)
    return _items(db, [(business, subscription)])[0]


def get_business(db: Session, business_id: int) -> Business:
    business = db.get(Business, business_id)
    if business is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Компания не найдена")
    return business


def business_detail(db: Session, business: Business) -> dict[str, Any]:
    """Карточка компании для ADMIN: показатели, сотрудники, интеграции, свежие ошибки."""
    item = get_business_item(db, business)
    members = db.execute(
        select(User.email, BusinessMember.role, User.status)
        .join(BusinessMember, BusinessMember.user_id == User.id)
        .where(BusinessMember.business_id == business.id)
        .order_by(BusinessMember.id)
    ).all()
    since = utcnow() - timedelta(hours=24)
    errors = db.scalars(
        select(SystemLog)
        .where(SystemLog.business_id == business.id, SystemLog.level.in_(ERROR_LEVELS))
        .order_by(SystemLog.id.desc())
        .limit(10)
    ).all()
    return {
        "item": item,
        "business": business,
        "owner_email": db.scalar(select(User.email).where(User.id == business.owner_id)),
        "members": members,
        "messages_24h": db.scalar(
            select(func.count(Message.id)).where(
                Message.business_id == business.id, Message.created_at >= since
            )
        )
        or 0,
        "errors": [_log_item(entry, business.name) for entry in errors],
    }


# --------------------------------------------------------------------------- #
# Изменение статуса, тарифа, пробного периода (раздел 15) — с аудитом
# --------------------------------------------------------------------------- #
def set_business_status(
    db: Session, admin: User, business: Business, new_status: BusinessStatus
) -> Business:
    old_status = business.status
    if old_status is new_status:
        return business
    business.status = new_status
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.ADMIN_BUSINESS_STATUS_CHANGED,
        message=(
            f"Статус компании «{business.name}» изменён: {old_status.value} → {new_status.value}"
        ),
        level=LogLevel.WARNING if new_status is BusinessStatus.SUSPENDED else LogLevel.INFO,
        business_id=business.id,
        actor_user_id=admin.id,
        payload={"old_status": old_status.value, "new_status": new_status.value},
    )
    db.commit()
    db.refresh(business)
    return business


def _snapshot(subscription: Subscription) -> dict[str, Any]:
    expires_at = _aware(subscription.expires_at)
    return {
        "plan": subscription.plan.value,
        "status": subscription.status.value,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


def update_subscription(
    db: Session, admin: User, business: Business, payload: AdminSubscriptionUpdate
) -> Subscription:
    subscription = ensure_subscription(db, business)
    before = _snapshot(subscription)
    now = utcnow()

    if payload.plan is not None and payload.plan is not subscription.plan:
        # Смена тарифа начинает новый период: платный — без срока, пока ADMIN его не задаст;
        # возврат на пробный — с новым пробным сроком.
        subscription.plan = payload.plan
        subscription.started_at = now
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.expires_at = (
            now + timedelta(days=settings.trial_days)
            if payload.plan is SubscriptionPlan.TRIAL
            else None
        )
    if payload.status is not None:
        subscription.status = payload.status
    if payload.expires_on is not None:
        subscription.expires_at = datetime.combine(payload.expires_on, time(23, 59, 59), tzinfo=UTC)
    if payload.extend_days is not None:
        current = _aware(subscription.expires_at)
        base = current if current is not None and current > now else now
        subscription.expires_at = base + timedelta(days=payload.extend_days)

    after = _snapshot(subscription)
    if after != before:
        audit_service.log_event(
            db,
            event_type=audit_service.EventType.ADMIN_SUBSCRIPTION_CHANGED,
            message=(
                f"Подписка компании «{business.name}» изменена: "
                f"{before['plan']} → {after['plan']}, срок до {after['expires_at'] or 'без срока'}"
            ),
            business_id=business.id,
            actor_user_id=admin.id,
            payload={"old": before, "new": after},
        )
    db.commit()
    db.refresh(subscription)
    return subscription


# --------------------------------------------------------------------------- #
# Метрики SaaS (раздел 15): компании, активные, trial, подписки, MRR
# --------------------------------------------------------------------------- #
def get_metrics(db: Session) -> AdminMetrics:
    """MRR = сумма цен платных тарифов у компаний со статусом ACTIVE и активной подпиской.
    Приостановленные и пробные компании в MRR не входят."""
    _backfill_subscriptions(db)
    now = utcnow()
    by_status = {
        business_status: count
        for business_status, count in db.execute(
            select(Business.status, func.count()).group_by(Business.status)
        ).all()
    }

    paid = db.execute(
        select(Subscription.plan, func.count())
        .join(Business, Business.id == Subscription.business_id)
        .where(
            Business.status == BusinessStatus.ACTIVE,
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.plan != SubscriptionPlan.TRIAL,
        )
        .group_by(Subscription.plan)
    ).all()

    trials_expired = (
        db.scalar(
            select(func.count())
            .select_from(Business)
            .join(Subscription, Subscription.business_id == Business.id)
            .where(
                Business.status == BusinessStatus.TRIAL,
                Subscription.plan == SubscriptionPlan.TRIAL,
                Subscription.expires_at.is_not(None),
                Subscription.expires_at < now,
            )
        )
        or 0
    )
    day_ago = now - timedelta(hours=24)
    return AdminMetrics(
        companies_total=sum(by_status.values()),
        companies_active=by_status.get(BusinessStatus.ACTIVE, 0),
        companies_trial=by_status.get(BusinessStatus.TRIAL, 0),
        companies_suspended=by_status.get(BusinessStatus.SUSPENDED, 0),
        trials_expired=trials_expired,
        paid_subscriptions=sum(count for _, count in paid),
        mrr_rub=sum(plan_price(plan) * count for plan, count in paid),
        integrations_with_errors=db.scalar(
            select(func.count())
            .select_from(Integration)
            .where(Integration.status == IntegrationStatus.ERROR)
        )
        or 0,
        errors_24h=db.scalar(
            select(func.count())
            .select_from(SystemLog)
            .where(SystemLog.level.in_(ERROR_LEVELS), SystemLog.created_at >= day_ago)
        )
        or 0,
        messages_24h=db.scalar(
            select(func.count()).select_from(Message).where(Message.created_at >= day_ago)
        )
        or 0,
    )


def integrations_with_errors(
    db: Session, limit: int = 10
) -> list[tuple[str, int, AdminIntegrationOut]]:
    """Интеграции в статусе ERROR: (название компании, id компании, интеграция)."""
    rows = db.execute(
        select(Business.name, Business.id, Integration)
        .join(Business, Business.id == Integration.business_id)
        .where(Integration.status == IntegrationStatus.ERROR)
        .order_by(Integration.updated_at.desc())
        .limit(limit)
    ).all()
    return [(name, bid, _integration_out(integration)) for name, bid, integration in rows]


def expired_trials(db: Session, limit: int = 10) -> list[AdminBusinessItem]:
    rows = db.execute(
        select(Business, Subscription)
        .join(Subscription, Subscription.business_id == Business.id)
        .where(
            Business.status == BusinessStatus.TRIAL,
            Subscription.plan == SubscriptionPlan.TRIAL,
            Subscription.expires_at.is_not(None),
            Subscription.expires_at < utcnow(),
        )
        .order_by(Subscription.expires_at)
        .limit(limit)
    ).all()
    return _items(db, [(b, s) for b, s in rows])


# --------------------------------------------------------------------------- #
# Системные логи (разделы 16, 17): чтение с маскированием секретов
# --------------------------------------------------------------------------- #
_SECRET_KEY = re.compile(
    r"token|secret|password|passwd|credential|authorization|api[_-]?key|cookie|hash",
    re.IGNORECASE,
)
# Токен бота Telegram («123456789:AA…») и адрес Bot API с токеном
_BOT_TOKEN = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")  # без \b: в URL перед токеном стоит «bot»
MASK = "***"


def mask_text(value: str) -> str:
    return _BOT_TOKEN.sub(MASK, value)


def mask_payload(value: Any) -> Any:
    """Значения секретов в payload логов не показываются даже ADMIN (раздел 16)."""
    if isinstance(value, dict):
        return {
            key: MASK if _SECRET_KEY.search(str(key)) else mask_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [mask_payload(item) for item in value]
    if isinstance(value, str):
        return mask_text(value)
    return value


def _log_item(entry: SystemLog, business_name: str | None) -> AdminLogItem:
    return AdminLogItem(
        id=entry.id,
        business_id=entry.business_id,
        business_name=business_name,
        level=entry.level,
        event_type=entry.event_type,
        message=mask_text(entry.message),
        payload=mask_payload(entry.payload) if entry.payload else None,
        created_at=entry.created_at,
    )


def list_logs(
    db: Session,
    *,
    levels: list[LogLevel] | None = None,
    event_type: str | None = None,
    business_id: int | None = None,
    q: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> AdminLogPage:
    conditions = []
    if levels:
        conditions.append(SystemLog.level.in_(levels))
    if event_type:
        conditions.append(SystemLog.event_type == event_type)
    if business_id is not None:
        conditions.append(SystemLog.business_id == business_id)
    if q:
        conditions.append(SystemLog.message.icontains(q.strip(), autoescape=True))
    if date_from is not None:
        conditions.append(SystemLog.created_at >= date_from)
    if date_to is not None:
        conditions.append(SystemLog.created_at <= date_to)

    total = db.scalar(select(func.count()).select_from(SystemLog).where(*conditions)) or 0
    rows = db.execute(
        select(SystemLog, Business.name)
        .outerjoin(Business, Business.id == SystemLog.business_id)
        .where(*conditions)
        .order_by(SystemLog.created_at.desc(), SystemLog.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return AdminLogPage(
        total=total,
        limit=limit,
        offset=offset,
        items=[_log_item(entry, name) for entry, name in rows],
    )


def event_types(db: Session) -> list[str]:
    """Типы событий, реально встречающиеся в логах (для фильтра в панели)."""
    return list(db.scalars(select(SystemLog.event_type).distinct().order_by(SystemLog.event_type)))
