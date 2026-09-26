"""
Onboarding нового владельца — этап 8 «SaaS-автоматизация» (раздел 20 ТЗ).

Чек-лист на дашборде: каждый шаг отмечается по фактическим данным компании,
а не по нажатию кнопки, поэтому чек-лист не «врёт» и исчезает сам, когда
ассистент готов к работе. Данные только своей компании (BusinessContext, §8).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Conversation, Integration, IntegrationStatus, Service
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
