"""
Интеграции каналов компании (раздел 13: «Настройки — данные бизнеса и интеграции»).

    GET    /businesses/{business_id}/integrations
    POST   /businesses/{business_id}/integrations/telegram
    DELETE /businesses/{business_id}/integrations/telegram

ВНЕ минимального списка раздела 11 ТЗ: эти маршруты нужны для критерия
приёмки 2 («Компания может подключить Telegram-бота»). Только OWNER —
интеграции относятся к системным настройкам, недоступным MANAGER (раздел 5).
Ответы не содержат токена бота и секрета webhook (раздел 16).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from database import get_db
from models import Integration, MemberRole
from schemas import IntegrationOut, TelegramConnectRequest
from services import integration_service
from services.access_service import BusinessContext, require_business_roles

router = APIRouter(tags=["integrations"])

OWNER_ONLY = (MemberRole.OWNER,)


def _to_out(integration: Integration) -> IntegrationOut:
    return IntegrationOut(
        id=integration.id,
        business_id=integration.business_id,
        channel=integration.channel,
        status=integration.status,
        bot_username=integration.external_account_name,
        webhook_url=integration_service.webhook_url(),
        last_error=integration.last_error,
        created_at=integration.created_at,
        updated_at=integration.updated_at,
    )


@router.get("/businesses/{business_id}/integrations", response_model=list[IntegrationOut])
def list_integrations(
    ctx: BusinessContext = Depends(require_business_roles(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    return [_to_out(item) for item in integration_service.list_integrations(db, ctx)]


@router.post(
    "/businesses/{business_id}/integrations/telegram",
    response_model=IntegrationOut,
    status_code=status.HTTP_201_CREATED,
)
def connect_telegram(
    payload: TelegramConnectRequest,
    ctx: BusinessContext = Depends(require_business_roles(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    """Подключить бота: проверка токена, сохранение и регистрация webhook."""
    integration = integration_service.connect_telegram(
        db, ctx, payload.bot_token.get_secret_value()
    )
    return _to_out(integration)


@router.delete(
    "/businesses/{business_id}/integrations/telegram", status_code=status.HTTP_204_NO_CONTENT
)
def disconnect_telegram(
    ctx: BusinessContext = Depends(require_business_roles(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    integration_service.disconnect_telegram(db, ctx)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
