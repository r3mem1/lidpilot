"""
Панель администратора (страницы на Jinja2) — раздел 15 ТЗ, этап 6.

    /admin                  Обзор: метрики SaaS, ошибки, интеграции в сбое, истёкшие trial
    /admin/companies        Компании: поиск, фильтры по статусу и тарифу
    /admin/companies/{id}   Карточка компании: показатели, сотрудники, интеграции, управление
    /admin/events           Системные события и ошибки

Страницы отделены от JSON API (routes/admin.py: /admin/businesses, /admin/logs) разными
адресами. Как и в кабинете, страницы только читают данные; статус, тариф и пробный период
меняет JavaScript через JSON API — роль, аудит и проверка Origin остаются в одном месте.
Доступ: не вошёл → форма входа; не ADMIN → 403 и событие ACCESS_DENIED (раздел 17).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from sqlalchemy.orm import Session

from database import get_db
from models import BusinessStatus, LogLevel, SubscriptionPlan, User
from routes.cabinet import LoginRequired, _current_path
from services import admin_service, audit_service
from services.access_service import get_current_user_optional
from templating import templates

router = APIRouter(prefix="/admin", include_in_schema=False)

PAGE_SIZE = 30
LOG_PAGE_SIZE = 50
NAV = [
    {"key": "overview", "label": "Обзор", "path": "/admin"},
    {"key": "companies", "label": "Компании", "path": "/admin/companies"},
    {"key": "events", "label": "События и ошибки", "path": "/admin/events"},
]


def page_admin(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_current_user_optional(request, db)
    if user is None:
        raise LoginRequired(_current_path(request))
    if not user.is_platform_admin:
        audit_service.log_event(
            db,
            event_type=audit_service.EventType.ACCESS_DENIED,
            message="Попытка доступа к странице панели администратора",
            level=LogLevel.WARNING,
            actor_user_id=user.id,
            payload={"path": request.url.path, "method": request.method},
            commit=True,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")
    return user


def _render(request: Request, user: User, template: str, active: str, title: str, **data):
    return templates.TemplateResponse(
        request,
        template,
        {"user": user, "nav": NAV, "active": active, "title": title, **data},
    )


def _enum_or_none(enum_cls, value: str | None):
    """Значение фильтра из адресной строки; мусор игнорируется, а не даёт 422."""
    try:
        return enum_cls(value) if value else None
    except ValueError:
        return None


def _int_or_none(value: str | None) -> int | None:
    try:
        parsed = int(value) if value else None
    except ValueError:
        return None
    return parsed if parsed and parsed > 0 else None


def _day(value: str | None, *, end: bool) -> datetime | None:
    try:
        day = date.fromisoformat(value) if value else None
    except ValueError:
        return None
    if day is None:
        return None
    return datetime.combine(day, time.max if end else time.min, tzinfo=UTC)


def _offset(value: int) -> int:
    return max(value, 0)


@router.get("")
def overview(request: Request, user: User = Depends(page_admin), db: Session = Depends(get_db)):
    errors = admin_service.list_logs(db, levels=list(admin_service.ERROR_LEVELS), limit=8).items
    return _render(
        request,
        user,
        "admin.html",
        "overview",
        "Обзор платформы",
        metrics=admin_service.get_metrics(db),
        errors=errors,
        integration_errors=admin_service.integrations_with_errors(db, limit=8),
        expired_trials=admin_service.expired_trials(db, limit=8),
    )


@router.get("/companies")
def companies(
    request: Request,
    q: str | None = Query(default=None, max_length=100),
    status_: str | None = Query(default=None, alias="status", max_length=20),
    plan: str | None = Query(default=None, max_length=20),
    offset: int = Query(default=0),
    user: User = Depends(page_admin),
    db: Session = Depends(get_db),
):
    offset = _offset(offset)
    page = admin_service.list_businesses(
        db,
        q=q,
        business_status=_enum_or_none(BusinessStatus, status_),
        plan=_enum_or_none(SubscriptionPlan, plan),
        limit=PAGE_SIZE,
        offset=offset,
    )
    return _render(
        request,
        user,
        "admin_companies.html",
        "companies",
        "Компании",
        page=page,
        filters={"q": q or "", "status": status_ or "", "plan": plan or ""},
        statuses=[s.value for s in BusinessStatus],
        plans=[p.value for p in SubscriptionPlan],
        prev_offset=offset - PAGE_SIZE if offset > 0 else None,
        next_offset=offset + PAGE_SIZE if offset + PAGE_SIZE < page.total else None,
    )


@router.get("/companies/{business_id}")
def company(
    request: Request,
    business_id: int = Path(..., ge=1),
    user: User = Depends(page_admin),
    db: Session = Depends(get_db),
):
    business = admin_service.get_business(db, business_id)
    return _render(
        request,
        user,
        "admin_company.html",
        "companies",
        business.name,
        detail=admin_service.business_detail(db, business),
        statuses=[s.value for s in BusinessStatus],
        plans=[p.value for p in SubscriptionPlan],
        prices={p.value: admin_service.plan_price(p) for p in SubscriptionPlan},
        now=datetime.now(UTC),
    )


@router.get("/events")
def events(
    request: Request,
    level: list[str] = Query(default=[]),
    event_type: str | None = Query(default=None, max_length=64),
    business_id: str | None = Query(default=None, max_length=12),
    q: str | None = Query(default=None, max_length=100),
    date_from: str | None = Query(default=None, max_length=10),
    date_to: str | None = Query(default=None, max_length=10),
    offset: int = Query(default=0),
    user: User = Depends(page_admin),
    db: Session = Depends(get_db),
):
    offset = _offset(offset)
    levels = [lv for lv in (_enum_or_none(LogLevel, v) for v in level) if lv is not None]
    start, end = _day(date_from, end=False), _day(date_to, end=True)
    if start is not None and end is not None and end < start:
        start = end = None
    page = admin_service.list_logs(
        db,
        levels=levels or None,
        event_type=event_type or None,
        business_id=_int_or_none(business_id),
        q=q,
        date_from=start,
        date_to=end,
        limit=LOG_PAGE_SIZE,
        offset=offset,
    )
    return _render(
        request,
        user,
        "admin_events.html",
        "events",
        "События и ошибки",
        page=page,
        filters={
            "levels": [lv.value for lv in levels],
            "event_type": event_type or "",
            "business_id": business_id or "",
            "q": q or "",
            "date_from": date_from if start else "",
            "date_to": date_to if end else "",
        },
        levels=[lv.value for lv in LogLevel],
        event_types=admin_service.event_types(db),
        prev_offset=offset - LOG_PAGE_SIZE if offset > 0 else None,
        next_offset=offset + LOG_PAGE_SIZE if offset + LOG_PAGE_SIZE < page.total else None,
    )
