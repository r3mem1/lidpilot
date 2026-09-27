"""
Onboarding нового владельца — этап 8 «SaaS-автоматизация» (раздел 20 ТЗ).

Чек-лист на дашборде: каждый шаг отмечается по фактическим данным компании,
а не по нажатию кнопки, поэтому чек-лист не «врёт» и исчезает сам, когда
ассистент готов к работе. Данные только своей компании (BusinessContext, §8).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Conversation, Integration, IntegrationStatus, Master, MasterShift, Service
from services import schedule_service
from services.access_service import BusinessContext


@dataclass(frozen=True)
class OnboardingStep:
    key: str
    title: str
    hint: str
    link: str
    link_text: str
    done: bool


def _has(db: Session, stmt) -> bool:
    return bool(db.scalar(select(stmt.exists())))


def steps(db: Session, ctx: BusinessContext) -> list[OnboardingStep]:
    business = ctx.business
    base = f"/cabinet/{ctx.business_id}"
    profile_done = all(
        (value or "").strip()
        for value in (business.address, business.phone, business.working_hours)
    )
    services_done = _has(
        db,
        select(Service.id).where(Service.business_id == ctx.business_id, Service.active.is_(True)),
    )
    # Этап 9: подойдёт любой подключённый канал — Telegram-бот или сообщество VK.
    channel_done = _has(
        db,
        select(Integration.id).where(
            Integration.business_id == ctx.business_id,
            Integration.status == IntegrationStatus.ACTIVE,
        ),
    )
    # Запись к мастерам (вне ТЗ, §22): AI сам называет свободное время и записывает,
    # только когда есть активный мастер со сменой на сегодня или позже и запись включена.
    today = datetime.now(schedule_service.business_tz(business)).date()
    has_master = _has(
        db,
        select(Master.id).where(Master.business_id == ctx.business_id, Master.active.is_(True)),
    )
    has_shift = has_master and _has(
        db,
        select(MasterShift.id)
        .join(Master, Master.id == MasterShift.master_id)
        .where(
            Master.business_id == ctx.business_id,
            Master.active.is_(True),
            MasterShift.day >= today,
        ),
    )
    booking_done = bool(business.booking_enabled) and has_shift
    # Кнопка ведёт к шагу, на котором владелец остановился.
    if not has_master:
        booking_link, booking_text = f"{base}/team", "К сотрудникам"
    elif not has_shift:
        booking_link, booking_text = f"{base}/schedule", "К расписанию"
    else:
        booking_link, booking_text = f"{base}/settings", "К настройкам"
    first_message_done = _has(
        db, select(Conversation.id).where(Conversation.business_id == ctx.business_id)
    )
    return [
        OnboardingStep(
            "profile",
            "Заполните адрес, телефон и график работы",
            "Ассистент отвечает на вопросы «где вы» и «когда открыты» только по этим данным.",
            f"{base}/settings",
            "К настройкам",
            profile_done,
        ),
        OnboardingStep(
            "services",
            "Добавьте услуги и цены",
            "Ассистент называет цены только из вашего прайса.",
            f"{base}/services",
            "К услугам",
            services_done,
        ),
        OnboardingStep(
            "booking",
            "Включите запись к мастерам",
            "Добавьте мастеров и их смены, затем включите в настройках «Ассистент записывает "
            "клиентов по расписанию» — тогда ассистент сам предложит свободное время и запишет клиента.",
            booking_link,
            booking_text,
            booking_done,
        ),
        OnboardingStep(
            "channel",
            "Подключите Telegram-бота или сообщество VK",
            "Через них клиенты будут писать вам.",
            f"{base}/settings",
            "К настройкам",
            channel_done,
        ),
        OnboardingStep(
            "first_message",
            "Напишите боту или сообществу как клиент",
            "Шаг выполнен, когда первое сообщение появится в разделе «Сообщения».",
            f"{base}/messages",
            "К сообщениям",
            first_message_done,
        ),
    ]
