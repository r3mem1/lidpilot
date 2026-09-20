"""
Лиды — разделы 6.5, 11, 13 и 14 ТЗ:
    GET   /businesses/{business_id}/leads
    PATCH /leads/{lead_id}                      (вне §11: раздел 14 —
                                                 «изменение статуса лида», «назначение ответственного»)

Доступ: OWNER и MANAGER своей компании (раздел 5: MANAGER работает с лидами
и статусами); ADMIN — к любой. Чужой лид — 404.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_db
from models import Lead, LeadPriority, LeadStatus, MemberRole
from schemas import ConversationOut, CustomerOut, LeadListItem, LeadOut, LeadUpdate
from services import lead_service
from services.access_service import BusinessContext, require_business_roles, require_lead_access

router = APIRouter(tags=["leads"])

ANY_MEMBER = (MemberRole.OWNER, MemberRole.MANAGER)


@router.get("/businesses/{business_id}/leads", response_model=list[LeadListItem])
def list_leads(
    priority: LeadPriority | None = Query(default=None, description="HOT / WARM / COLD"),
    status_filter: LeadStatus | None = Query(default=None, alias="status"),
    assigned_to: int | None = Query(default=None, ge=1, description="id ответственного"),
    unassigned: bool = Query(default=False, description="только без ответственного"),
    date_from: datetime | None = Query(default=None, description="Создан не раньше (UTC)"),
    date_to: datetime | None = Query(default=None, description="Создан не позже (UTC)"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    ctx: BusinessContext = Depends(require_business_roles(*ANY_MEMBER)),
    db: Session = Depends(get_db),
):
    """Лиды компании: сначала горячие, внутри приоритета — новые."""
    rows = lead_service.list_leads(
        db,
        ctx,
        priority=priority,
        lead_status=status_filter,
        assigned_to=assigned_to,
        unassigned=unassigned,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return [
        LeadListItem(
            lead=LeadOut.model_validate(lead),
            conversation=ConversationOut.model_validate(conversation),
            customer=CustomerOut.model_validate(customer),
        )
        for lead, conversation, customer in rows
    ]


@router.patch("/leads/{lead_id}", response_model=LeadOut)
def update_lead(
    payload: LeadUpdate,
    resolved: tuple[Lead, BusinessContext] = Depends(require_lead_access(*ANY_MEMBER)),
    db: Session = Depends(get_db),
):
    """Статус лида и ответственный. RESOLVED/LOST закрывают диалог,
    NEW/IN_PROGRESS переоткрывают его."""
    lead, ctx = resolved
    return lead_service.update_lead(db, ctx, lead, payload.model_dump(exclude_unset=True))
