"""
Диалоги и сообщения — раздел 11 ТЗ:
    POST /webhooks/telegram
    GET  /businesses/{business_id}/conversations
    GET  /conversations/{conversation_id}

Ручной ответ POST /conversations/{id}/reply, лиды и статусы диалога — этап 4.

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
from models import Conversation, ConversationStatus, LeadPriority, LogLevel, MemberRole
from schemas import (
    AiDecisionOut,
    ConversationDetail,
    ConversationListItem,
    ConversationOut,
    CustomerOut,
    MessageOut,
)
from services import audit_service, integration_service, message_service, rate_limit_service
from services.access_service import (
    BusinessContext,
    require_business_roles,
    require_conversation_access,
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
        )
        for conversation, customer, last in rows
    ]


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    resolved: tuple[Conversation, BusinessContext] = Depends(
        require_conversation_access(*ANY_MEMBER)
    ),
    db: Session = Depends(get_db),
):
    conversation, ctx = resolved
    customer, messages, decisions = message_service.get_conversation_detail(db, ctx, conversation)
    return ConversationDetail(
        conversation=ConversationOut.model_validate(conversation),
        customer=CustomerOut.model_validate(customer),
        messages=[MessageOut.model_validate(m) for m in messages],
        ai_decisions=[AiDecisionOut.model_validate(d) for d in decisions],
    )
