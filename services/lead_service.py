"""
Лиды: создание по классификации, статусы, приоритет, назначение менеджера —
разделы 6.5, 10, 11 и 14 ТЗ.

Правила (ТЗ их не фиксирует, приняты для MVP):
* один лид на диалог; спам лидом не считается;
* приоритет лида внутри открытого диалога только повышается: «горячая» запись
  не должна остыть из-за следующего сообщения «спасибо» (раздел 6.5);
* причина классификации хранится в лиде (раздел 6.5: «с сохранением причины»);
* закрытие лида (RESOLVED/LOST) закрывает диалог; переоткрытие лида
  переоткрывает диалог;
* ответственным можно назначить только участника ЭТОЙ компании (раздел 16).

Все выборки фильтруются по business_id из проверенного BusinessContext.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from models import (
    BusinessMember,
    Conversation,
    ConversationStatus,
    Customer,
    Lead,
    LeadPriority,
    LeadStatus,
    utcnow,
)
from services import audit_service
from services.access_service import BusinessContext

_PRIORITY_RANK = {LeadPriority.COLD: 0, LeadPriority.WARM: 1, LeadPriority.HOT: 2}


def max_priority(current: LeadPriority, new: LeadPriority) -> LeadPriority:
    """Более высокий из двух приоритетов."""
    return new if _PRIORITY_RANK[new] > _PRIORITY_RANK[current] else current


# --------------------------------------------------------------------------- #
# Создание и обновление по результату AI (вызывается из message_service)
# --------------------------------------------------------------------------- #
def register_classification(
    db: Session,
    conversation: Conversation,
    *,
    intent: str,
    priority: LeadPriority,
    reason: str,
    message_id: int,
) -> Lead | None:
    """Создать лид диалога либо обновить его по новой классификации.

    Работает в транзакции вызывающего кода (commit делает он). Возвращает лид
    или None, если сообщение — спам и лида ещё нет.
    """
    lead = db.scalar(select(Lead).where(Lead.conversation_id == conversation.id))

    if lead is None:
        if intent == "SPAM":
            return None
        lead = Lead(
            business_id=conversation.business_id,
            conversation_id=conversation.id,
            status=LeadStatus.NEW,
            priority=priority,
            intent=intent,
            reason=reason,
        )
        db.add(lead)
        db.flush()
        audit_service.log_event(
            db,
            event_type=audit_service.EventType.LEAD_CREATED,
            message=f"Создан лид ({priority.value}, {intent})",
            business_id=conversation.business_id,
            payload={
                "lead_id": lead.id,
                "conversation_id": conversation.id,
                "message_id": message_id,
                "priority": priority.value,
                "intent": intent,
                "reason": reason,
            },
        )
        return lead

    if _PRIORITY_RANK[priority] > _PRIORITY_RANK[lead.priority]:
        # Приоритет вырос — причина обновляется вместе с ним.
        lead.priority = priority
        lead.intent = intent
        lead.reason = reason
    return lead


# --------------------------------------------------------------------------- #
# Смена состояния диалога и лида
# --------------------------------------------------------------------------- #
def close_conversation(conversation: Conversation, lead: Lead | None) -> None:
    """«Решено» (раздел 14): диалог закрыт, AI снова может отвечать новым обращениям."""
    conversation.status = ConversationStatus.RESOLVED
    conversation.attention_reason = None
    conversation.handled_by_manager = False
    if lead is not None and not lead.status.is_closed:
        lead.status = LeadStatus.RESOLVED


def reopen_conversation(conversation: Conversation, lead: Lead | None) -> None:
    """Менеджер вернулся к закрытому диалогу."""
    if conversation.status is ConversationStatus.RESOLVED:
        conversation.status = ConversationStatus.OPEN
        conversation.attention_reason = None
    if lead is not None and lead.status.is_closed:
        lead.status = LeadStatus.IN_PROGRESS


def get_lead_for_conversation(db: Session, conversation_id: int) -> Lead | None:
    return db.scalar(select(Lead).where(Lead.conversation_id == conversation_id))


def assign_if_unassigned(lead: Lead | None, ctx: BusinessContext) -> None:
    """Первый ответивший менеджер становится ответственным (раздел 14)."""
    if lead is None:
        return
    if not lead.status.is_closed and lead.status is LeadStatus.NEW:
        lead.status = LeadStatus.IN_PROGRESS
    if lead.assigned_to is None and ctx.role is not None:
        lead.assigned_to = ctx.user.id


# --------------------------------------------------------------------------- #
# API: список и изменение
# --------------------------------------------------------------------------- #
def list_leads(
    db: Session,
    ctx: BusinessContext,
    *,
    priority: LeadPriority | None = None,
    lead_status: LeadStatus | None = None,
    assigned_to: int | None = None,
    unassigned: bool = False,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[tuple[Lead, Conversation, Customer]]:
    """Лиды компании (раздел 13: фильтры HOT/WARM/COLD, статус, ответственный).

    Сортировка: сначала горячие, внутри приоритета — самые свежие.
    """
    stmt = (
        select(Lead, Conversation, Customer)
        .join(Conversation, Conversation.id == Lead.conversation_id)
        .join(Customer, Customer.id == Conversation.customer_id)
        .where(Lead.business_id == ctx.business_id)
    )
    if priority is not None:
        stmt = stmt.where(Lead.priority == priority)
    if lead_status is not None:
        stmt = stmt.where(Lead.status == lead_status)
    if unassigned:
        stmt = stmt.where(Lead.assigned_to.is_(None))
    elif assigned_to is not None:
        stmt = stmt.where(Lead.assigned_to == assigned_to)
    if date_from is not None:
        stmt = stmt.where(Lead.created_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(Lead.created_at <= date_to)

    rank = case(
        (Lead.priority == LeadPriority.HOT, 0),
        (Lead.priority == LeadPriority.WARM, 1),
        else_=2,
    )
    rows = db.execute(
        stmt.order_by(rank, Lead.created_at.desc(), Lead.id.desc()).limit(limit).offset(offset)
    ).all()
    return [(lead, conversation, customer) for lead, conversation, customer in rows]


def update_lead(db: Session, ctx: BusinessContext, lead: Lead, changes: dict) -> Lead:
    """Статус и ответственный (раздел 14). lead уже проверен зависимостью доступа."""
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Нет полей для изменения"
        )

    conversation = db.get(Conversation, lead.conversation_id)
    if conversation is None or conversation.business_id != ctx.business_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Лид не найден")

    audit_payload: dict = {"lead_id": lead.id, "conversation_id": conversation.id}

    if "assigned_to" in changes:
        user_id = changes["assigned_to"]
        if user_id is not None:
            member = db.scalar(
                select(BusinessMember).where(
                    BusinessMember.business_id == ctx.business_id,
                    BusinessMember.user_id == user_id,
                )
            )
            if member is None:
                raise HTTPException(
                    status_code=422,
                    detail="Ответственным можно назначить только сотрудника этой компании",
                )
        audit_payload["assigned_to"] = {"from": lead.assigned_to, "to": user_id}
        lead.assigned_to = user_id

    if "status" in changes:
        new_status: LeadStatus = changes["status"]
        audit_payload["status"] = {"from": lead.status.value, "to": new_status.value}
        lead.status = new_status
        if new_status.is_closed:
            conversation.status = ConversationStatus.RESOLVED
            conversation.attention_reason = None
            conversation.handled_by_manager = False
        else:
            reopen_conversation(conversation, lead)

    lead.updated_at = utcnow()
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.LEAD_UPDATED,
        message="Изменён лид",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload=audit_payload,
    )
    db.commit()
    db.refresh(lead)
    return lead


def resolve_conversation(
    db: Session, ctx: BusinessContext, conversation: Conversation
) -> tuple[Conversation, Lead | None]:
    """«Решено» (раздел 14). Повторный вызов безопасен."""
    lead = get_lead_for_conversation(db, conversation.id)
    already = conversation.status is ConversationStatus.RESOLVED
    close_conversation(conversation, lead)
    if not already:
        audit_service.log_event(
            db,
            event_type=audit_service.EventType.CONVERSATION_RESOLVED,
            message="Диалог отмечен как решённый",
            business_id=ctx.business_id,
            actor_user_id=ctx.user.id,
            payload={
                "conversation_id": conversation.id,
                "lead_id": lead.id if lead else None,
            },
        )
    db.commit()
    db.refresh(conversation)
    return conversation, lead
