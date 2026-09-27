"""
Кабинет бизнеса (страницы на Jinja2) — разделы 9, 13 и 14 ТЗ, этап 5.

    /login, /register, /invite/{token}      вход, регистрация, приглашение
    /cabinet                                выбор компании / создание первой
    /cabinet/{id}                           Обзор
    /cabinet/{id}/messages                  Сообщения: список диалогов и переписка (§13, §14)
    /cabinet/{id}/leads                     Лиды
    /cabinet/{id}/customers[/{customer_id}] Клиенты и история обращений
    /cabinet/{id}/services                  Услуги
    /cabinet/{id}/ai                        AI: правила, стиль, автоответ, проверка ответа
    /cabinet/{id}/team                      Сотрудники и приглашения
    /cabinet/{id}/settings                  Данные компании и интеграции
    /cabinet/{id}/analytics                 Аналитика

Страницы — тонкий слой: данные берут сервисы (те же, что у JSON API), доступ проверяет
access_service. Любое изменение данных JavaScript делает через JSON API — так роли,
изоляция компаний, аудит и защита от CSRF остаются в одном месте (раздел 16).

Роли (раздел 5): OWNER — всё; MANAGER — обзор, сообщения, лиды, клиенты и просмотр
услуг, без системных настроек. Чужая компания — 404, недостаточная роль — 403.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import TypeVar

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from cabinet_labels import TONE_HINTS
from config import settings
from database import get_db
from models import (
    BookingStatus,
    Business,
    BusinessMember,
    Channel,
    Conversation,
    ConversationStatus,
    Customer,
    LeadPriority,
    LeadStatus,
    MemberRole,
    SubscriptionPlan,
    User,
)
from services import (
    analytics_service,
    booking_request_service,
    booking_service,
    business_service,
    integration_service,
    lead_service,
    master_service,
    message_service,
    onboarding_service,
    schedule_service,
    subscription_service,
    team_service,
)
from services.access_service import (
    BusinessContext,
    _assert_role,
    build_business_context,
    get_current_user_optional,
)
from templating import templates

router = APIRouter(include_in_schema=False)

ANY_MEMBER = (MemberRole.OWNER, MemberRole.MANAGER)
OWNER_ONLY = (MemberRole.OWNER,)
# Вне ТЗ (§22): мастер видит только «Моё расписание», «Мои записи», «Уведомления».
ALL_MEMBERS = (MemberRole.OWNER, MemberRole.MANAGER, MemberRole.MASTER)

PAGE_SIZE = 30
LEADS_PAGE_SIZE = 50

E = TypeVar("E")


class LoginRequired(Exception):
    """Страница требует входа: обработчик в main.py перенаправит на /login."""

    def __init__(self, next_url: str) -> None:
        self.next_url = next_url


# --------------------------------------------------------------------------- #
# Общие помощники
# --------------------------------------------------------------------------- #
def safe_next(value: str | None) -> str:
    """Куда вернуть пользователя после входа. Только внутренние адреса кабинета:
    иначе форма входа стала бы открытым перенаправлением на чужой сайт."""
    if (
        value
        and (value.startswith(("/cabinet", "/invite/", "/admin/")) or value == "/admin")
        and not value.startswith("//")
    ):
        return value if "\\" not in value else "/cabinet"
    return "/cabinet"


def _current_path(request: Request) -> str:
    query = f"?{request.url.query}" if request.url.query else ""
    return f"{request.url.path}{query}"


def page_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_current_user_optional(request, db)
    if user is None:
        raise LoginRequired(_current_path(request))
    return user


def page_business(*allowed: MemberRole):
    """Доступ к странице компании: вход + членство + роль (404 / 403 / редирект на вход)."""

    def dependency(
        request: Request,
        business_id: int = Path(..., ge=1),
        db: Session = Depends(get_db),
    ) -> BusinessContext:
        user = get_current_user_optional(request, db)
        if user is None:
            raise LoginRequired(_current_path(request))
        ctx = build_business_context(db, user, business_id)
        _assert_role(ctx, allowed)
        return ctx

    return dependency


def _enum_or_none(enum_cls: type[E], value: str | None) -> E | None:
    """Значение фильтра из адресной строки; мусор игнорируется, а не даёт 422."""
    if not value:
        return None
    try:
        return enum_cls(value)  # type: ignore[call-arg]
    except ValueError:
        return None


def _int_or_none(value: str | None) -> int | None:
    try:
        parsed = int(value) if value else None
    except ValueError:
        return None
    return parsed if parsed and parsed > 0 else None


def _nav(is_owner: bool, is_master: bool = False) -> list[dict]:
    if is_master:
        items = [
            ("schedule", "Моё расписание", "/schedule"),
            ("bookings", "Мои записи", "/bookings"),
            ("notify", "Уведомления", "/notify"),
        ]
        return [
            {
                "title": "Мастер",
                "items": [{"key": k, "label": lbl, "path": p} for k, lbl, p in items],
            }
        ]
    work = [
        ("dashboard", "Обзор", ""),
        ("messages", "Сообщения", "/messages"),
        ("bookings", "Записи", "/bookings"),
        ("schedule", "Расписание", "/schedule"),
        ("leads", "Лиды", "/leads"),
        ("customers", "Клиенты", "/customers"),
    ]
    setup = [("services", "Услуги", "/services")]
    if is_owner:
        setup += [
            ("ai", "AI-ассистент", "/ai"),
            ("team", "Сотрудники", "/team"),
            ("settings", "Настройки", "/settings"),
            ("analytics", "Аналитика", "/analytics"),
        ]
    return [
        {"title": "Работа", "items": [{"key": k, "label": lbl, "path": p} for k, lbl, p in work]},
        {
            "title": "Настройка",
            "items": [{"key": k, "label": lbl, "path": p} for k, lbl, p in setup],
        },
    ]


def _memberships(db: Session, user: User) -> list[dict]:
    rows = db.execute(
        select(BusinessMember.business_id, Business.name, BusinessMember.role)
        .join(Business, Business.id == BusinessMember.business_id)
        .where(BusinessMember.user_id == user.id)
        .order_by(Business.name)
    ).all()
    return [{"id": bid, "name": name, "role": role.value} for bid, name, role in rows]


def render(
    request: Request,
    db: Session,
    ctx: BusinessContext,
    template: str,
    active: str,
    title: str,
    **data,
) -> HTMLResponse:
    """Страница кабинета с общей рамкой: меню по роли, счётчик «требует внимания»."""
    is_owner = ctx.is_platform_admin or ctx.role is MemberRole.OWNER
    is_master = not ctx.is_platform_admin and ctx.role is MemberRole.MASTER
    context = {
        "user": ctx.user,
        "business": ctx.business,
        "business_id": ctx.business_id,
        "role": "ADMIN" if ctx.is_platform_admin else (ctx.role.value if ctx.role else ""),
        "is_owner": is_owner,
        "memberships": _memberships(db, ctx.user),
        "nav": _nav(is_owner, is_master),
        "is_master": is_master,
        "active": active,
        "title": title,
        "attention_count": analytics_service.attention_count(db, ctx.business_id),
        # Вне ТЗ (§22): открытые заявки на запись — счётчик у «Записей» для сотрудников.
        "requests_count": (
            booking_request_service.count_open(db, ctx.business_id)
            if master_service.is_staff(ctx)
            else 0
        ),
        # Этап 8: тариф и срок — баннер об окончании срока на всех страницах кабинета.
        "subscription": subscription_service.subscription_info(db, ctx.business_id),
        **data,
    }
    return templates.TemplateResponse(request, template, context)


# --------------------------------------------------------------------------- #
# Вход, регистрация, приглашение
# --------------------------------------------------------------------------- #
@router.get("/")
def root() -> RedirectResponse:
    return RedirectResponse("/cabinet", status_code=303)


@router.get("/login")
def login_page(
    request: Request,
    next: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    target = safe_next(next)
    if get_current_user_optional(request, db) is not None:
        return RedirectResponse(target, status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"title": "Вход", "next": target, "email": ""}
    )


@router.get("/register")
def register_page(
    request: Request,
    next: str | None = Query(default=None),
    email: str | None = Query(default=None, max_length=320),
    db: Session = Depends(get_db),
):
    target = safe_next(next)
    if get_current_user_optional(request, db) is not None:
        return RedirectResponse(target, status_code=303)
    return templates.TemplateResponse(
        request,
        "register.html",
        {"title": "Регистрация", "next": target, "email": email or ""},
    )


@router.get("/invite/{token}")
def invite_page(
    request: Request, token: str = Path(..., max_length=200), db: Session = Depends(get_db)
):
    invitation = team_service.find_valid_invitation(db, token)
    user = get_current_user_optional(request, db)
    business = db.get(Business, invitation.business_id) if invitation else None
    return templates.TemplateResponse(
        request,
        "invite.html",
        {
            "title": "Приглашение",
            "invitation": invitation,
            "business": business,
            "user": user,
            "token": token,
            "email_matches": bool(user and invitation and user.email == invitation.email),
            "next": f"/invite/{token}",
        },
        status_code=200 if invitation else 404,
    )


# --------------------------------------------------------------------------- #
# Выбор компании
# --------------------------------------------------------------------------- #
@router.get("/cabinet")
def cabinet_home(request: Request, user: User = Depends(page_user), db: Session = Depends(get_db)):
    memberships = _memberships(db, user)
    if user.is_platform_admin and not memberships:
        return RedirectResponse("/admin", status_code=303)  # у ADMIN своей компании нет
    if len(memberships) == 1:
        return RedirectResponse(f"/cabinet/{memberships[0]['id']}", status_code=303)
    return templates.TemplateResponse(
        request,
        "home.html",
        {"title": "Компании", "user": user, "memberships": memberships, "first": not memberships},
    )


# --------------------------------------------------------------------------- #
# Обзор
# --------------------------------------------------------------------------- #
@router.get("/cabinet/{business_id}")
def dashboard(
    request: Request,
    ctx: BusinessContext = Depends(page_business(*ALL_MEMBERS)),
    db: Session = Depends(get_db),
):
    if not ctx.is_platform_admin and ctx.role is MemberRole.MASTER:
        # У мастера нет обзора: его «главная» — собственное расписание.
        return RedirectResponse(f"/cabinet/{ctx.business_id}/schedule", status_code=303)
    summary = analytics_service.dashboard_summary(db, ctx)
    queue = message_service.list_conversations(
        db, ctx, status=ConversationStatus.NEEDS_ATTENTION, limit=8
    )
    hot_leads = lead_service.list_leads(
        db, ctx, priority=LeadPriority.HOT, lead_status=LeadStatus.NEW, limit=5
    )
    # Этап 8: чек-лист onboarding видит владелец, пока не выполнены все шаги.
    steps = onboarding_service.steps(db, ctx) if _is_owner(ctx) else []
    return render(
        request,
        db,
        ctx,
        "dashboard.html",
        "dashboard",
        "Обзор",
        summary=summary,
        queue=queue,
        hot_leads=hot_leads,
        onboarding={
            "steps": steps,
            "done": sum(step.done for step in steps),
            "show": bool(steps) and not all(step.done for step in steps),
        },
    )


def _is_owner(ctx: BusinessContext) -> bool:
    return ctx.is_platform_admin or ctx.role is MemberRole.OWNER


# --------------------------------------------------------------------------- #
# Сообщения (разделы 13, 14)
# --------------------------------------------------------------------------- #
@router.get("/cabinet/{business_id}/messages")
def messages_page(
    request: Request,
    status: str | None = Query(default=None),
    priority: str | None = Query(default=None),
    c: str | None = Query(default=None),
    offset: int = Query(default=0, ge=0, le=100000),
    ctx: BusinessContext = Depends(page_business(*ANY_MEMBER)),
    db: Session = Depends(get_db),
):
    status_filter = _enum_or_none(ConversationStatus, status)
    priority_filter = _enum_or_none(LeadPriority, priority)
    rows = message_service.list_conversations(
        db,
        ctx,
        status=status_filter,
        priority=priority_filter,
        limit=PAGE_SIZE + 1,
        offset=offset,
    )
    has_more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]

    # Выбранный диалог: явный c (только своей компании) или первый в списке.
    explicit_id = _int_or_none(c)
    selected: Conversation | None = None
    if explicit_id is not None:
        candidate = db.get(Conversation, explicit_id)
        if candidate is not None and candidate.business_id == ctx.business_id:
            selected = candidate
    if selected is None and rows:
        selected = rows[0][0]

    detail = None
    if selected is not None:
        customer, lead, messages, decisions = message_service.get_conversation_detail(
            db, ctx, selected
        )
        authors = {}
        author_ids = {m.author_user_id for m in messages if m.author_user_id}
        if author_ids:
            authors = {
                u.id: u.email for u in db.scalars(select(User).where(User.id.in_(author_ids))).all()
            }
        detail = {
            "conversation": selected,
            "customer": customer,
            "lead": lead,
            "messages": messages,
            "authors": authors,
            "last_decision": decisions[-1] if decisions else None,
            "decisions_by_reply": {
                d.response_message_id: d for d in decisions if d.response_message_id
            },
        }

    counts = analytics_service.conversation_counts(db, ctx.business_id)
    members = business_service.list_members(db, ctx)
    return render(
        request,
        db,
        ctx,
        "messages.html",
        "messages",
        "Сообщения",
        rows=rows,
        has_more=has_more,
        next_offset=offset + PAGE_SIZE,
        status_filter=status_filter.value if status_filter else "",
        priority_filter=priority_filter.value if priority_filter else "",
        counts=counts,
        last_message_id=message_service.inbox_state(db, ctx)["last_message_id"],
        detail=detail,
        explicit=explicit_id is not None and selected is not None,
        members=[{"id": u.id, "email": u.email} for _m, u in members],
        lead_statuses=[s.value for s in LeadStatus],
    )


# --------------------------------------------------------------------------- #
# Лиды
# --------------------------------------------------------------------------- #
@router.get("/cabinet/{business_id}/leads")
def leads_page(
    request: Request,
    priority: str | None = Query(default=None),
    status: str | None = Query(default=None),
    assigned: str | None = Query(default=None),
    date_from: str | None = Query(default=None, max_length=10),
    date_to: str | None = Query(default=None, max_length=10),
    offset: int = Query(default=0, ge=0, le=100000),
    ctx: BusinessContext = Depends(page_business(*ANY_MEMBER)),
    db: Session = Depends(get_db),
):
    priority_filter = _enum_or_none(LeadPriority, priority)
    status_filter = _enum_or_none(LeadStatus, status)
    unassigned = assigned == "none"
    assigned_id = None if unassigned else _int_or_none(assigned)
    start = _day_start(date_from)
    end = _day_end(date_to)
    rows = lead_service.list_leads(
        db,
        ctx,
        priority=priority_filter,
        lead_status=status_filter,
        assigned_to=assigned_id,
        unassigned=unassigned,
        date_from=start,
        date_to=end,
        limit=LEADS_PAGE_SIZE + 1,
        offset=offset,
    )
    has_more = len(rows) > LEADS_PAGE_SIZE
    members = business_service.list_members(db, ctx)
    return render(
        request,
        db,
        ctx,
        "leads.html",
        "leads",
        "Лиды",
        rows=rows[:LEADS_PAGE_SIZE],
        has_more=has_more,
        next_offset=offset + LEADS_PAGE_SIZE,
        filters={
            "priority": priority_filter.value if priority_filter else "",
            "status": status_filter.value if status_filter else "",
            "assigned": "none" if unassigned else (str(assigned_id) if assigned_id else ""),
            "date_from": date_from if start else "",
            "date_to": date_to if end else "",
        },
        members=[{"id": u.id, "email": u.email} for _m, u in members],
        lead_statuses=[s.value for s in LeadStatus],
    )


def _parse_day(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _day_start(value: str | None) -> datetime | None:
    day = _parse_day(value)
    return datetime.combine(day, time.min, tzinfo=UTC) if day else None


def _day_end(value: str | None) -> datetime | None:
    day = _parse_day(value)
    return datetime.combine(day, time.max, tzinfo=UTC) if day else None


# --------------------------------------------------------------------------- #
# Клиенты
# --------------------------------------------------------------------------- #
@router.get("/cabinet/{business_id}/customers")
def customers_page(
    request: Request,
    q: str | None = Query(default=None, max_length=100),
    offset: int = Query(default=0, ge=0, le=100000),
    ctx: BusinessContext = Depends(page_business(*ANY_MEMBER)),
    db: Session = Depends(get_db),
):
    rows = message_service.list_customers(db, ctx, search=q, limit=PAGE_SIZE + 1, offset=offset)
    return render(
        request,
        db,
        ctx,
        "customers.html",
        "customers",
        "Клиенты",
        rows=rows[:PAGE_SIZE],
        has_more=len(rows) > PAGE_SIZE,
        next_offset=offset + PAGE_SIZE,
        q=q or "",
    )


@router.get("/cabinet/{business_id}/customers/{customer_id}")
def customer_page(
    request: Request,
    customer_id: int = Path(..., ge=1),
    ctx: BusinessContext = Depends(page_business(*ANY_MEMBER)),
    db: Session = Depends(get_db),
):
    customer = db.get(Customer, customer_id)
    if customer is None or customer.business_id != ctx.business_id:
        raise HTTPException(status_code=404, detail="Клиент не найден")
    history = message_service.get_customer_history(db, ctx, customer)
    return render(
        request,
        db,
        ctx,
        "customer_detail.html",
        "customers",
        customer.name or (f"@{customer.username}" if customer.username else "Клиент"),
        customer=customer,
        history=history,
    )


# --------------------------------------------------------------------------- #
# Услуги
# --------------------------------------------------------------------------- #
@router.get("/cabinet/{business_id}/services")
def services_page(
    request: Request,
    ctx: BusinessContext = Depends(page_business(*ANY_MEMBER)),
    db: Session = Depends(get_db),
):
    return render(
        request,
        db,
        ctx,
        "services.html",
        "services",
        "Услуги",
        services=business_service.list_services(db, ctx),
        can_edit=_is_owner(ctx),
    )


# --------------------------------------------------------------------------- #
# AI (раздел 13: правила, стиль ответа, разрешённые действия)
# --------------------------------------------------------------------------- #
@router.get("/cabinet/{business_id}/ai")
def ai_page(
    request: Request,
    ctx: BusinessContext = Depends(page_business(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    return render(
        request,
        db,
        ctx,
        "ai.html",
        "ai",
        "AI-ассистент",
        tone_hints=TONE_HINTS,
        preview_enabled=settings.ai_preview_enabled,
        offline=settings.ai_provider == "stub",
    )


# --------------------------------------------------------------------------- #
# Сотрудники
# --------------------------------------------------------------------------- #
@router.get("/cabinet/{business_id}/team")
def team_page(
    request: Request,
    ctx: BusinessContext = Depends(page_business(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    members = business_service.list_members(db, ctx)
    masters = master_service.list_masters(db, ctx)
    return render(
        request,
        db,
        ctx,
        "team.html",
        "team",
        "Сотрудники",
        members=members,
        invitations=team_service.list_invitations(db, ctx),
        masters=masters,
        master_services=master_service.service_ids_by_master(db, [m.id for m in masters]),
        services=business_service.list_services(db, ctx),
        owners_count=sum(1 for m, _u in members if m.role is MemberRole.OWNER),
        invitation_ttl_days=settings.invitation_ttl_days,
    )


# --------------------------------------------------------------------------- #
# Расписание мастеров и записи (вне ТЗ, §22)
# --------------------------------------------------------------------------- #
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
RU_TIMEZONES = (
    ("Europe/Kaliningrad", "Калининград (МСК−1)"),
    ("Europe/Moscow", "Москва (МСК)"),
    ("Europe/Samara", "Самара (МСК+1)"),
    ("Asia/Yekaterinburg", "Екатеринбург (МСК+2)"),
    ("Asia/Omsk", "Омск (МСК+3)"),
    ("Asia/Novosibirsk", "Новосибирск (МСК+4)"),
    ("Asia/Krasnoyarsk", "Красноярск (МСК+4)"),
    ("Asia/Irkutsk", "Иркутск (МСК+5)"),
    ("Asia/Yakutsk", "Якутск (МСК+6)"),
    ("Asia/Vladivostok", "Владивосток (МСК+7)"),
    ("Asia/Magadan", "Магадан (МСК+8)"),
    ("Asia/Kamchatka", "Камчатка (МСК+9)"),
)


def _local(value: datetime, tz) -> datetime:
    return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(tz)


@router.get("/cabinet/{business_id}/schedule")
def schedule_page(
    request: Request,
    week: str | None = Query(default=None),
    ctx: BusinessContext = Depends(page_business(*ALL_MEMBERS)),
    db: Session = Depends(get_db),
):
    tz = schedule_service.business_tz(ctx.business)
    today = datetime.now(tz).date()
    start = schedule_service.week_start(_parse_day(week) or today)
    days = [start + timedelta(days=i) for i in range(7)]
    masters = master_service.list_masters(db, ctx)
    shifts = schedule_service.list_shifts(db, ctx, days[0], days[-1])
    bookings = booking_service.list_bookings(
        db, ctx, day_from=days[0], day_to=days[-1], statuses=booking_service.ACTIVE
    )
    service_names = {s.id: s.name for s in business_service.list_services(db, ctx)}
    grid: dict = {(m.id, d): {"shifts": [], "bookings": []} for m in masters for d in days}
    for shift in shifts:
        if (shift.master_id, shift.day) in grid:
            grid[(shift.master_id, shift.day)]["shifts"].append(shift)
    for booking in bookings:
        local = _local(booking.starts_at, tz)
        key = (booking.master_id, local.date())
        if key in grid:
            grid[key]["bookings"].append(
                {
                    "time": local.strftime("%H:%M"),
                    "service": service_names.get(booking.service_id or 0, "Услуга"),
                    "client": booking.client_name,
                    "pending": booking.status is BookingStatus.PENDING,
                }
            )
    editable = [m for m in masters if master_service.can_edit_schedule(ctx, m)]
    is_master = not ctx.is_platform_admin and ctx.role is MemberRole.MASTER
    return render(
        request,
        db,
        ctx,
        "schedule.html",
        "schedule",
        "Моё расписание" if is_master else "Расписание",
        days=[(d, WEEKDAYS[d.weekday()]) for d in days],
        today=today,
        masters=masters,
        grid=grid,
        editable={m.id for m in editable},
        editable_masters=editable,
        prev_week=(start - timedelta(days=7)).isoformat(),
        next_week=(start + timedelta(days=7)).isoformat(),
        this_week=schedule_service.week_start(today).isoformat(),
        tz_name=ctx.business.timezone,
    )


@router.get("/cabinet/{business_id}/bookings")
def bookings_page(
    request: Request,
    date_from: str | None = Query(default=None, alias="from"),
    date_to: str | None = Query(default=None, alias="to"),
    status_filter: str | None = Query(default=None, alias="status"),
    master: str | None = Query(default=None),
    slot_service: str | None = Query(default=None),
    slot_day: str | None = Query(default=None),
    ctx: BusinessContext = Depends(page_business(*ALL_MEMBERS)),
    db: Session = Depends(get_db),
):
    tz = schedule_service.business_tz(ctx.business)
    today = datetime.now(tz).date()
    start = _parse_day(date_from) or today
    end = _parse_day(date_to) or start + timedelta(days=13)
    if end < start or (end - start).days > schedule_service.MAX_RANGE_DAYS:
        end = start + timedelta(days=13)
    booking_status = _enum_or_none(BookingStatus, status_filter)
    master_id = _int_or_none(master)
    rows = booking_service.list_bookings(
        db,
        ctx,
        day_from=start,
        day_to=end,
        master_id=master_id,
        statuses=(booking_status,) if booking_status else None,
    )
    masters = master_service.list_masters(db, ctx)
    master_names = {m.id: m.display_name for m in masters}
    services = business_service.list_services(db, ctx)
    service_names = {s.id: s.name for s in services}
    is_staff = master_service.is_staff(ctx)
    items = [
        {
            "booking": b,
            "local": _local(b.starts_at, tz),
            "end": _local(b.ends_at, tz),
            "master": master_names.get(b.master_id, "—"),
            "service": service_names.get(b.service_id or 0, "—"),
        }
        for b in rows
    ]

    # Вне ТЗ (§22): заявки на запись, которые AI понял, но не забронировал сам.
    requests = []
    if is_staff:
        weekdays = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
        parts = {"morning": "утром", "day": "днём", "evening": "вечером"}
        for r in booking_request_service.list_open(db, ctx):
            when = []
            if r.desired_day:
                when.append(f"{r.desired_day:%d.%m} ({weekdays[r.desired_day.weekday()]})")
            if r.desired_time:
                when.append(f"{r.desired_time:%H:%M}")
            elif r.part_of_day:
                when.append(parts.get(r.part_of_day, ""))
            requests.append(
                {
                    "request": r,
                    "when": ", ".join(when),
                    "service": service_names.get(r.service_id or 0),
                    "master": master_names.get(r.master_id or 0),
                    "created": _local(r.created_at, tz),
                    "day": r.desired_day.isoformat() if r.desired_day else "",
                    "time": f"{r.desired_time:%H:%M}" if r.desired_time else "",
                }
            )

    slots: list = []
    slot_service_id = _int_or_none(slot_service)
    chosen_day = _parse_day(slot_day)
    if is_staff and slot_service_id and chosen_day:
        chosen = next((s for s in services if s.id == slot_service_id), None)
        if chosen is not None:
            slots = booking_service.free_slots(
                db, ctx.business, chosen, day_from=chosen_day, day_to=chosen_day, limit=60
            )
    return render(
        request,
        db,
        ctx,
        "bookings.html",
        "bookings",
        "Записи" if is_staff else "Мои записи",
        items=items,
        requests=requests,
        masters=masters,
        services=[s for s in services if s.active],
        is_staff=is_staff,
        date_from=start.isoformat(),
        date_to=end.isoformat(),
        status_filter=booking_status.value if booking_status else "",
        master_filter=master_id,
        statuses=list(BookingStatus),
        slots=slots,
        slot_service=slot_service_id,
        slot_day=chosen_day.isoformat() if chosen_day else today.isoformat(),
        tz_name=ctx.business.timezone,
    )


@router.get("/cabinet/{business_id}/notify")
def notify_page(
    request: Request,
    ctx: BusinessContext = Depends(page_business(*ALL_MEMBERS)),
    db: Session = Depends(get_db),
):
    master = master_service.own_master(db, ctx)
    channels = [
        i.channel
        for i in integration_service.list_integrations(db, ctx)
        if i.status.value == "ACTIVE"
    ]
    return render(
        request,
        db,
        ctx,
        "notify.html",
        "notify",
        "Уведомления",
        master=master,
        channels=channels,
    )


# --------------------------------------------------------------------------- #
# Настройки и интеграции
# --------------------------------------------------------------------------- #
@router.get("/cabinet/{business_id}/settings")
def settings_page(
    request: Request,
    ctx: BusinessContext = Depends(page_business(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    integrations = integration_service.list_integrations(db, ctx)
    telegram = next((i for i in integrations if i.channel.value == "TELEGRAM"), None)
    vk_integration = next((i for i in integrations if i.channel.value == "VK"), None)
    return render(
        request,
        db,
        ctx,
        "settings.html",
        "settings",
        "Настройки",
        telegram=telegram,
        vk=vk_integration,
        vk_webhook_url=integration_service.webhook_url(Channel.VK),
        webhook_url=integration_service.webhook_url(),
        webhook_ready=bool((settings.public_base_url or "").lower().startswith("https://")),
        plans=[
            (plan, subscription_service.plan_price(plan))
            for plan in (SubscriptionPlan.START, SubscriptionPlan.PRO)
        ],
        billing_contact=settings.billing_contact,
        timezones=RU_TIMEZONES,
        masters_count=len(master_service.list_masters(db, ctx, only_active=True)),
    )


# --------------------------------------------------------------------------- #
# Аналитика
# --------------------------------------------------------------------------- #
@router.get("/cabinet/{business_id}/analytics")
def analytics_page(
    request: Request,
    days: int = Query(default=30, ge=1, le=366),
    date_from: str | None = Query(default=None, max_length=10),
    date_to: str | None = Query(default=None, max_length=10),
    ctx: BusinessContext = Depends(page_business(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    now = datetime.now(UTC)
    start, end = now - timedelta(days=days), now
    custom = False
    from_day, to_day = _day_start(date_from), _day_end(date_to)
    if from_day is not None and to_day is not None:
        start, end, custom = from_day, to_day, True
    try:
        start, end = analytics_service.resolve_period(start, end)
    except HTTPException:
        # Некорректный период (конец раньше начала, больше года): последние 30 суток.
        start, end, custom = now - timedelta(days=30), now, False
    summary = analytics_service.period_summary(db, ctx, start, end)
    daily_max = max((d["incoming"] for d in summary["daily"]), default=0)
    return render(
        request,
        db,
        ctx,
        "analytics.html",
        "analytics",
        "Аналитика",
        summary=summary,
        daily_max=daily_max,
        days=None if custom else days,
        date_from=start.date().isoformat(),
        date_to=end.date().isoformat(),
    )


# --------------------------------------------------------------------------- #
# Ответы-заглушки для обработчиков ошибок (вызываются из main.py)
# --------------------------------------------------------------------------- #
def login_redirect(exc: LoginRequired) -> Response:
    from urllib.parse import quote

    return RedirectResponse(
        f"/login?next={quote(safe_next(exc.next_url), safe='/?=&')}", status_code=303
    )
