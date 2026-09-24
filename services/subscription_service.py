"""
Срок подписки компании — этап 8 «SaaS-автоматизация» (разделы 15, 19, 20 ТЗ).

Платежей в MVP нет (§19: сложная биллинговая система вне MVP): тариф и срок
продлевает ADMIN вручную после оплаты по счёту. Здесь только правило доступа:
когда срок trial или оплаченного периода истёк, AI перестаёт отвечать клиентам,
но входящие сообщения по-прежнему сохраняются, видны в кабинете и менеджер
отвечает вручную — лиды не теряются (§18).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from config import settings
from models import Subscription, SubscriptionPlan, SubscriptionStatus, utcnow

_DAY_SECONDS = 86400


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite отдаёт даты без часового пояса — приводим к UTC для сравнений."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def plan_price(plan: SubscriptionPlan) -> int:
    """Месячная цена тарифа, ₽ (заглушки из настроек: ТЗ сетку не задаёт)."""
    return {
        SubscriptionPlan.TRIAL: 0,
        SubscriptionPlan.START: settings.plan_price_start_rub,
        SubscriptionPlan.PRO: settings.plan_price_pro_rub,
    }[plan]


def is_active(subscription: Subscription | None, now: datetime) -> bool:
    """Действует ли подписка: не отменена и срок не истёк (expires_at NULL — бессрочно).

    Компания без записи подписки (созданная до этапа 6) не ограничивается:
    ADMIN видит её в панели и назначает тариф.
    """
    if subscription is None:
        return True
    if subscription.status is not SubscriptionStatus.ACTIVE:
        return False
    expires_at = as_utc(subscription.expires_at)
    return expires_at is None or expires_at >= now


def get_subscription(db: Session, business_id: int) -> Subscription | None:
    return db.scalar(select(Subscription).where(Subscription.business_id == business_id))


def ai_allowed(db: Session, business_id: int, now: datetime | None = None) -> bool:
    """Может ли AI отвечать клиентам компании (срок подписки не истёк)."""
    return is_active(get_subscription(db, business_id), now or utcnow())


@dataclass(frozen=True)
class SubscriptionInfo:
    """Сведения о подписке для кабинета: тариф, срок, предупреждение."""

    plan: SubscriptionPlan
    expires_at: datetime | None
    active: bool
    days_left: int | None  # полных и неполных суток до конца срока; None — бессрочно
    expiring_soon: bool  # осталось не больше SUBSCRIPTION_WARNING_DAYS
    billing_contact: str | None


def subscription_info(
    db: Session, business_id: int, now: datetime | None = None
) -> SubscriptionInfo | None:
    subscription = get_subscription(db, business_id)
    if subscription is None:
        return None
    now = now or utcnow()
    active = is_active(subscription, now)
    expires_at = as_utc(subscription.expires_at)
    days_left: int | None = None
    if expires_at is not None:
        seconds = (expires_at - now).total_seconds()
        # Округление вверх: «остался 1 день», пока срок не истёк.
        days_left = max(0, -int(-seconds // _DAY_SECONDS))
    return SubscriptionInfo(
        plan=subscription.plan,
        expires_at=expires_at,
        active=active,
        days_left=days_left,
        expiring_soon=(
            active and days_left is not None and days_left <= settings.subscription_warning_days
        ),
        billing_contact=settings.billing_contact,
    )
