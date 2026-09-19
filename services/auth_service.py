"""
Регистрация, аутентификация, хеширование паролей и выпуск токенов.

Раздел 6.1 ТЗ: регистрация по email+пароль, вход, выход, хеширование пароля.
Раздел 16 ТЗ: современный алгоритм хеширования, секреты — из окружения.

ДОПУЩЕНИЕ (ТЗ не фиксирует): используется Argon2id + JWT (см. README/ответ).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import settings
from models import User, UserRole, UserStatus
from schemas import UserRegisterRequest

# Argon2id — рекомендация OWASP для новых проектов (устойчив к подбору на GPU).
# Параметры по умолчанию argon2-cffi (m=64 МБ, t=3, p=4) соответствуют
# рекомендованному профилю; при изменении параметров старые хеши остаются
# валидными и обновляются при следующем успешном входе (см. needs_rehash).
_hasher = PasswordHasher()

# Фиктивный хеш для входа с несуществующим email: проверка выполняется всегда,
# чтобы время ответа не позволяло определить, зарегистрирован ли email.
_DUMMY_HASH = _hasher.hash("leadpilot-dummy-password")


# --------------------------------------------------------------------------- #
# Пароли
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    """Хеш Argon2id вместе с солью и параметрами (формат PHC-строки)."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True, если хеш создан с устаревшими параметрами стоимости."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return False


# --------------------------------------------------------------------------- #
# Токены доступа
# --------------------------------------------------------------------------- #
def create_access_token(user: User) -> tuple[str, int]:
    """Возвращает (JWT, время жизни в секундах).

    В токене нет прав на конкретную компанию: доступ к данным компании
    проверяется по business_members при каждом запросе (раздел 16),
    поэтому отзыв доступа сотрудника действует сразу, не дожидаясь
    истечения токена.
    """
    now = datetime.now(UTC)
    expires_delta = timedelta(minutes=settings.access_token_ttl_minutes)
    payload = {
        "sub": str(user.id),
        "role": user.role.value,
        "iss": settings.jwt_issuer,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
        "jti": uuid.uuid4().hex,
        "typ": "access",
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, int(expires_delta.total_seconds())


def decode_access_token(token: str) -> dict:
    """Разбор и проверка JWT. Любая проблема — 401, без деталей наружу."""
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Недействительный или истёкший токен",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "iss"]},
        )
    except jwt.PyJWTError as exc:  # истёк, подпись не совпала, алгоритм не тот
        raise credentials_error from exc
    if payload.get("typ") != "access":
        raise credentials_error
    return payload


# --------------------------------------------------------------------------- #
# Сценарии
# --------------------------------------------------------------------------- #
def get_user_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == email.strip().lower()))


def register_user(db: Session, payload: UserRegisterRequest) -> User:
    """Регистрация пользователя (раздел 6.1).

    Роль ADMIN через публичную регистрацию получить нельзя: владелец SaaS
    создаётся отдельно (переменные BOOTSTRAP_ADMIN_* в .env).
    """
    if get_user_by_email(db, payload.email) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Пользователь с таким email уже зарегистрирован",
        )

    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=UserRole.OWNER,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    try:
        db.flush()  # получаем id, но не фиксируем — коммитит вызывающий слой
    except IntegrityError as exc:  # гонка двух одновременных регистраций
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Пользователь с таким email уже зарегистрирован",
        ) from exc
    return user


def authenticate_user(db: Session, email: str, password: str) -> User | None:
    """Проверка пары email/пароль. None — если вход невозможен."""
    user = get_user_by_email(db, email)
    if user is None:
        verify_password(password, _DUMMY_HASH)  # выравниваем время ответа
        return None
    if not verify_password(password, user.password_hash):
        return None
    if user.status is not UserStatus.ACTIVE:
        # Заблокированный пользователь не получает токен (раздел 15: блокировки).
        return None
    if needs_rehash(user.password_hash):
        # Прозрачное усиление хеша при смене параметров Argon2.
        user.password_hash = hash_password(password)
        db.flush()
    return user
