"""
Модели БД — раздел 10 ТЗ.

Этап 1: users, businesses, business_members, services, system_logs.
Этап 3: integrations, customers, conversations, messages, ai_responses —
минимум, без которого нельзя сохранить входящее сообщение и ответ (раздел 18).
Этап 4: leads. Этап 5: invitations. Этап 6: subscriptions (тариф и пробный период, раздел 15).

Все ENUM объявлены как native_enum=False (VARCHAR + CHECK): одинаково работает
в SQLite на разработке и в PostgreSQL в production, миграция значений не требует
ALTER TYPE.
"""

from __future__ import annotations

import enum
from datetime import UTC, date, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    false,
    true,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


def utcnow() -> datetime:
    """Единая точка получения времени: всегда UTC и timezone-aware."""
    return datetime.now(UTC)


# JSON в SQLite, JSONB в PostgreSQL — для metadata системных логов.
JSONType = JSON().with_variant(JSONB(), "postgresql")


# --------------------------------------------------------------------------- #
# Перечисления (раздел 5, 10, 15 ТЗ)
# --------------------------------------------------------------------------- #
class UserRole(str, enum.Enum):
    """Роль пользователя на уровне платформы (раздел 5).

    ADMIN — владелец LeadPilot, доступ ко всем компаниям.
    OWNER / MANAGER — роль по умолчанию; фактические права на данные компании
    определяются записью в business_members, а не этим полем.
    """

    OWNER = "OWNER"
    MANAGER = "MANAGER"
    ADMIN = "ADMIN"


class UserStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class MemberRole(str, enum.Enum):
    """Роль пользователя внутри конкретной компании (раздел 5)."""

    OWNER = "OWNER"
    MANAGER = "MANAGER"
    # Вне ТЗ (§22 «автоматическая запись»): мастер видит и ведёт только своё
    # расписание и свои записи; остальные разделы кабинета ему закрыты.
    MASTER = "MASTER"


class BookingStatus(str, enum.Enum):
    """Статус записи клиента к мастеру. PENDING — бронь (слот занят),
    ждёт подтверждения менеджера или мастера."""

    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class BookingSource(str, enum.Enum):
    AI = "AI"  # бронь поставил ассистент по сообщению клиента
    STAFF = "STAFF"  # запись создал сотрудник в кабинете


class NotificationStatus(str, enum.Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class AiTone(str, enum.Enum):
    """Стиль ответов AI (раздел 13: «AI — правила, стиль ответа»)."""

    FRIENDLY = "FRIENDLY"  # тёплый, по-человечески
    FORMAL = "FORMAL"  # вежливо, на «вы», без сленга
    BRIEF = "BRIEF"  # максимально коротко и по делу


class BusinessStatus(str, enum.Enum):
    """Статус компании (раздел 15: active/trial/suspended)."""

    TRIAL = "TRIAL"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"


class LogLevel(str, enum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class SubscriptionPlan(str, enum.Enum):
    """Тариф компании (раздел 15). Цены — в настройках (config.plan_price_*)."""

    TRIAL = "TRIAL"
    START = "START"
    PRO = "PRO"


class SubscriptionStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    CANCELED = "CANCELED"


def enum_column(enum_cls: type[enum.Enum], **kwargs: Any):
    return mapped_column(
        SAEnum(enum_cls, native_enum=False, length=32, validate_strings=True),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# users
# --------------------------------------------------------------------------- #
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # email хранится в нижнем регистре (нормализация в schemas.py),
    # поэтому UNIQUE работает одинаково в SQLite и PostgreSQL.
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = enum_column(UserRole, nullable=False, default=UserRole.OWNER)
    status: Mapped[UserStatus] = enum_column(UserStatus, nullable=False, default=UserStatus.ACTIVE)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    memberships: Mapped[list[BusinessMember]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    owned_businesses: Mapped[list[Business]] = relationship(
        back_populates="owner", foreign_keys="Business.owner_id"
    )

    @property
    def is_platform_admin(self) -> bool:
        return self.role is UserRole.ADMIN

    def __repr__(self) -> str:  # pragma: no cover - диагностика
        return f"<User id={self.id} email={self.email} role={self.role.value}>"


# --------------------------------------------------------------------------- #
# businesses
# --------------------------------------------------------------------------- #
class Business(Base):
    __tablename__ = "businesses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str | None] = mapped_column(String(120))
    address: Mapped[str | None] = mapped_column(String(500))
    phone: Mapped[str | None] = mapped_column(String(50))
    working_hours: Mapped[str | None] = mapped_column(String(500))
    # Поля из раздела 6.2, отсутствующие в минимальном перечне раздела 10.
    description: Mapped[str | None] = mapped_column(Text)
    escalation_contact: Mapped[str | None] = mapped_column(String(255))
    # Правила поведения AI (раздел 6.2). Используются AI-модулем на этапе 2.
    ai_rules: Mapped[str | None] = mapped_column(Text)
    # Стиль и разрешённые действия AI (раздел 13). ai_auto_reply=False: AI только
    # классифицирует, а отвечает клиентам менеджер (разделы 6.7, 19).
    ai_tone: Mapped[AiTone] = enum_column(
        AiTone, nullable=False, default=AiTone.FRIENDLY, server_default=AiTone.FRIENDLY.value
    )
    ai_auto_reply: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    status: Mapped[BusinessStatus] = enum_column(
        BusinessStatus, nullable=False, default=BusinessStatus.TRIAL
    )
    # Запись к мастерам (вне ТЗ, §22): часовой пояс расписания, разрешение AI ставить
    # брони и шаг сетки свободного времени.
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Europe/Moscow", server_default="Europe/Moscow"
    )
    booking_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    slot_step_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    owner: Mapped[User] = relationship(back_populates="owned_businesses", foreign_keys=[owner_id])
    members: Mapped[list[BusinessMember]] = relationship(
        back_populates="business", cascade="all, delete-orphan"
    )
    services: Mapped[list[Service]] = relationship(
        back_populates="business", cascade="all, delete-orphan"
    )
    subscription: Mapped[Subscription | None] = relationship(
        back_populates="business", cascade="all, delete-orphan", uselist=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Business id={self.id} name={self.name!r}>"


# --------------------------------------------------------------------------- #
# business_members — связь пользователь ↔ компания (раздел 6.1)
# --------------------------------------------------------------------------- #
class BusinessMember(Base):
    __tablename__ = "business_members"
    __table_args__ = (
        # Один пользователь — одна роль в одной компании.
        UniqueConstraint("business_id", "user_id", name="uq_business_members_business_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[MemberRole] = enum_column(MemberRole, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    business: Mapped[Business] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BusinessMember business_id={self.business_id} user_id={self.user_id} role={self.role.value}>"


# --------------------------------------------------------------------------- #
# services (раздел 6.3)
# --------------------------------------------------------------------------- #
class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Деньги — Numeric, а не float: AI на этапе 2 обязан отдавать клиенту
    # точную цену из БД (раздел 6.6).
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    duration: Mapped[int | None] = mapped_column(Integer)  # минуты, опционально
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    business: Mapped[Business] = relationship(back_populates="services")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Service id={self.id} business_id={self.business_id} name={self.name!r}>"


# --------------------------------------------------------------------------- #
# subscriptions — тариф и пробный период (разделы 10, 15). Этап 6
# --------------------------------------------------------------------------- #
class Subscription(Base):
    """Текущая подписка компании: одна запись на компанию (UNIQUE business_id).

    MVP без биллинга (раздел 19): тариф и срок меняет ADMIN вручную; история
    изменений — события ADMIN_SUBSCRIPTION_CHANGED в system_logs. Для пробного
    периода expires_at — конец trial.
    """

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    plan: Mapped[SubscriptionPlan] = enum_column(
        SubscriptionPlan, nullable=False, default=SubscriptionPlan.TRIAL
    )
    status: Mapped[SubscriptionStatus] = enum_column(
        SubscriptionStatus, nullable=False, default=SubscriptionStatus.ACTIVE
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    business: Mapped[Business] = relationship(back_populates="subscription")


class RateLimitCounter(Base):
    """Счётчик ограничения частоты запросов (раздел 16, этап 9).

    Фиксированное окно в общей БД: лимит один на все воркеры и реплики.
    key — «область:идентификатор» (например, auth:login:1.2.3.4),
    window_start — начало окна в секундах Unix.
    """

    __tablename__ = "rate_limit_counters"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    window_start: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


# --------------------------------------------------------------------------- #
# system_logs (разделы 16, 17)
# --------------------------------------------------------------------------- #
class SystemLog(Base):
    __tablename__ = "system_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # NULL для событий уровня платформы (регистрация, неудачный логин).
    business_id: Mapped[int | None] = mapped_column(
        ForeignKey("businesses.id", ondelete="SET NULL"), index=True
    )
    level: Mapped[LogLevel] = enum_column(LogLevel, nullable=False, default=LogLevel.INFO)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    # Поле БД называется metadata (раздел 10), в Python — payload:
    # имя `metadata` зарезервировано declarative-базой SQLAlchemy.
    payload: Mapped[dict | None] = mapped_column("metadata", JSONType)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SystemLog id={self.id} event_type={self.event_type}>"


# --------------------------------------------------------------------------- #
# Этап 3: каналы, клиенты, диалоги, сообщения (разделы 6.4, 10, 11, 18)
# --------------------------------------------------------------------------- #
class Channel(str, enum.Enum):
    """Канал коммуникации. MVP — Telegram (раздел 1); этап 9 — VK (сообщения
    сообщества). Новый канал = значение enum + модуль в integrations/ по
    контракту integrations/base.py, без правок ядра. Колонки channel — VARCHAR
    без CHECK, поэтому новое значение не требует миграции."""

    TELEGRAM = "TELEGRAM"
    VK = "VK"


class IntegrationStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    ERROR = "ERROR"  # webhook не удалось зарегистрировать


class ConversationStatus(str, enum.Enum):
    """Статус диалога (раздел 14: «требует внимания», «решено»)."""

    OPEN = "OPEN"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    RESOLVED = "RESOLVED"


class LeadPriority(str, enum.Enum):
    """Приоритет обращения (раздел 6.5). Общий для диалогов и лидов (этап 4)."""

    HOT = "HOT"
    WARM = "WARM"
    COLD = "COLD"


class LeadStatus(str, enum.Enum):
    """Статус лида (раздел 14: «изменение статуса лида», «отметка решено»)."""

    NEW = "NEW"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"  # решено
    LOST = "LOST"  # клиент не дошёл до услуги

    @property
    def is_closed(self) -> bool:
        return self in (LeadStatus.RESOLVED, LeadStatus.LOST)


class SenderType(str, enum.Enum):
    """Отправитель сообщения (раздел 10: messages.sender_type)."""

    CUSTOMER = "CUSTOMER"
    AI = "AI"
    MANAGER = "MANAGER"


class ProcessingStatus(str, enum.Enum):
    """Обработка входящего сообщения AI-pipeline (раздел 18: повторная обработка)."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    FAILED = "FAILED"


class DeliveryStatus(str, enum.Enum):
    """Доставка исходящего сообщения клиенту (раздел 6.4: статус сообщения)."""

    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class AiResponseStatus(str, enum.Enum):
    PENDING = "PENDING"  # ответ подготовлен и сохранён, доставка не завершена
    SENT = "SENT"  # ответ AI доставлен клиенту
    FAILED = "FAILED"  # ответ подготовлен, но доставить не удалось
    ESCALATED = "ESCALATED"  # передано менеджеру без ответа AI
    BLOCKED = "BLOCKED"  # ответ не прошёл валидатор и клиенту не отправлялся


class Integration(Base):
    """Подключение канала компании (раздел 10: integrations).

    credentials_ref — ссылка/шифртекст токена бота, а не открытый секрет
    (services/secret_store.py). Секрет webhook хранится только как SHA-256.
    """

    __tablename__ = "integrations"
    __table_args__ = (
        UniqueConstraint("business_id", "channel", name="uq_integrations_business_channel"),
        UniqueConstraint("channel", "external_account_id", name="uq_integrations_channel_account"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[Channel] = enum_column(Channel, nullable=False)
    credentials_ref: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[IntegrationStatus] = enum_column(
        IntegrationStatus, nullable=False, default=IntegrationStatus.ACTIVE
    )
    webhook_secret_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    external_account_id: Mapped[str | None] = mapped_column(String(64))
    external_account_name: Mapped[str | None] = mapped_column(String(120))
    # Этап 9: несекретные параметры канала (VK: код подтверждения и id сервера
    # Callback API). Токены и секреты сюда не кладутся (раздел 16).
    channel_settings: Mapped[dict | None] = mapped_column(JSONType)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class Customer(Base):
    """Клиент компании в конкретном канале (раздел 10: customers).

    channel добавлен к полям раздела 10: external_id (chat_id Telegram, номер
    WhatsApp) уникален только внутри канала.
    """

    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "channel", "external_id", name="uq_customers_business_channel_external"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[Channel] = enum_column(Channel, nullable=False)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(50))
    # Клиент заблокировал бота (Telegram 403): отправка бессмысленна, нужен менеджер.
    channel_blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[Channel] = enum_column(Channel, nullable=False)
    status: Mapped[ConversationStatus] = enum_column(
        ConversationStatus, nullable=False, default=ConversationStatus.OPEN, index=True
    )
    priority: Mapped[LeadPriority] = enum_column(
        LeadPriority, nullable=False, default=LeadPriority.COLD, index=True
    )
    # Почему диалог требует внимания (EscalationReason либо DELIVERY_FAILED и т.п.).
    attention_reason: Mapped[str | None] = mapped_column(String(64))
    # Менеджер вмешался вручную: AI больше не отвечает клиенту в этом диалоге
    # до отметки «решено» (раздел 14, раздел 19: без полностью автономного AI).
    handled_by_manager: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow, index=True
    )

    customer: Mapped[Customer] = relationship()
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", order_by="Message.id", cascade="all, delete-orphan"
    )


class Message(Base):
    """Сообщение диалога (раздел 10: messages).

    business_id добавлен к полям раздела 10, чтобы любой запрос к сообщениям
    фильтровался по компании напрямую (раздел 16), а не только через диалог.
    Идемпотентность webhook: UNIQUE(conversation_id, sender_type, external_message_id).
    """

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id",
            "sender_type",
            "external_message_id",
            name="uq_messages_conversation_sender_external",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sender_type: Mapped[SenderType] = enum_column(SenderType, nullable=False)
    external_message_id: Mapped[str | None] = mapped_column(String(128))
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # "text" | "attachment": вложения AI не видит, такое сообщение решает человек.
    content_type: Mapped[str] = mapped_column(String(20), nullable=False, default="text")
    intent: Mapped[str | None] = mapped_column(String(32))
    # Кто из сотрудников написал ответ (sender_type = MANAGER), раздел 16: аудит.
    author_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )

    # Обработка входящих (только CUSTOMER).
    processing_status: Mapped[ProcessingStatus | None] = enum_column(
        ProcessingStatus, nullable=True, index=True
    )
    processing_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_error: Mapped[str | None] = mapped_column(Text)

    # Доставка исходящих (AI и MANAGER).
    delivery_status: Mapped[DeliveryStatus | None] = enum_column(DeliveryStatus, nullable=True)
    delivery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delivery_error: Mapped[str | None] = mapped_column(Text)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class AiResponse(Base):
    """Решение AI по входящему сообщению (раздел 10: ai_responses).

    По этой записи и system_logs видно, ПОЧЕМУ клиенту ушёл именно такой ответ
    (раздел 17). Поля decision/escalation_reason/details добавлены к разделу 10.
    """

    __tablename__ = "ai_responses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # Исходящее сообщение, доставленное клиенту (ответ AI либо безопасный ответ).
    response_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(64))
    response_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[AiResponseStatus] = enum_column(AiResponseStatus, nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    escalation_reason: Mapped[str | None] = mapped_column(String(32))
    details: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


# --------------------------------------------------------------------------- #
# Этап 4: лиды (разделы 6.5, 10, 11, 14)
# --------------------------------------------------------------------------- #
class Lead(Base):
    """Лид — обращение, которым занимается менеджер (раздел 10: leads).

    Один лид на диалог (UNIQUE). business_id и поля intent/updated_at добавлены
    к разделу 10: фильтрация по компании напрямую (раздел 16) и история изменений.
    reason хранит причину классификации (раздел 6.5).
    """

    __tablename__ = "leads"
    __table_args__ = (UniqueConstraint("conversation_id", name="uq_leads_conversation"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[LeadStatus] = enum_column(
        LeadStatus, nullable=False, default=LeadStatus.NEW, index=True
    )
    priority: Mapped[LeadPriority] = enum_column(
        LeadPriority, nullable=False, default=LeadPriority.COLD, index=True
    )
    intent: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    # Ответственный менеджер. Должен быть участником этой же компании.
    assigned_to: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


# --------------------------------------------------------------------------- #
# Этап 5: приглашения сотрудников (раздел 13: «Сотрудники — приглашение менеджеров»)
# --------------------------------------------------------------------------- #
class Invitation(Base):
    """Приглашение в компанию по одноразовой ссылке.

    Писем система не шлёт (почтовой инфраструктуры в MVP нет): владелец получает
    ссылку один раз и передаёт её сам. Токен хранится только как SHA-256, принять
    приглашение может пользователь с тем же email (раздел 16).
    """

    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    role: Mapped[MemberRole] = enum_column(MemberRole, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------- #
# Мастера, смены и записи (вне ТЗ, §22 «интеграция с календарями, автоматическая запись»)
# --------------------------------------------------------------------------- #
class Master(Base):
    """Мастер компании. user_id — участник с ролью MASTER; без аккаунта (NULL)
    расписание мастера ведёт владелец. Уведомления о записях мастер получает
    от бота/сообщества компании в выбранном канале (notify_*)."""

    __tablename__ = "masters"
    __table_args__ = (UniqueConstraint("business_id", "user_id", name="uq_masters_business_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notify_channel: Mapped[Channel | None] = enum_column(Channel)
    notify_chat_id: Mapped[str | None] = mapped_column(String(64))
    # Одноразовый код привязки уведомлений: только SHA-256 и срок (раздел 16).
    notify_code_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    notify_code_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class MasterService(Base):
    """Какие услуги выполняет мастер (AI предлагает только подходящих мастеров)."""

    __tablename__ = "master_services"
    __table_args__ = (UniqueConstraint("master_id", "service_id", name="uq_master_services_pair"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    master_id: Mapped[int] = mapped_column(
        ForeignKey("masters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), nullable=False, index=True
    )


class MasterShift(Base):
    """Рабочий интервал мастера на конкретную дату (время — в зоне компании).
    В один день может быть несколько интервалов (перерыв между ними)."""

    __tablename__ = "master_shifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    master_id: Mapped[int] = mapped_column(
        ForeignKey("masters.id", ondelete="CASCADE"), nullable=False
    )
    day: Mapped[date] = mapped_column(Date, nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


Index("ix_master_shifts_business_day", MasterShift.business_id, MasterShift.day)
Index("ix_master_shifts_master_day", MasterShift.master_id, MasterShift.day)


class Booking(Base):
    """Запись клиента к мастеру. Время — UTC; активные статусы (PENDING, CONFIRMED)
    занимают интервал мастера: пересечение проверяется под блокировкой строки мастера."""

    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    master_id: Mapped[int] = mapped_column(
        ForeignKey("masters.id", ondelete="CASCADE"), nullable=False
    )
    service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id", ondelete="SET NULL"))
    customer_id: Mapped[int | None] = mapped_column(
        ForeignKey("customers.id", ondelete="SET NULL"), index=True
    )
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL")
    )
    client_name: Mapped[str] = mapped_column(String(255), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[BookingStatus] = enum_column(
        BookingStatus, nullable=False, default=BookingStatus.PENDING
    )
    source: Mapped[BookingSource] = enum_column(BookingSource, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


Index("ix_bookings_business_starts", Booking.business_id, Booking.starts_at)
Index("ix_bookings_master_starts", Booking.master_id, Booking.starts_at)


class MasterNotification(Base):
    """Outbox уведомлений мастеру: сначала запись, потом отправка (раздел 18);
    неотправленные повторяет фоновый цикл."""

    __tablename__ = "master_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_id: Mapped[int] = mapped_column(
        ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False
    )
    master_id: Mapped[int] = mapped_column(
        ForeignKey("masters.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[Channel] = enum_column(Channel, nullable=False)
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[NotificationStatus] = enum_column(
        NotificationStatus, nullable=False, default=NotificationStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


Index("ix_master_notifications_status", MasterNotification.status)
