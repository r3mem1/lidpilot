"""
Маршруты аутентификации — раздел 11 ТЗ:
    POST /auth/register
    POST /auth/login
    POST /auth/logout   (раздел 6.1: «авторизация и выход»)
    GET  /me
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models import Business, BusinessMember, LogLevel, User
from schemas import (
    MembershipOut,
    MeResponse,
    TokenResponse,
    UserLoginRequest,
    UserOut,
    UserRegisterRequest,
)
from services import audit_service, auth_service, rate_limit_service
from services.access_service import get_current_user, get_current_user_optional

router = APIRouter(prefix="/auth", tags=["auth"])
# GET /me по ТЗ находится в корне, без префикса /auth.
me_router = APIRouter(tags=["auth"])


def _set_auth_cookie(response: Response, token: str, max_age: int) -> None:
    """Токен в HttpOnly cookie: кабинет на Jinja2 не хранит его в JS
    (защита от XSS-кражи токена, раздел 16)."""
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        max_age=max_age,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite=settings.auth_cookie_samesite,
        path="/",
    )


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(
    payload: UserRegisterRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> User:
    rate_limit_service.enforce_auth_rate_limit(request, "auth:register")

    user = auth_service.register_user(db, payload)
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.AUTH_REGISTER,
        message=f"Зарегистрирован пользователь {user.email}",
        actor_user_id=user.id,
        payload={"ip": rate_limit_service.client_ip(request)},
    )
    db.commit()
    db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse)
def login(
    payload: UserLoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenResponse:
    rate_limit_service.enforce_auth_rate_limit(request, "auth:login")

    user = auth_service.authenticate_user(db, payload.email, payload.password)
    if user is None:
        # Событие безопасности (раздел 17). commit=True — запрос завершится 401.
        audit_service.log_event(
            db,
            event_type=audit_service.EventType.AUTH_LOGIN_FAILED,
            message="Неудачная попытка входа",
            level=LogLevel.WARNING,
            payload={"email": payload.email, "ip": rate_limit_service.client_ip(request)},
            commit=True,
        )
        # Одинаковый ответ для неверного пароля, несуществующего и
        # заблокированного пользователя — без подсказок для перебора.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный email или пароль",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token, expires_in = auth_service.create_access_token(user)
    _set_auth_cookie(response, token, expires_in)

    audit_service.log_event(
        db,
        event_type=audit_service.EventType.AUTH_LOGIN_SUCCESS,
        message=f"Вход пользователя {user.email}",
        actor_user_id=user.id,
        payload={"ip": rate_limit_service.client_ip(request)},
    )
    db.commit()
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_current_user_optional),
) -> Response:
    """Выход: cookie удаляется всегда, даже если токен уже истёк.

    JWT не отзывается по одному запросу (stateless). Срок жизни короткий,
    а блокировка пользователя действует немедленно — проверяется
    в get_current_user при каждом запросе.
    """
    response.delete_cookie(key=settings.auth_cookie_name, path="/")
    if user is not None:
        audit_service.log_event(
            db,
            event_type=audit_service.EventType.AUTH_LOGOUT,
            message=f"Выход пользователя {user.email}",
            actor_user_id=user.id,
        )
        db.commit()
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@me_router.get("/me", response_model=MeResponse)
def me(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MeResponse:
    """Текущий пользователь и компании, к которым у него есть доступ
    (раздел 6.1: одна или несколько компаний в соответствии с ролью)."""
    rows = db.execute(
        select(BusinessMember.business_id, Business.name, BusinessMember.role)
        .join(Business, Business.id == BusinessMember.business_id)
        .where(BusinessMember.user_id == user.id)
        .order_by(Business.name)
    ).all()

    memberships = [
        MembershipOut(business_id=business_id, business_name=name, role=role)
        for business_id, name, role in rows
    ]
    return MeResponse(user=UserOut.model_validate(user), memberships=memberships)
