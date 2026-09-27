"""
Заявки на запись без брони — вне ТЗ (§22), решение заказчика 2026-09-28.

AI понял просьбу о записи («на стрижку завтра на 15»), но сам забронировать не
может: расписание не подключено или свободных окон нет. Заявка сохраняется и
видна владельцу и менеджеру в «Записях»; по ней сотрудник создаёт запись
(клиенту уходит «Готово, вы записаны») или закрывает её.

Одна открытая заявка на диалог — уточнения клиента обновляют её. Доступ только
в пределах своей компании (BusinessContext): чужая заявка — 404 (раздел 16).
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai.booking import RequestDraft
from models import (
    BookingRequest,
    BookingRequestStatus,
    Conversation,
    ConversationStatus,
    LogLevel,
    Master,
    Service,
    utcnow,
)
from services import audit_service
from services.access_service import BusinessContext


def save_from_ai(
    db: Session,
    *,
    business_id: int,
    conversation: Conversation,
    client_name: str,
    draft: RequestDraft,
    text: str | None,
    message_id: int,
) -> BookingRequest:
    """Создать или обновить открытую заявку диалога по разбору AI. Названия услуги
    и мастера сопоставляются только с данными этой компании. Без commit."""
    request = db.scalar(
        select(BookingRequest).where(
            BookingRequest.business_id == business_id,
            BookingRequest.conversation_id == conversation.id,
            BookingRequest.status == BookingRequestStatus.OPEN,
        )
    )
    created = request is None
    if request is None:
        request = BookingRequest(
            business_id=business_id,
            conversation_id=conversation.id,
            customer_id=conversation.customer_id,
            client_name=client_name[:255] or "Клиент",
        )
        db.add(request)
    # Сопоставление без учёта регистра — в Python: lower() SQLite не знает кириллицу.
    if draft.service:
        wanted = draft.service.casefold()
        request.service_id = next(
            (
                sid
                for sid, name in db.execute(
                    select(Service.id, Service.name).where(Service.business_id == business_id)
                )
                if name.casefold() == wanted
            ),
            request.service_id,
        )
    if draft.master:
        wanted = draft.master.casefold()
        request.master_id = next(
            (
                mid
                for mid, name in db.execute(
                    select(Master.id, Master.display_name).where(Master.business_id == business_id)
                )
                if name.casefold() == wanted
            ),
            request.master_id,
        )
    request.desired_day = draft.day or request.desired_day
    request.desired_time = draft.at or request.desired_time
    request.part_of_day = draft.part_of_day or request.part_of_day
    if text:
        request.last_text = text[:1000]
    request.updated_at = utcnow()
    db.flush()
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.BOOKING_REQUEST_SAVED,
        message="Заявка на запись " + ("создана" if created else "уточнена") + " по сообщению",
        business_id=business_id,
        payload={
            "booking_request_id": request.id,
            "conversation_id": conversation.id,
            "message_id": message_id,
            "draft": draft.as_dict(),
        },
    )
    return request


def list_open(db: Session, ctx: BusinessContext) -> list[BookingRequest]:
    return list(
        db.scalars(
            select(BookingRequest)
            .where(
                BookingRequest.business_id == ctx.business_id,
                BookingRequest.status == BookingRequestStatus.OPEN,
            )
            .order_by(BookingRequest.created_at.desc(), BookingRequest.id.desc())
        )
    )


def count_open(db: Session, business_id: int) -> int:
    return (
        db.scalar(
            select(func.count(BookingRequest.id)).where(
                BookingRequest.business_id == business_id,
                BookingRequest.status == BookingRequestStatus.OPEN,
            )
        )
        or 0
    )


def get_open(db: Session, ctx: BusinessContext, request_id: int) -> BookingRequest:
    """Открытая заявка своей компании; чужая или несуществующая — 404, закрытая — 409."""
    request = db.get(BookingRequest, request_id)
    if request is None or request.business_id != ctx.business_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Заявка не найдена")
    if request.status is not BookingRequestStatus.OPEN:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Заявка уже обработана")
    return request


def close(db: Session, ctx: BusinessContext, request: BookingRequest) -> BookingRequest:
    """Закрыть без записи (договорились иначе). Клиенту ничего не пишется."""
    request.status = BookingRequestStatus.CLOSED
    request.closed_by_user_id = ctx.user.id
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.BOOKING_REQUEST_CLOSED,
        message=f"Заявка на запись #{request.id} закрыта без записи",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={"booking_request_id": request.id, "conversation_id": request.conversation_id},
    )
    db.commit()
    db.refresh(request)
    return request


def mark_done(db: Session, ctx: BusinessContext, request: BookingRequest, booking_id: int) -> None:
    """По заявке создана запись: заявка выполнена, диалог больше не ждёт внимания
    из-за записи (клиенту уже ушло «Готово, вы записаны»)."""
    request.status = BookingRequestStatus.DONE
    request.booking_id = booking_id
    request.closed_by_user_id = ctx.user.id
    if request.conversation_id is not None:
        conversation = db.get(Conversation, request.conversation_id)
        if conversation is not None and conversation.attention_reason == "HOT_LEAD_CONFIRMATION":
            conversation.status = ConversationStatus.OPEN
            conversation.attention_reason = None
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.BOOKING_REQUEST_DONE,
        message=f"По заявке #{request.id} создана запись #{booking_id}",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        level=LogLevel.INFO,
        payload={"booking_request_id": request.id, "booking_id": booking_id},
    )
    db.commit()
