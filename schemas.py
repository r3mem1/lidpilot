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

from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from ai.classifier import ClassificationSource, Intent, Priority
from ai.context import HistoryRole
from ai.pipeline import Decision, EscalationReason
from config import settings
from models import (
    AiResponseStatus,
    AiTone,
    BookingSource,
    BookingStatus,
    BusinessStatus,
    Channel,
    ConversationStatus,
    DeliveryStatus,
    IntegrationStatus,
    LeadPriority,
    LeadStatus,
    LogLevel,
    MemberRole,
    ProcessingStatus,
    SenderType,
    SubscriptionPlan,
    SubscriptionStatus,
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
    ai_tone: AiTone = AiTone.FRIENDLY
    ai_auto_reply: bool = True
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
    ai_tone: AiTone | None = None
    ai_auto_reply: bool | None = None
    escalation_contact: str | None = Field(default=None, max_length=255)
    # Запись к мастерам (вне ТЗ, §22): часовой пояс расписания, AI-бронь, шаг сетки.
    timezone: str | None = Field(default=None, max_length=64)
    booking_enabled: bool | None = None
    slot_step_minutes: int | None = Field(default=None, ge=5, le=120)

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Неизвестный часовой пояс") from exc
        return value

    @model_validator(mode="after")
    def _required_fields_not_null(self) -> BusinessUpdate:
        """Обязательные поля можно менять, но не обнулять: иначе ошибка БД (500)."""
        for field in (
            "name",
            "ai_tone",
            "ai_auto_reply",
            "timezone",
            "booking_enabled",
            "slot_step_minutes",
        ):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} не может быть null")
        return self


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
    ai_tone: AiTone
    ai_auto_reply: bool
    escalation_contact: str | None
    status: BusinessStatus
    timezone: str = "Europe/Moscow"
    booking_enabled: bool = False
    slot_step_minutes: int = 30
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
# Системные логи (разделы 16, 17) — читаются в панели ADMIN (этап 6, GET /admin/logs)
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
    # Что именно получит клиент: ответ AI, шаблон при передаче менеджеру или ответ
    # движка записи; None — клиенту ничего не отправится (автоответы выключены).
    client_reply: str | None = None
    # Вне ТЗ (§22): итог движка записи (HOLD, OFFER, ASK_SERVICE, NO_SLOTS);
    # в проверке бронь не создаётся.
    booking: str | None = None


# --------------------------------------------------------------------------- #
# Интеграции каналов (этап 3). Секреты (токен бота, секрет webhook) в схемах
# ответа отсутствуют намеренно (раздел 16: не возвращать секреты интеграций).
# --------------------------------------------------------------------------- #
class TelegramConnectRequest(BaseModel):
    """SecretStr: токен не попадает в repr, логи и тело ошибки валидации 422."""

    # Без min/max_length: при нарушении ограничения FastAPI вернул бы введённое
    # значение в теле 422 (поле input). Формат проверяет сервис.
    bot_token: SecretStr


class VkConnectRequest(BaseModel):
    """Ключ доступа сообщества VK (этап 9). SecretStr — как у токена бота."""

    access_token: SecretStr


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
    delivery_error: str | None = None
    processing_status: ProcessingStatus | None
    author_user_id: int | None = None
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
    handled_by_manager: bool = False
    created_at: datetime
    updated_at: datetime


class LeadOut(ORMModel):
    """Лид (раздел 10): приоритет, причина классификации, ответственный."""

    id: int
    business_id: int
    conversation_id: int
    status: LeadStatus
    priority: LeadPriority
    intent: str | None
    reason: str | None
    assigned_to: int | None
    created_at: datetime
    updated_at: datetime


class ConversationListItem(BaseModel):
    conversation: ConversationOut
    customer: CustomerOut
    last_message: MessageOut | None = None
    lead: LeadOut | None = None


class ConversationDetail(BaseModel):
    conversation: ConversationOut
    customer: CustomerOut
    lead: LeadOut | None = None
    messages: list[MessageOut]
    ai_decisions: list[AiDecisionOut]


# --------------------------------------------------------------------------- #
# CRM-ядро (этап 4): лиды, ручной ответ, клиенты
# --------------------------------------------------------------------------- #
class LeadListItem(BaseModel):
    lead: LeadOut
    conversation: ConversationOut
    customer: CustomerOut


class LeadUpdate(BaseModel):
    """PATCH /leads/{id} (раздел 14). Передаются только меняемые поля;
    assigned_to=null снимает ответственного, отсутствие поля — не трогает."""

    status: LeadStatus | None = None
    assigned_to: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _status_not_null(self) -> LeadUpdate:
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("status не может быть null")
        return self


class ReplyRequest(BaseModel):
    """Ручной ответ менеджера клиенту (раздел 11: POST /conversations/{id}/reply)."""

    text: str = Field(min_length=1, max_length=4000)

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Текст ответа не может быть пустым")
        return value.strip()


class CustomerListItem(BaseModel):
    customer: CustomerOut
    conversations_count: int
    last_activity_at: datetime | None = None


class CustomerDetail(BaseModel):
    """Клиент и история его обращений (раздел 13: «Клиенты: история обращений»)."""

    customer: CustomerOut
    created_at: datetime
    conversations: list[ConversationListItem]


class ConversationState(BaseModel):
    """Диалог и его лид после изменения (например, «решено»)."""

    conversation: ConversationOut
    lead: LeadOut | None = None


# --------------------------------------------------------------------------- #
# Кабинет (этап 5): сотрудники, приглашения, аналитика (раздел 13)
# --------------------------------------------------------------------------- #
class MemberRoleUpdate(BaseModel):
    role: MemberRole


class InvitationCreate(_EmailMixin):
    role: MemberRole = MemberRole.MANAGER
    # Только для роли MASTER: мастер без аккаунта, к которому привязать сотрудника.
    master_id: int | None = Field(default=None, ge=1)


class InvitationOut(BaseModel):
    id: int
    email: EmailStr
    role: MemberRole
    expires_at: datetime
    created_at: datetime


class InvitationCreated(InvitationOut):
    """Ссылка показывается ОДИН раз: в БД хранится только хеш токена."""

    invite_url: str


class InvitationAccept(BaseModel):
    # SecretStr и без ограничений длины: значение не должно попасть в тело ошибки 422.
    token: SecretStr


class DailyPoint(BaseModel):
    date: str
    incoming: int
    ai: int
    manager: int


class AnalyticsOut(BaseModel):
    """Базовые показатели за период (раздел 13). Границы периода — UTC."""

    period_start: datetime
    period_end: datetime
    conversations_new: int
    messages_incoming: int
    customers_active: int
    manager_replies: int
    ai_answered: int
    ai_escalated: int
    ai_share_percent: int | None
    ai_avg_latency_ms: int | None
    ai_errors: int
    delivery_failed: int
    leads_total: int
    leads_by_priority: dict[str, int]
    leads_by_status: dict[str, int]
    intents: dict[str, int]
    daily: list[DailyPoint]


# --------------------------------------------------------------------------- #
# Административная панель (этап 6, раздел 15). Секретов интеграций здесь нет:
# только канал, статус и текст последней ошибки.
# --------------------------------------------------------------------------- #
class AdminIntegrationOut(BaseModel):
    channel: Channel
    status: IntegrationStatus
    external_account_name: str | None
    last_error: str | None
    created_at: datetime


class AdminBusinessItem(BaseModel):
    id: int
    name: str
    category: str | None
    status: BusinessStatus
    plan: SubscriptionPlan
    subscription_status: SubscriptionStatus
    expires_at: datetime | None
    trial_expired: bool
    created_at: datetime
    users_count: int
    messages_count: int
    last_activity_at: datetime | None
    integrations: list[AdminIntegrationOut]


class AdminBusinessPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[AdminBusinessItem]


class AdminBusinessStatusUpdate(BaseModel):
    status: BusinessStatus


class AdminSubscriptionUpdate(BaseModel):
    """Ручное изменение тарифа и пробного периода (раздел 15).

    expires_on — последний день срока (trial или оплаченного периода);
    extend_days — продлить от текущего срока (или от сегодня, если срок истёк).
    """

    plan: SubscriptionPlan | None = None
    status: SubscriptionStatus | None = None
    expires_on: date | None = None
    extend_days: int | None = Field(default=None, ge=1, le=365)

    @model_validator(mode="after")
    def _validate(self) -> AdminSubscriptionUpdate:
        if not self.model_fields_set:
            raise ValueError("Нет полей для изменения")
        if self.expires_on is not None and self.extend_days is not None:
            raise ValueError("Укажите либо дату окончания, либо число дней продления")
        return self


class AdminMetrics(BaseModel):
    companies_total: int
    companies_active: int
    companies_trial: int
    companies_suspended: int
    trials_expired: int
    paid_subscriptions: int
    mrr_rub: int
    integrations_with_errors: int
    errors_24h: int
    messages_24h: int


class AdminLogItem(BaseModel):
    id: int
    business_id: int | None
    business_name: str | None
    level: LogLevel
    event_type: str
    message: str
    payload: dict | None
    created_at: datetime


class AdminLogPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[AdminLogItem]


# --------------------------------------------------------------------------- #
# Мастера, смены и записи (вне ТЗ, §22 «автоматическая запись»)
# --------------------------------------------------------------------------- #
class MasterCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    user_id: int | None = Field(default=None, ge=1)


class MasterUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    active: bool | None = None


class MasterServicesUpdate(BaseModel):
    service_ids: list[int] = Field(default_factory=list, max_length=500)


class MasterOut(ORMModel):
    id: int
    business_id: int
    user_id: int | None
    display_name: str
    active: bool
    notify_channel: Channel | None
    notify_linked: bool = False
    service_ids: list[int] = Field(default_factory=list)


class ShiftCreate(BaseModel):
    day: date
    start_time: time
    end_time: time


class ShiftUpdate(BaseModel):
    start_time: time
    end_time: time


class ShiftOut(ORMModel):
    id: int
    master_id: int
    day: date
    start_time: time
    end_time: time


class BookingCreate(BaseModel):
    """Ручная запись сотрудником: дата и время — в часовом поясе компании."""

    master_id: int = Field(ge=1)
    service_id: int = Field(ge=1)
    day: date
    start_time: time
    client_name: str = Field(min_length=1, max_length=255)
    comment: str | None = Field(default=None, max_length=1000)
    # Вне ТЗ (§22): запись по заявке клиента — привязка к его диалогу, клиенту
    # уходит «Готово, вы записаны», заявка закрывается.
    request_id: int | None = Field(default=None, ge=1)


class BookingReschedule(BaseModel):
    """Перенос записи сотрудником: новое время в поясе компании, мастер — по желанию."""

    day: date
    start_time: time
    master_id: int | None = Field(default=None, ge=1)


class BookingOut(ORMModel):
    id: int
    business_id: int
    master_id: int
    service_id: int | None
    customer_id: int | None
    conversation_id: int | None
    client_name: str
    starts_at: datetime
    ends_at: datetime
    status: BookingStatus
    source: BookingSource
    comment: str | None
    created_at: datetime


class SlotOut(BaseModel):
    master_id: int
    master_name: str
    starts_at: datetime
    ends_at: datetime
    local_start: datetime


class NotifyLinkRequest(BaseModel):
    channel: Channel


class NotifyLinkOut(BaseModel):
    """Код показывается ОДИН раз: в БД хранится только его хеш."""

    code: str
    link: str | None
    channel: Channel
    instruction: str
    expires_at: datetime
