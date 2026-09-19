"""
Pydantic-схемы (контракты API) — раздел 11 ТЗ.

Принципы:
* схемы ответов никогда не содержат password_hash и секретов интеграций
  (раздел 16 ТЗ);
* email нормализуется к нижнему регистру, чтобы UNIQUE-ограничение
  и вход в систему были регистронезависимыми;
* цена — Decimal, а не float (точные деньги для AI-ответов, раздел 6.6).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator

from ai.classifier import ClassificationSource, Intent, Priority
from ai.context import HistoryRole
from ai.pipeline import Decision, EscalationReason
from config import settings
from models import (
    AiResponseStatus,
    BusinessStatus,
    Channel,
    ConversationStatus,
    DeliveryStatus,
    IntegrationStatus,
    LeadPriority,
    LogLevel,
    MemberRole,
    ProcessingStatus,
    SenderType,
    UserRole,
    UserStatus,
)

Price = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]


class ORMModel(BaseModel):
    """Базовая схема для сериализации ORM-объектов."""

    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- #
# Аутентификация (раздел 6.1)
# --------------------------------------------------------------------------- #
class _EmailMixin(BaseModel):
    email: EmailStr

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class UserRegisterRequest(_EmailMixin):
    password: str = Field(min_length=1, max_length=128)

    @field_validator("password")
    @classmethod
    def _check_password_policy(cls, value: str) -> str:
        if len(value) < settings.password_min_length:
            raise ValueError(
                f"Пароль должен содержать не менее {settings.password_min_length} символов"
            )
        return value


class UserLoginRequest(_EmailMixin):
    password: str = Field(min_length=1, max_length=128)


class UserOut(ORMModel):
    id: int
    email: EmailStr
    role: UserRole
    status: UserStatus
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - тип схемы OAuth2, не пароль
    expires_in: int  # секунды


class MembershipOut(BaseModel):
    business_id: int
    business_name: str
    role: MemberRole


class MeResponse(BaseModel):
    """GET /me — пользователь и его доступные компании (раздел 6.1)."""

    user: UserOut
    memberships: list[MembershipOut]


# --------------------------------------------------------------------------- #
# Компания (разделы 6.2, 11)
# --------------------------------------------------------------------------- #
class BusinessCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    category: str | None = Field(default=None, max_length=120)
    address: str | None = Field(default=None, max_length=500)
    phone: str | None = Field(default=None, max_length=50)
    working_hours: str | None = Field(default=None, max_length=500)
    description: str | None = None
    ai_rules: str | None = None
    escalation_contact: str | None = Field(default=None, max_length=255)


class BusinessUpdate(BaseModel):
    """PUT /businesses/{id}. Передаются только изменяемые поля;
    отсутствующее поле означает «не изменять» (см. ДОПУЩЕНИЕ 6).
    Статус компании меняет только ADMIN через свою панель (раздел 15)."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    category: str | None = Field(default=None, max_length=120)
    address: str | None = Field(default=None, max_length=500)
    phone: str | None = Field(default=None, max_length=50)
    working_hours: str | None = Field(default=None, max_length=500)
    description: str | None = None
    ai_rules: str | None = None
    escalation_contact: str | None = Field(default=None, max_length=255)


class BusinessOut(ORMModel):
    id: int
    owner_id: int
    name: str
    category: str | None
    address: str | None
    phone: str | None
    working_hours: str | None
    description: str | None
    ai_rules: str | None
    escalation_contact: str | None
    status: BusinessStatus
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- #
# Услуги (раздел 6.3)
# --------------------------------------------------------------------------- #
class ServiceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    price: Price
    description: str | None = None
    duration: int | None = Field(default=None, gt=0, le=24 * 60)  # минуты
    active: bool = True


class ServiceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    price: Price | None = None
    description: str | None = None
    duration: int | None = Field(default=None, gt=0, le=24 * 60)
    active: bool | None = None


class ServiceOut(ORMModel):
    id: int
    business_id: int
    name: str
    price: Decimal
    description: str | None
    duration: int | None
    active: bool
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- #
# Сотрудники компании (раздел 6.1: привязка пользователя к компании)
# --------------------------------------------------------------------------- #
class BusinessMemberCreate(_EmailMixin):
    """Добавление уже зарегистрированного пользователя в компанию.
    Приглашения по email — этап 5 (раздел 13, «Сотрудники»)."""

    role: MemberRole = MemberRole.MANAGER


class BusinessMemberOut(BaseModel):
    id: int
    business_id: int
    user_id: int
    email: EmailStr
    role: MemberRole
    created_at: datetime


# --------------------------------------------------------------------------- #
# Системные логи (разделы 16, 17) — чтение появится в панели ADMIN на этапе 6
# --------------------------------------------------------------------------- #
class SystemLogOut(ORMModel):
    id: int
    business_id: int | None
    level: LogLevel
    event_type: str
    message: str
    payload: dict | None
    created_at: datetime


# --------------------------------------------------------------------------- #
# Диагностика AI (этап 2). Вне минимального списка endpoints раздела 11:
# включается флагом AI_PREVIEW_ENABLED и служит проверкой правил и прайса
# до подключения Telegram. На этапе 5 переедет в раздел «AI» кабинета.
# --------------------------------------------------------------------------- #
class AIHistoryTurnIn(BaseModel):
    role: HistoryRole
    text: str = Field(min_length=1, max_length=4000)


class AIPreviewRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    history: list[AIHistoryTurnIn] = Field(default_factory=list, max_length=20)


class AIPreviewResponse(BaseModel):
    """Полный след работы pipeline (разделы 12.1, 12.2, 17)."""

    decision: Decision
    reply: str | None
    intent: Intent
    priority: Priority
    needs_manager: bool
    reason: str
    classification_source: ClassificationSource
    escalation_reason: EscalationReason | None = None
    escalation_detail: str | None = None
    validation: dict | None = None
    model: str | None = None
    prompt_version: str | None = None
    latency_ms: int


# --------------------------------------------------------------------------- #
# Интеграции каналов (этап 3). Секреты (токен бота, секрет webhook) в схемах
# ответа отсутствуют намеренно (раздел 16: не возвращать секреты интеграций).
# --------------------------------------------------------------------------- #
class TelegramConnectRequest(BaseModel):
    """SecretStr: токен не попадает в repr, логи и тело ошибки валидации 422."""

    # Без min/max_length: при нарушении ограничения FastAPI вернул бы введённое
    # значение в теле 422 (поле input). Формат проверяет сервис.
    bot_token: SecretStr


class IntegrationOut(BaseModel):
    id: int
    business_id: int
    channel: Channel
    status: IntegrationStatus
    bot_username: str | None = None
    webhook_url: str | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- #
# Диалоги и сообщения (этап 3, раздел 11)
# --------------------------------------------------------------------------- #
class CustomerOut(ORMModel):
    id: int
    name: str | None
    username: str | None
    phone: str | None
    channel_blocked: bool


class MessageOut(ORMModel):
    id: int
    sender_type: SenderType
    text: str
    content_type: str
    intent: str | None
    delivery_status: DeliveryStatus | None
    processing_status: ProcessingStatus | None
    created_at: datetime


class AiDecisionOut(ORMModel):
    """Решение AI по сообщению: почему клиенту ушёл именно такой ответ (раздел 17)."""

    id: int
    message_id: int
    response_message_id: int | None
    model: str | None
    prompt_version: str | None
    response_text: str
    latency_ms: int | None
    status: AiResponseStatus
    decision: str
    escalation_reason: str | None
    details: dict | None
    created_at: datetime


class ConversationOut(ORMModel):
    id: int
    business_id: int
    channel: Channel
    status: ConversationStatus
    priority: LeadPriority
    attention_reason: str | None
    created_at: datetime
    updated_at: datetime


class ConversationListItem(BaseModel):
    conversation: ConversationOut
    customer: CustomerOut
    last_message: MessageOut | None = None


class ConversationDetail(BaseModel):
    conversation: ConversationOut
    customer: CustomerOut
    messages: list[MessageOut]
    ai_decisions: list[AiDecisionOut]
