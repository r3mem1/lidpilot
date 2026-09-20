"""
Диалоги и сообщения — раздел 11 ТЗ:
    POST /webhooks/telegram
    GET  /businesses/{business_id}/conversations
    GET  /conversations/{conversation_id}
    POST /conversations/{conversation_id}/reply
    POST /conversations/{conversation_id}/resolve   (вне §11, раздел 14)
    GET  /businesses/{business_id}/customers        (вне §11, раздел 13)
    GET  /customers/{customer_id}                   (вне §11, раздел 13)

Этап 4 добавляет ручной ответ POST /conversations/{id}/reply (раздел 11), отметку
«решено» (раздел 14, вне §11) и клиентов с историей обращений (раздел 13, вне §11).

Webhook — единственный маршрут без пользовательской авторизации: подлинность
запроса подтверждает заголовок X-Telegram-Bot-Api-Secret-Token (раздел 11),
по нему же определяется компания. Тело запроса не содержит business_id и не
может его подменить (раздел 16).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    status,
)
from sqlalchemy.orm import Session

from database import get_db
from integrations import telegram
from models import (
    BusinessStatus,
    Conversation,
    ConversationStatus,
    Customer,
    LeadPriority,
    LogLevel,
    MemberRole,
)
from schemas import (
    AiDecisionOut,
    ConversationDetail,
    ConversationListItem,
    ConversationOut,
    ConversationState,
    CustomerDetail,
    CustomerListItem,
    CustomerOut,
    LeadOut,
    MessageOut,
    ReplyRequest,
)
from services import (
    audit_service,
    integration_service,
    lead_service,
    message_service,
    rate_limit_service,
)
from services.access_service import (
    BusinessContext,
    require_business_roles,
    require_conversation_access,
    require_customer_access,
)

router = APIRouter(tags=["messages"])

ANY_MEMBER = (MemberRole.OWNER, MemberRole.MANAGER)


# --------------------------------------------------------------------------- #
# Webhook Telegram (раздел 11, Приложение B)
# --------------------------------------------------------------------------- #
@router.post("/webhooks/telegram")
def telegram_webhook(
    request: Request,
    background: BackgroundTasks,
    update: dict = Body(...),
    secret_token: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
    db: Session = Depends(get_db),
) -> dict:
    """Принять Update. Отвечает быстро: сообщение сохраняется в БД, а AI и
    отправка ответа выполняются после ответа Telegram (фоновая задача).

    200 отдаётся только когда сообщение сохранено (или это дубликат/игнорируемый
    апдейт): при сбое БД Telegram получит 5xx и повторит доставку (раздел 18).
    """
    integration = integration_service.find_by_webhook_secret(db, secret_token)
    if integration is None:
        # Ограничение перебора секрета: превышение лимита — 429 без записи в БД,
        # чтобы поток мусорных запросов не забивал system_logs.
        rate_limit_service.enforce_webhook_rejection_limit(request)
        audit_service.log_event(
            db,
            event_type=audit_service.EventType.WEBHOOK_REJECTED,
            message="Webhook отклонён: неверный или отсутствующий secret_token",
            level=LogLevel.WARNING,
            payload={"ip": rate_limit_service.client_ip(request)},
            commit=True,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

    incoming, ignored_reason = telegram.parse_update(update)
    if incoming is None:
        audit_service.log_event(
            db,
            event_type=audit_service.EventType.WEBHOOK_IGNORED,
            message=f"Апдейт Telegram не обрабатывается: {ignored_reason}",
            business_id=integration.business_id,
            payload={"reason": ignored_reason, "update_id": update.get("update_id")},
            commit=True,
        )
        return {"ok": True, "ignored": ignored_reason}

    received = message_service.receive_incoming(db, integration, incoming)
    if received.duplicate:
        return {"ok": True, "duplicate": True}

    background.add_task(message_service.process_incoming_message, received.message_id)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Диалоги (раздел 6.4, 11)
# --------------------------------------------------------------------------- #
@router.get("/businesses/{business_id}/conversations", response_model=list[ConversationListItem])
def list_conversations(
    status_filter: ConversationStatus | None = Query(default=None, alias="status"),
    priority: LeadPriority | None = Query(default=None),
    date_from: datetime | None = Query(default=None, description="Обновлён не раньше (UTC)"),
    date_to: datetime | None = Query(default=None, description="Обновлён не позже (UTC)"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: BusinessContext = Depends(require_business_roles(*ANY_MEMBER)),
    db: Session = Depends(get_db),
):
    rows = message_service.list_conversations(
        db,
        ctx,
        status=status_filter,
        priority=priority,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return [
        ConversationListItem(
            conversation=ConversationOut.model_validate(conversation),
            customer=CustomerOut.model_validate(customer),
            last_message=MessageOut.model_validate(last) if last else None,
            lead=LeadOut.model_validate(lead) if lead else None,
        )
        for conversation, customer, last, lead in rows
    ]


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    resolved: tuple[Conversation, BusinessContext] = Depends(
        require_conversation_access(*ANY_MEMBER)
    ),
    db: Session = Depends(get_db),
):
    conversation, ctx = resolved
    customer, lead, messages, decisions = message_service.get_conversation_detail(
        db, ctx, conversation
    )
    return ConversationDetail(
        conversation=ConversationOut.model_validate(conversation),
        customer=CustomerOut.model_validate(customer),
        lead=LeadOut.model_validate(lead) if lead else None,
        messages=[MessageOut.model_validate(m) for m in messages],
        ai_decisions=[AiDecisionOut.model_validate(d) for d in decisions],
    )


# --------------------------------------------------------------------------- #
# Рабочее место менеджера (раздел 14)
# --------------------------------------------------------------------------- #
@router.post(
    "/conversations/{conversation_id}/reply",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
)
def reply_to_conversation(
    payload: ReplyRequest,
    resolved: tuple[Conversation, BusinessContext] = Depends(
        require_conversation_access(*ANY_MEMBER)
    ),
    db: Session = Depends(get_db),
):
    """Ручной ответ клиенту. 201 возвращается, когда сообщение СОХРАНЕНО;
    доставку показывает delivery_status (SENT / PENDING — повторится / FAILED)."""
    conversation, ctx = resolved
    if ctx.business.status is BusinessStatus.SUSPENDED and not ctx.is_platform_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Компания приостановлена: отправка сообщений недоступна",
        )
    message = message_service.send_manager_reply(db, ctx, conversation, payload.text)
    return MessageOut.model_validate(message)


@router.post("/conversations/{conversation_id}/resolve", response_model=ConversationState)
def resolve_conversation(
    resolved: tuple[Conversation, BusinessContext] = Depends(
        require_conversation_access(*ANY_MEMBER)
    ),
    db: Session = Depends(get_db),
):
    """Отметка «решено»: диалог и лид закрываются, AI снова отвечает на новые обращения."""
    conversation, ctx = resolved
    conversation, lead = lead_service.resolve_conversation(db, ctx, conversation)
    return ConversationState(
        conversation=ConversationOut.model_validate(conversation),
        lead=LeadOut.model_validate(lead) if lead else None,
    )


# --------------------------------------------------------------------------- #
# Клиенты и история обращений (раздел 13)
# --------------------------------------------------------------------------- #
@router.get("/businesses/{business_id}/customers", response_model=list[CustomerListItem])
def list_customers(
    search: str | None = Query(default=None, max_length=100, description="Имя или @username"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: BusinessContext = Depends(require_business_roles(*ANY_MEMBER)),
    db: Session = Depends(get_db),
):
    rows = message_service.list_customers(db, ctx, search=search, limit=limit, offset=offset)
    return [
        CustomerListItem(
            customer=CustomerOut.model_validate(customer),
            conversations_count=count,
            last_activity_at=last_activity,
        )
        for customer, count, last_activity in rows
    ]


@router.get("/customers/{customer_id}", response_model=CustomerDetail)
def get_customer(
    resolved: tuple[Customer, BusinessContext] = Depends(require_customer_access(*ANY_MEMBER)),
    db: Session = Depends(get_db),
):
    """Клиент и вся история его обращений."""
    customer, ctx = resolved
    history = message_service.get_customer_history(db, ctx, customer)
    return CustomerDetail(
        customer=CustomerOut.model_validate(customer),
        created_at=customer.created_at,
        conversations=[
            ConversationListItem(
                conversation=ConversationOut.model_validate(conversation),
                customer=CustomerOut.model_validate(owner),
                last_message=MessageOut.model_validate(last) if last else None,
                lead=LeadOut.model_validate(lead) if lead else None,
            )
            for conversation, owner, last, lead in history
        ],
    )
