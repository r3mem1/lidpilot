"""
Базовая аналитика компании — раздел 13 ТЗ («Аналитика: базовые показатели за
выбранный период», «Обзор»). Расширенная сквозная аналитика в MVP не входит (раздел 19).

Границы периода и разбивка по дням — в часовом поясе компании (businesses.timezone):
«сегодня» в отчёте совпадает с «сегодня» владельца. Все запросы фильтруются по business_id.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, tzinfo

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import (
    AiResponse,
    AiResponseStatus,
    Conversation,
    ConversationStatus,
    Customer,
    DeliveryStatus,
    Lead,
    LeadPriority,
    LeadStatus,
    Message,
    SenderType,
    utcnow,
)
from services.access_service import BusinessContext

MAX_PERIOD_DAYS = 366
DEFAULT_PERIOD_DAYS = 30


def resolve_period(
    date_from: datetime | None, date_to: datetime | None
) -> tuple[datetime, datetime]:
    """Период по умолчанию — последние 30 суток; максимум — 366 суток."""
    end = date_to or utcnow()
    start = date_from or (end - timedelta(days=DEFAULT_PERIOD_DAYS))
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    if start > end:
        raise HTTPException(status_code=422, detail="Начало периода не может быть позже его конца")
    if end - start > timedelta(days=MAX_PERIOD_DAYS):
        raise HTTPException(
            status_code=422, detail=f"Период не может быть длиннее {MAX_PERIOD_DAYS} суток"
        )
    return start, end


def _count(db: Session, stmt) -> int:
    return int(db.scalar(stmt) or 0)


def period_summary(
    db: Session, ctx: BusinessContext, start: datetime, end: datetime, tz: tzinfo = UTC
) -> dict:
    """Показатели за период. Каждая цифра считается отдельным простым запросом."""
    bid = ctx.business_id
    in_period_msg = (
        Message.business_id == bid,
        Message.created_at >= start,
        Message.created_at <= end,
    )

    conversations_new = _count(
        db,
        select(func.count(Conversation.id)).where(
            Conversation.business_id == bid,
            Conversation.created_at >= start,
            Conversation.created_at <= end,
        ),
    )
    messages_incoming = _count(
        db,
        select(func.count(Message.id)).where(
            *in_period_msg, Message.sender_type == SenderType.CUSTOMER
        ),
    )
    customers_active = _count(
        db,
        select(func.count(func.distinct(Conversation.customer_id)))
        .join(Message, Message.conversation_id == Conversation.id)
        .where(*in_period_msg, Message.sender_type == SenderType.CUSTOMER),
    )
    manager_replies = _count(
        db,
        select(func.count(Message.id)).where(
            *in_period_msg, Message.sender_type == SenderType.MANAGER
        ),
    )

    ai_window = (
        AiResponse.business_id == bid,
        AiResponse.created_at >= start,
        AiResponse.created_at <= end,
    )
    ai_answered = _count(
        db,
        select(func.count(AiResponse.id)).where(*ai_window, AiResponse.decision == "SEND"),
    )
    ai_escalated = _count(
        db,
        select(func.count(AiResponse.id)).where(*ai_window, AiResponse.decision == "ESCALATE"),
    )
    ai_total = ai_answered + ai_escalated
    latency = db.scalar(
        select(func.avg(AiResponse.latency_ms)).where(*ai_window, AiResponse.model.is_not(None))
    )
    delivery_failed = _count(
        db,
        select(func.count(Message.id)).where(
            *in_period_msg, Message.delivery_status == DeliveryStatus.FAILED
        ),
    )
    ai_errors = _count(
        db,
        select(func.count(AiResponse.id)).where(
            *ai_window, AiResponse.escalation_reason == "EXTERNAL_API_ERROR"
        ),
    )

    lead_window = (Lead.business_id == bid, Lead.created_at >= start, Lead.created_at <= end)
    leads_by_priority = {
        priority.value: _count(
            db, select(func.count(Lead.id)).where(*lead_window, Lead.priority == priority)
        )
        for priority in LeadPriority
    }
    leads_by_status = {
        lead_status.value: _count(
            db, select(func.count(Lead.id)).where(*lead_window, Lead.status == lead_status)
        )
        for lead_status in LeadStatus
    }

    intent_rows = db.execute(
        select(Message.intent, func.count(Message.id))
        .where(*in_period_msg, Message.sender_type == SenderType.CUSTOMER)
        .group_by(Message.intent)
    ).all()
    intents = {(intent or "UNKNOWN"): int(count) for intent, count in intent_rows}

    return {
        "period_start": start,
        "period_end": end,
        "conversations_new": conversations_new,
        "messages_incoming": messages_incoming,
        "customers_active": customers_active,
        "manager_replies": manager_replies,
        "ai_answered": ai_answered,
        "ai_escalated": ai_escalated,
        "ai_share_percent": round(ai_answered * 100 / ai_total) if ai_total else None,
        "ai_avg_latency_ms": int(latency) if latency is not None else None,
        "ai_errors": ai_errors,
        "delivery_failed": delivery_failed,
        "leads_total": sum(leads_by_priority.values()),
        "leads_by_priority": leads_by_priority,
        "leads_by_status": leads_by_status,
        "intents": intents,
        "daily": _daily_series(db, bid, start, end, tz),
    }


def _daily_series(
    db: Session, business_id: int, start: datetime, end: datetime, tz: tzinfo = UTC
) -> list[dict]:
    """Сообщения по суткам компании: входящие, ответы AI и ответы менеджеров.
    Сутки считаются в поясе компании в Python: date() в SQL дал бы сутки UTC,
    а перевод пояса в SQLite и PostgreSQL устроен по-разному."""
    rows = db.execute(
        select(Message.created_at, Message.sender_type).where(
            Message.business_id == business_id,
            Message.created_at >= start,
            Message.created_at <= end,
        )
    ).all()
    by_day: dict[str, dict[str, int]] = {}
    for created, sender in rows:
        moment = created if created.tzinfo else created.replace(tzinfo=UTC)
        bucket = by_day.setdefault(moment.astimezone(tz).date().isoformat(), {})
        key = sender.value if hasattr(sender, "value") else str(sender)
        bucket[key] = bucket.get(key, 0) + 1

    series = []
    cursor: date = start.astimezone(tz).date()
    last: date = end.astimezone(tz).date()
    while cursor <= last:
        bucket = by_day.get(cursor.isoformat(), {})
        series.append(
            {
                "date": cursor.isoformat(),
                "incoming": bucket.get("CUSTOMER", 0),
                "ai": bucket.get("AI", 0),
                "manager": bucket.get("MANAGER", 0),
            }
        )
        cursor += timedelta(days=1)
    return series


def dashboard_summary(db: Session, ctx: BusinessContext) -> dict:
    """Цифры для «Обзора» (раздел 13): обращения, обработано AI, горячие лиды,
    обращения, требующие внимания. Окна скользящие — 24 часа и 7 суток."""
    bid = ctx.business_id
    now = utcnow()
    day_ago, week_ago = now - timedelta(days=1), now - timedelta(days=7)

    def conversations_since(moment: datetime) -> int:
        return _count(
            db,
            select(func.count(Conversation.id)).where(
                Conversation.business_id == bid, Conversation.created_at >= moment
            ),
        )

    week_answered = _count(
        db,
        select(func.count(AiResponse.id)).where(
            AiResponse.business_id == bid,
            AiResponse.created_at >= week_ago,
            AiResponse.decision == "SEND",
            AiResponse.status == AiResponseStatus.SENT,
        ),
    )
    week_ai_total = _count(
        db,
        select(func.count(AiResponse.id)).where(
            AiResponse.business_id == bid, AiResponse.created_at >= week_ago
        ),
    )
    return {
        "conversations_24h": conversations_since(day_ago),
        "conversations_7d": conversations_since(week_ago),
        "ai_answered_7d": week_answered,
        "ai_share_percent_7d": round(week_answered * 100 / week_ai_total)
        if week_ai_total
        else None,
        "hot_leads_open": _count(
            db,
            select(func.count(Lead.id)).where(
                Lead.business_id == bid,
                Lead.priority == LeadPriority.HOT,
                Lead.status.in_([LeadStatus.NEW, LeadStatus.IN_PROGRESS]),
            ),
        ),
        "needs_attention": _count(
            db,
            select(func.count(Conversation.id)).where(
                Conversation.business_id == bid,
                Conversation.status == ConversationStatus.NEEDS_ATTENTION,
            ),
        ),
        "customers_total": _count(
            db, select(func.count(Customer.id)).where(Customer.business_id == bid)
        ),
    }


def attention_count(db: Session, business_id: int) -> int:
    """Число диалогов, требующих внимания, — для счётчика в меню кабинета."""
    return _count(
        db,
        select(func.count(Conversation.id)).where(
            Conversation.business_id == business_id,
            Conversation.status == ConversationStatus.NEEDS_ATTENTION,
        ),
    )


def conversation_counts(db: Session, business_id: int) -> dict[str, int]:
    """Число диалогов по статусам — для вкладок списка сообщений."""
    counts = {status.value: 0 for status in ConversationStatus}
    rows = db.execute(
        select(Conversation.status, func.count(Conversation.id))
        .where(Conversation.business_id == business_id)
        .group_by(Conversation.status)
    ).all()
    for conversation_status, count in rows:
        counts[conversation_status.value] = int(count)
    counts["ALL"] = sum(counts.values())
    return counts
