"""
Разграничение доступа (RBAC + мультитенантность).

Раздел 5 ТЗ — роли OWNER / MANAGER / ADMIN.
Раздел 16 ТЗ — роль проверяется на каждом защищённом endpoint, а любой запрос
к данным бизнеса ограничен business_id пользователя.

Ключевое правило модуля: маршрут не работает с business_id из запроса напрямую.
Он получает BusinessContext, который создаётся только после проверки членства
пользователя в компании. Если доступа нет — ответ 404 (а не 403), чтобы нельзя
было перебором id узнать, какие компании существуют в системе.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Path, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models import (
    Business,
    BusinessMember,
    Conversation,
    LogLevel,
    MemberRole,
    Service,
    User,
    UserStatus,
)
from services import audit_service
from services.auth_service import decode_access_token


@dataclass(frozen=True)
class BusinessContext:
    """Проверенный доступ пользователя к конкретной компании."""

    business: Business
    user: User
    role: MemberRole | None  # None — только для платформенного ADMIN без членства
    is_platform_admin: bool

    @property
    def business_id(self) -> int:
        return self.business.id


# --------------------------------------------------------------------------- #
# Текущий пользователь
# --------------------------------------------------------------------------- #
def _extract_token(request: Request) -> str | None:
    """Токен берётся из заголовка Authorization (API-клиенты) либо из
    HttpOnly cookie (кабинет на Jinja2, раздел 9)."""
    header = request.headers.get("Authorization")
    if header:
        scheme, _, credentials = header.partition(" ")
        if scheme.lower() == "bearer" and credentials:
            return credentials.strip()
    return request.cookies.get(settings.auth_cookie_name)


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Обязательная аутентификация: используется всеми защищёнными маршрутами."""
    token = _extract_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Требуется авторизация",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_access_token(token)

    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Недействительный токен"
        ) from exc

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Недействительный токен"
        )
    if user.status is not UserStatus.ACTIVE:
        # Блокировка пользователя действует немедленно, даже если токен ещё жив.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Учётная запись заблокирована"
        )
    return user


def get_current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    """Мягкая аутентификация: None вместо 401.

    Нужна там, где отсутствие/истечение токена не является ошибкой:
    выход из системы и (на этапе 5) страницы кабинета на Jinja2.
    """
    if not _extract_token(request):
        return None
    try:
        return get_current_user(request, db)
    except HTTPException:
        return None


def require_platform_admin(user: User = Depends(get_current_user)) -> User:
    """Доступ только для владельца LeadPilot (раздел 15: панель /admin)."""
    if not user.is_platform_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")
    return user


# --------------------------------------------------------------------------- #
# Доступ к данным конкретной компании
# --------------------------------------------------------------------------- #
def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Компания не найдена")


def build_business_context(db: Session, user: User, business_id: int) -> BusinessContext:
    """Единственный способ получить доступ к данным компании.

    ADMIN платформы получает доступ к любой компании (раздел 5).
    Остальные — только при наличии записи в business_members.
    """
    business = db.get(Business, business_id)

    if user.is_platform_admin:
        if business is None:
            raise _not_found()
        return BusinessContext(business=business, user=user, role=None, is_platform_admin=True)

    membership = db.scalar(
        select(BusinessMember).where(
            BusinessMember.business_id == business_id,
            BusinessMember.user_id == user.id,
        )
    )
    if business is None or membership is None:
        # Попытка обратиться к существующей чужой компании — событие безопасности
        # (раздел 17: критические действия). Наружу всё равно уходит 404.
        if business is not None:
            audit_service.log_event(
                db,
                event_type=audit_service.EventType.ACCESS_DENIED,
                message="Попытка доступа к данным другой компании",
                level=LogLevel.WARNING,
                business_id=business_id,
                actor_user_id=user.id,
                commit=True,
            )
        raise _not_found()

    return BusinessContext(
        business=business, user=user, role=membership.role, is_platform_admin=False
    )


def get_business_context(
    business_id: int = Path(..., ge=1),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BusinessContext:
    """Зависимость для маршрутов вида /businesses/{business_id}/..."""
    return build_business_context(db, user, business_id)


def _assert_role(ctx: BusinessContext, allowed: tuple[MemberRole, ...]) -> None:
    if ctx.is_platform_admin:
        return  # полный доступ к SaaS (раздел 5)
    if ctx.role not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Недостаточно прав для этого действия",
        )


def require_business_roles(*allowed: MemberRole):
    """Фабрика зависимостей: доступ к компании + проверка роли внутри неё.

    Пример: Depends(require_business_roles(MemberRole.OWNER))
    """

    def dependency(ctx: BusinessContext = Depends(get_business_context)) -> BusinessContext:
        _assert_role(ctx, allowed)
        return ctx

    return dependency


def require_service_access(*allowed: MemberRole):
    """Для маршрутов /services/{service_id}, где business_id нет в пути.

    business_id берётся из самой услуги, после чего выполняется та же проверка
    членства — без этого PUT/DELETE /services/{id} позволял бы править
    чужой прайс (раздел 16).
    """

    def dependency(
        service_id: int = Path(..., ge=1),
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> tuple[Service, BusinessContext]:
        service = db.get(Service, service_id)
        if service is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Услуга не найдена")
        try:
            ctx = build_business_context(db, user, service.business_id)
        except HTTPException as exc:
            if exc.status_code == status.HTTP_404_NOT_FOUND:
                # Не раскрываем существование чужой услуги.
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Услуга не найдена"
                ) from exc
            raise
        _assert_role(ctx, allowed)
        return service, ctx

    return dependency


def require_conversation_access(*allowed: MemberRole):
    """Для маршрутов /conversations/{conversation_id}, где business_id нет в пути.

    business_id берётся из самого диалога и проходит ту же проверку членства.
    Чужой и несуществующий диалог неразличимы для клиента API — оба дают 404.
    """

    def dependency(
        conversation_id: int = Path(..., ge=1),
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> tuple[Conversation, BusinessContext]:
        not_found = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Диалог не найден")
        conversation = db.get(Conversation, conversation_id)
        if conversation is None:
            raise not_found
        try:
            ctx = build_business_context(db, user, conversation.business_id)
        except HTTPException as exc:
            if exc.status_code == status.HTTP_404_NOT_FOUND:
                raise not_found from exc
            raise
        _assert_role(ctx, allowed)
        return conversation, ctx

    return dependency
