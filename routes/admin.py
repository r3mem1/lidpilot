"""
Административная панель владельца SaaS — разделы 11 и 15 ТЗ (этап 6). JSON API.

    GET /admin/businesses                    список компаний, поиск и фильтры (раздел 11)
    GET /admin/logs                          системные логи с фильтрами (раздел 11)
    PUT /admin/businesses/{id}/status        active / trial / suspended (раздел 11)
    PUT /admin/businesses/{id}/plan          вне §11: тариф и пробный период вручную (§15)
    GET /admin/metrics                       вне §11: метрики SaaS, MRR (§15)

Каждый маршрут закрыт `require_platform_admin` (раздел 15: панель только для ADMIN);
не-ADMIN получает 403 и событие ACCESS_DENIED. Страницы панели — routes/admin_pages.py.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from database import get_db
from models import BusinessStatus, LogLevel, SubscriptionPlan, User
from schemas import (
    AdminBusinessItem,
    AdminBusinessPage,
    AdminBusinessStatusUpdate,
    AdminLogPage,
    AdminMetrics,
    AdminSubscriptionUpdate,
)
from services import admin_service
from services.access_service import require_platform_admin

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/businesses", response_model=AdminBusinessPage)
def list_businesses(
    q: str | None = Query(default=None, max_length=100, description="Поиск по названию"),
    status: BusinessStatus | None = None,
    plan: SubscriptionPlan | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> AdminBusinessPage:
    return admin_service.list_businesses(
        db, q=q, business_status=status, plan=plan, limit=limit, offset=offset
    )


@router.get("/logs", response_model=AdminLogPage)
def list_logs(
    level: list[LogLevel] | None = Query(default=None),
    event_type: str | None = Query(default=None, max_length=64),
    business_id: int | None = Query(default=None, ge=1),
    q: str | None = Query(default=None, max_length=100, description="Поиск по тексту события"),
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> AdminLogPage:
    return admin_service.list_logs(
        db,
        levels=level,
        event_type=event_type,
        business_id=business_id,
        q=q,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )


@router.put("/businesses/{business_id}/status", response_model=AdminBusinessItem)
def update_status(
    payload: AdminBusinessStatusUpdate,
    business_id: int = Path(..., ge=1),
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> AdminBusinessItem:
    business = admin_service.get_business(db, business_id)
    business = admin_service.set_business_status(db, admin, business, payload.status)
    return admin_service.get_business_item(db, business)


@router.put("/businesses/{business_id}/plan", response_model=AdminBusinessItem)
def update_plan(
    payload: AdminSubscriptionUpdate,
    business_id: int = Path(..., ge=1),
    admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> AdminBusinessItem:
    business = admin_service.get_business(db, business_id)
    admin_service.update_subscription(db, admin, business, payload)
    return admin_service.get_business_item(db, business)


@router.get("/metrics", response_model=AdminMetrics)
def metrics(
    _admin: User = Depends(require_platform_admin),
    db: Session = Depends(get_db),
) -> AdminMetrics:
    return admin_service.get_metrics(db)
