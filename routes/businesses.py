"""
Маршруты компаний, услуг и сотрудников — раздел 11 ТЗ:
    POST   /businesses
    GET    /businesses/{business_id}
    PUT    /businesses/{business_id}
    POST   /businesses/{business_id}/services
    GET    /businesses/{business_id}/services
    PUT    /services/{service_id}
    DELETE /services/{service_id}
    POST   /businesses/{business_id}/members   (раздел 6.1, вне минимального списка)
    GET    /businesses/{business_id}/members

Каждый маршрут, работающий с данными компании, получает BusinessContext через
Depends(require_business_roles(...)) / Depends(require_service_access(...)):
доступ и роль проверены до входа в обработчик (разделы 5 и 16 ТЗ).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from ai.context import HistoryTurn
from config import settings
from database import get_db
from models import MemberRole, Service, User
from schemas import (
    AIPreviewRequest,
    AIPreviewResponse,
    AnalyticsOut,
    BusinessCreate,
    BusinessMemberCreate,
    BusinessMemberOut,
    BusinessOut,
    BusinessUpdate,
    ServiceCreate,
    ServiceOut,
    ServiceUpdate,
)
from services import (
    ai_service,
    analytics_service,
    business_service,
    rate_limit_service,
    subscription_service,
)
from services.access_service import (
    BusinessContext,
    get_current_user,
    require_business_roles,
    require_service_access,
)

router = APIRouter(tags=["businesses"])

# Роли, допустимые для операций (раздел 5):
# OWNER — настройки бизнеса и услуги; MANAGER — только чтение прайса,
# без финансовых и системных настроек.
ANY_MEMBER = (MemberRole.OWNER, MemberRole.MANAGER)
OWNER_ONLY = (MemberRole.OWNER,)


# --------------------------------------------------------------------------- #
# Компания
# --------------------------------------------------------------------------- #
@router.post("/businesses", response_model=BusinessOut, status_code=status.HTTP_201_CREATED)
def create_business(
    payload: BusinessCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Создание компании (раздел 6.2). Создатель становится её OWNER."""
    return business_service.create_business(db, user, payload)


@router.get("/businesses/{business_id}", response_model=BusinessOut)
def get_business(ctx: BusinessContext = Depends(require_business_roles(*ANY_MEMBER))):
    return ctx.business


@router.put("/businesses/{business_id}", response_model=BusinessOut)
def update_business(
    payload: BusinessUpdate,
    ctx: BusinessContext = Depends(require_business_roles(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    return business_service.update_business(db, ctx, payload)


# --------------------------------------------------------------------------- #
# Услуги (раздел 6.3)
# --------------------------------------------------------------------------- #
@router.get("/businesses/{business_id}/services", response_model=list[ServiceOut])
def list_services(
    active: bool | None = Query(default=None, description="Фильтр по статусу услуги"),
    ctx: BusinessContext = Depends(require_business_roles(*ANY_MEMBER)),
    db: Session = Depends(get_db),
):
    return business_service.list_services(db, ctx, active=active)


@router.post(
    "/businesses/{business_id}/services",
    response_model=ServiceOut,
    status_code=status.HTTP_201_CREATED,
)
def create_service(
    payload: ServiceCreate,
    ctx: BusinessContext = Depends(require_business_roles(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    return business_service.create_service(db, ctx, payload)


@router.put("/services/{service_id}", response_model=ServiceOut)
def update_service(
    payload: ServiceUpdate,
    resolved: tuple[Service, BusinessContext] = Depends(require_service_access(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    """business_id берётся из самой услуги и проверяется на принадлежность
    пользователю — иначе можно было бы изменить чужой прайс."""
    service, ctx = resolved
    return business_service.update_service(db, ctx, service, payload)


@router.delete("/services/{service_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_service(
    resolved: tuple[Service, BusinessContext] = Depends(require_service_access(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    service, ctx = resolved
    business_service.delete_service(db, ctx, service)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- #
# Сотрудники компании (раздел 6.1)
# --------------------------------------------------------------------------- #
@router.get("/businesses/{business_id}/members", response_model=list[BusinessMemberOut])
def list_members(
    ctx: BusinessContext = Depends(require_business_roles(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    return [
        BusinessMemberOut(
            id=member.id,
            business_id=member.business_id,
            user_id=member.user_id,
            email=user.email,
            role=member.role,
            created_at=member.created_at,
        )
        for member, user in business_service.list_members(db, ctx)
    ]


@router.post(
    "/businesses/{business_id}/members",
    response_model=BusinessMemberOut,
    status_code=status.HTTP_201_CREATED,
)
def add_member(
    payload: BusinessMemberCreate,
    ctx: BusinessContext = Depends(require_business_roles(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    """Привязка зарегистрированного пользователя к компании с ролью
    OWNER/MANAGER. Приглашения по email — этап 5."""
    member, user = business_service.add_member(db, ctx, payload)
    return BusinessMemberOut(
        id=member.id,
        business_id=member.business_id,
        user_id=member.user_id,
        email=user.email,
        role=member.role,
        created_at=member.created_at,
    )


# --------------------------------------------------------------------------- #
# Диагностика AI (этап 2)
#
# Endpoint'а нет в минимальном списке раздела 11 ТЗ: он нужен, чтобы владелец
# мог проверить правила AI и прайс до подключения Telegram (этап 3). Выключен
# по умолчанию (AI_PREVIEW_ENABLED=false) и при выключенном флаге отвечает 404,
# не раскрывая своего существования. Сообщения в БД не сохраняет — таблицы
# conversations/messages появятся на этапах 3–4.
# --------------------------------------------------------------------------- #
@router.post("/businesses/{business_id}/ai/preview", response_model=AIPreviewResponse)
def ai_preview(
    payload: AIPreviewRequest,
    ctx: BusinessContext = Depends(require_business_roles(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    if not settings.ai_preview_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    # Этап 8: по истечении срока подписки AI не работает, в том числе проверка ответов.
    if not subscription_service.ai_allowed(db, ctx.business_id):
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Срок подписки истёк: проверка ответов AI недоступна до продления",
        )
    # Каждая проверка — платный запрос к LLM: ограничиваем частоту на пользователя.
    if settings.rate_limit_enabled and not rate_limit_service.hit(
        "ai:preview",
        str(ctx.user.id),
        limit=settings.ai_preview_rate_limit_per_minute,
        window_seconds=60,
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Слишком много проверок, повторите через минуту",
            headers={"Retry-After": "60"},
        )

    history = [HistoryTurn(role=turn.role, text=turn.text) for turn in payload.history]
    result = ai_service.process_message(
        db,
        ctx.business,
        payload.text,
        history=history,
        actor_user_id=ctx.user.id,
    )

    return AIPreviewResponse(
        decision=result.decision,
        reply=result.reply_text,
        intent=result.classification.intent,
        priority=result.classification.priority,
        needs_manager=result.classification.needs_manager,
        reason=result.classification.reason,
        classification_source=result.classification.source,
        escalation_reason=result.escalation_reason,
        escalation_detail=result.escalation_detail,
        validation=result.validation.as_dict() if result.validation else None,
        model=result.response.model if result.response else None,
        prompt_version=result.response.prompt_version if result.response else None,
        latency_ms=result.latency_ms,
    )


# --------------------------------------------------------------------------- #
# Аналитика (раздел 13: «Аналитика — базовые показатели за выбранный период»)
# Вне минимального списка раздела 11. Только OWNER: показатели работы компании
# относятся к настройкам владельца (раздел 5).
# --------------------------------------------------------------------------- #
@router.get("/businesses/{business_id}/analytics", response_model=AnalyticsOut)
def get_analytics(
    date_from: datetime | None = Query(default=None, description="Начало периода (UTC)"),
    date_to: datetime | None = Query(default=None, description="Конец периода (UTC)"),
    ctx: BusinessContext = Depends(require_business_roles(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    start, end = analytics_service.resolve_period(date_from, date_to)
    return analytics_service.period_summary(db, ctx, start, end)
