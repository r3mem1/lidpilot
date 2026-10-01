"""
Приём и хранение сообщений, запуск AI-pipeline, доставка ответов —
разделы 6.4, 12.1, 17, 18 и Приложение B ТЗ.

Поток (Приложение B):

    webhook → receive_incoming()        сохранить сообщение (идемпотентно), commit
            → process_incoming_message  AI-pipeline → ответ/эскалация → доставка

Инварианты (CLAUDE.md, разделы 18, 21):
* СНАЧАЛА сохраняем входящее и только потом обращаемся к внешним API; исходящее
  тоже сохраняется (delivery_status=PENDING) до отправки в Telegram;
* повторная доставка одного и того же webhook безопасна: UNIQUE в БД + разбор
  IntegrityError, а повторная обработка одного сообщения защищена атомарным
  «захватом» (UPDATE … WHERE статус подходит);
* любой сбой оставляет сообщение в состоянии, из которого его подхватит
  reprocess_pending(); после исчерпания попыток диалог помечается «требует
  внимания» — сообщение не теряется молча (критерий приёмки 15);
* на каждом шаге пишется system_logs с message_id / conversation_id — по логам
  видно, что случилось с конкретным сообщением и почему ушёл именно такой ответ.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from ai.booking import BookingKind, format_when
from ai.context import HistoryRole, HistoryTurn
from ai.pipeline import (
    REPLY_RECEIVED,
    REPLY_REPEAT,
    REPLY_STAFF_WILL_ANSWER,
    Decision,
    EscalationReason,
)
from config import settings
from database import SessionLocal
from integrations.base import ChannelSendError, IncomingMessage
from models import (
    AiResponse,
    AiResponseStatus,
    Booking,
    Business,
    BusinessStatus,
    Channel,
    Conversation,
    ConversationStatus,
    Customer,
    DeliveryStatus,
    Integration,
    Lead,
    LeadPriority,
    LogLevel,
    Master,
    Message,
    ProcessingStatus,
    SenderType,
    Service,
    utcnow,
)
from services import (
    ai_service,
    audit_service,
    booking_ai_provider,
    booking_request_service,
    booking_service,
    integration_service,
    lead_service,
    schedule_service,
    secret_store,
    subscription_service,
)
from services.access_service import BusinessContext

logger = logging.getLogger("leadpilot.messages")

# Свежие сообщения обрабатывает фоновая задача webhook; sweeper берёт только те,
# что «застряли» дольше этого времени.
_GRACE_SECONDS = 30

# Отметка сообщения, ответ на которое дан вместе со следующим сообщением серии.
MERGED_NOTE = "Объединено со следующим сообщением клиента"


@dataclass(frozen=True)
class ReceiveResult:
    message_id: int
    conversation_id: int
    customer_id: int
    duplicate: bool


def _log(
    db: Session,
    event_type: str,
    message: str,
    *,
    business_id: int,
    level: LogLevel = LogLevel.INFO,
    payload: dict | None = None,
    commit: bool = False,
) -> None:
    audit_service.log_event(
        db,
        event_type=event_type,
        message=message,
        level=level,
        business_id=business_id,
        payload=payload,
        commit=commit,
    )


# --------------------------------------------------------------------------- #
# Приём входящего сообщения (быстрая часть webhook)
# --------------------------------------------------------------------------- #
def _get_or_create_customer(
    db: Session, business_id: int, channel: Channel, incoming: IncomingMessage
) -> Customer:
    def find() -> Customer | None:
        return db.scalar(
            select(Customer).where(
                Customer.business_id == business_id,
                Customer.channel == channel,
                Customer.external_id == incoming.external_chat_id,
            )
        )

    customer = find()
    if customer is None:
        customer = Customer(
            business_id=business_id,
            channel=channel,
            external_id=incoming.external_chat_id,
            name=incoming.sender_name,
            username=incoming.sender_username,
        )
        db.add(customer)
        try:
            db.flush()
        except IntegrityError:
            # Два первых сообщения клиента пришли одновременно. Транзакция только
            # началась, откат ничего не теряет.
            db.rollback()
            customer = find()
            if customer is None:
                raise
        return customer

    # Имя и username в Telegram меняются — храним актуальные.
    if incoming.sender_name and customer.name != incoming.sender_name:
        customer.name = incoming.sender_name
    if incoming.sender_username and customer.username != incoming.sender_username:
        customer.username = incoming.sender_username
    customer.channel_blocked = False  # клиент снова пишет — значит, бота разблокировал
    return customer


def _get_or_create_conversation(
    db: Session, business_id: int, customer: Customer, channel: Channel
) -> Conversation:
    """Диалог клиента: последний незакрытый, иначе новый (после «решено»
    новое обращение открывает новый диалог)."""
    conversation = db.scalar(
        select(Conversation)
        .where(Conversation.customer_id == customer.id, Conversation.business_id == business_id)
        .order_by(Conversation.id.desc())
        .limit(1)
    )
    if conversation is None or conversation.status is ConversationStatus.RESOLVED:
        conversation = Conversation(
            business_id=business_id, customer_id=customer.id, channel=channel
        )
        db.add(conversation)
        db.flush()
    return conversation


def receive_incoming(
    db: Session, integration: Integration, incoming: IncomingMessage
) -> ReceiveResult:
    """Сохранить входящее сообщение. Возвращается, когда оно уже в БД (commit).

    Дубликат (Telegram повторил webhook) не создаёт второй записи и не
    запускает повторную отправку ответа.
    """
    business_id = integration.business_id
    channel = integration.channel

    customer = _get_or_create_customer(db, business_id, channel, incoming)
    conversation = _get_or_create_conversation(db, business_id, customer, channel)
    customer_id, conversation_id = customer.id, conversation.id

    message = Message(
        business_id=business_id,
        conversation_id=conversation_id,
        sender_type=SenderType.CUSTOMER,
        external_message_id=incoming.external_message_id,
        text=incoming.text,
        content_type=incoming.content_type,
        processing_status=ProcessingStatus.PENDING,
    )
    db.add(message)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing_id = db.scalar(
            select(Message.id).where(
                Message.conversation_id == conversation_id,
                Message.sender_type == SenderType.CUSTOMER,
                Message.external_message_id == incoming.external_message_id,
            )
        )
        if existing_id is None:
            raise
        _log(
            db,
            audit_service.EventType.MESSAGE_DUPLICATE,
            "Повторная доставка webhook: сообщение уже сохранено",
            business_id=business_id,
            payload={
                "message_id": existing_id,
                "conversation_id": conversation_id,
                "external_message_id": incoming.external_message_id,
                "update_id": incoming.update_id,
            },
            commit=True,
        )
        return ReceiveResult(existing_id, conversation_id, customer_id, duplicate=True)

    conversation.updated_at = utcnow()
    _log(
        db,
        audit_service.EventType.WEBHOOK_RECEIVED,
        "Получено сообщение клиента",
        business_id=business_id,
        payload={
            "message_id": message.id,
            "conversation_id": conversation_id,
            "customer_id": customer_id,
            "external_message_id": incoming.external_message_id,
            "update_id": incoming.update_id,
            "channel": channel.value,
            "content_type": incoming.content_type,
        },
    )
    db.commit()
    return ReceiveResult(message.id, conversation_id, customer_id, duplicate=False)


# --------------------------------------------------------------------------- #
# Обработка сообщения AI
# --------------------------------------------------------------------------- #
def process_incoming_message(message_id: int, *, debounce: bool = False) -> None:
    """Обработать сохранённое сообщение (фоновая задача webhook и sweeper).

    Собственная сессия: вызывается после ответа Telegram, когда сессия
    запроса уже закрыта. Исключения наружу не выходят — они фиксируются
    в сообщении и в system_logs.

    debounce=True (webhook): пауза reply_debounce_seconds, чтобы серия сообщений
    клиента получила один ответ — ранние сообщения серии увидят более новое и
    передадут ответ ему (решение 2026-09-27).
    """
    if debounce and settings.reply_debounce_seconds > 0:
        time.sleep(settings.reply_debounce_seconds)
    with SessionLocal() as db:
        if not _claim(db, message_id):
            return  # уже обработано или обрабатывается другим воркером
        try:
            _run(db, message_id)
        except Exception as exc:  # noqa: BLE001 - любой сбой = повторная обработка
            db.rollback()
            _mark_failed(db, message_id, exc)


def _claim(db: Session, message_id: int) -> bool:
    """Атомарный захват сообщения: только один воркер получает rowcount == 1."""
    now = utcnow()
    stale = now - timedelta(seconds=settings.message_processing_timeout_seconds)
    result = db.execute(
        update(Message)
        .where(
            Message.id == message_id,
            Message.sender_type == SenderType.CUSTOMER,
            Message.processing_attempts < settings.message_max_attempts,
            or_(
                Message.processing_status.in_([ProcessingStatus.PENDING, ProcessingStatus.FAILED]),
                and_(
                    Message.processing_status == ProcessingStatus.PROCESSING,
                    Message.processing_started_at < stale,
                ),
            ),
        )
        .values(
            processing_status=ProcessingStatus.PROCESSING,
            processing_attempts=Message.processing_attempts + 1,
            processing_started_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return cast(CursorResult, result).rowcount == 1


def _mark_failed(db: Session, message_id: int, exc: Exception) -> None:
    message = db.get(Message, message_id)
    if message is None:
        return
    message.processing_status = ProcessingStatus.FAILED
    message.processing_error = f"{type(exc).__name__}: {exc}"[:500]
    exhausted = message.processing_attempts >= settings.message_max_attempts
    conversation = db.get(Conversation, message.conversation_id)
    reply_needed = False
    if exhausted and conversation is not None:
        conversation.status = ConversationStatus.NEEDS_ATTENTION
        conversation.attention_reason = "PROCESSING_FAILED"
        reply_needed = not _has_reply_after(db, message)
    _log(
        db,
        audit_service.EventType.MESSAGE_PROCESSING_FAILED,
        "Не удалось обработать сообщение"
        + (": попытки исчерпаны, диалог передан менеджеру" if exhausted else ", будет повтор"),
        business_id=message.business_id,
        level=LogLevel.ERROR,
        payload={
            "message_id": message.id,
            "conversation_id": message.conversation_id,
            "attempt": message.processing_attempts,
            "max_attempts": settings.message_max_attempts,
            "error": message.processing_error,
        },
    )
    db.commit()
    logger.error("Сообщение %s: сбой обработки (%s)", message_id, message.processing_error)
    # Попытки исчерпаны — клиент всё равно получает ответ (решение 2026-09-27).
    if reply_needed and conversation is not None:
        try:
            _auto_reply(
                db,
                conversation,
                REPLY_RECEIVED,
                reason="PROCESSING_FAILED",
                payload={"message_id": message_id, "conversation_id": conversation.id},
            )
        except Exception:  # noqa: BLE001 - сбой шаблона подхватит контроль ответа
            db.rollback()
            logger.exception("Сообщение %s: не удалось отправить шаблонный ответ", message_id)


def _has_reply_after(db: Session, message: Message) -> bool:
    """После сообщения клиента в диалоге уже есть ответ (AI или менеджера)."""
    return (
        db.scalar(
            select(Message.id)
            .where(
                Message.conversation_id == message.conversation_id,
                Message.id > message.id,
                Message.sender_type != SenderType.CUSTOMER,
            )
            .limit(1)
        )
        is not None
    )


def _dedupe_template(db: Session, conversation_id: int, text: str) -> str:
    """Тот же шаблон уже уходил клиенту (после последнего ответа менеджера) —
    вместо повтора короткое «администратор уже видит ваш запрос»."""
    rows = db.scalars(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.sender_type != SenderType.CUSTOMER,
        )
        .order_by(Message.id.desc())
        .limit(10)
    ).all()
    for row in rows:
        if row.sender_type is SenderType.MANAGER:
            return text
        if row.text == REPLY_REPEAT:
            continue
        return REPLY_REPEAT if row.text == text else text
    return text


def _auto_reply(
    db: Session,
    conversation: Conversation,
    text: str,
    *,
    reason: str,
    payload: dict,
) -> DeliveryStatus | None:
    """Шаблонный ответ клиенту без вызова AI (компания приостановлена, срок
    подписки, сбой обработки, контроль ответа). Молчит, только если владелец
    выключил автоответы или диалог ведёт менеджер (решение 2026-09-27)."""
    business = db.get_one(Business, conversation.business_id)
    if not business.ai_auto_reply or conversation.handled_by_manager:
        return None
    text = _dedupe_template(db, conversation.id, text)
    outgoing = Message(
        business_id=business.id,
        conversation_id=conversation.id,
        sender_type=SenderType.AI,
        text=text,
        delivery_status=DeliveryStatus.PENDING,
    )
    db.add(outgoing)
    db.flush()
    outgoing_id = outgoing.id
    _log(
        db,
        audit_service.EventType.AUTO_REPLY_TEMPLATE,
        "Клиенту отправлен шаблонный ответ без AI",
        business_id=business.id,
        payload={**payload, "reason": reason, "outgoing_message_id": outgoing_id, "text": text},
    )
    db.commit()  # ответ сохранён ДО обращения к каналу (раздел 18)
    return deliver_outgoing(db, outgoing_id)


def _merged_batch(db: Session, message: Message) -> list[Message]:
    """Сообщения серии, ответ на которые передан этому: подряд идущие перед ним
    сообщения клиента с отметкой MERGED_NOTE (от старых к новым)."""
    rows = db.scalars(
        select(Message)
        .where(Message.conversation_id == message.conversation_id, Message.id < message.id)
        .order_by(Message.id.desc())
        .limit(20)
    ).all()
    batch: list[Message] = []
    for row in rows:
        if row.sender_type is not SenderType.CUSTOMER or row.processing_error != MERGED_NOTE:
            break
        batch.append(row)
    return list(reversed(batch))


def _load_history(db: Session, conversation_id: int, before_message_id: int) -> list[HistoryTurn]:
    """Последние сообщения диалога до текущего (раздел 6.6). Недоставленные
    ответы в историю не попадают: клиент их не видел."""
    rows = db.scalars(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.id < before_message_id,
            Message.text != "",
            or_(
                Message.delivery_status.is_(None), Message.delivery_status != DeliveryStatus.FAILED
            ),
        )
        .order_by(Message.id.desc())
        .limit(max(settings.ai_history_turns, 1))
    ).all()
    return [
        HistoryTurn(role=HistoryRole(row.sender_type.value), text=row.text)
        for row in reversed(rows)
    ]


def _run(db: Session, message_id: int) -> None:
    message = db.get(Message, message_id)
    if message is None:
        return
    conversation = db.get(Conversation, message.conversation_id)
    business = db.get(Business, message.business_id)
    if conversation is None or business is None:
        raise RuntimeError("Диалог или компания сообщения не найдены")

    business_id, conversation_id = business.id, conversation.id
    log_context = {
        "message_id": message.id,
        "conversation_id": conversation_id,
        "customer_id": conversation.customer_id,
        "external_message_id": message.external_message_id,
        "attempt": message.processing_attempts,
    }

    # Повторный запуск после сбоя между сохранением решения и доставкой:
    # AI второй раз не вызываем (иначе клиент получил бы два разных ответа).
    existing = db.scalar(select(AiResponse).where(AiResponse.message_id == message.id))
    if existing is not None:
        if existing.response_message_id is not None:
            deliver_outgoing(db, existing.response_message_id)
        message.processing_status = ProcessingStatus.DONE
        message.processing_error = None
        db.commit()
        return

    # Клиент пишет серией: пока ждали паузу, пришло следующее сообщение — ответ
    # на всю серию даст обработка последнего (решение 2026-09-27).
    newer = db.scalar(
        select(Message.id)
        .where(
            Message.conversation_id == conversation_id,
            Message.sender_type == SenderType.CUSTOMER,
            Message.id > message.id,
        )
        .limit(1)
    )
    if newer is not None:
        message.processing_status = ProcessingStatus.DONE
        message.processing_error = MERGED_NOTE
        _log(
            db,
            audit_service.EventType.MESSAGE_MERGED,
            "Сообщение серии: ответ будет дан вместе со следующим сообщением клиента",
            business_id=business_id,
            payload={**log_context, "next_message_id": newer},
        )
        db.commit()
        return

    # Заблокированная компания: AI не запускается (раздел 15: suspended), диалог
    # виден менеджеру, клиент получает шаблон «сотрудник ответит».
    if business.status is BusinessStatus.SUSPENDED:
        conversation.status = ConversationStatus.NEEDS_ATTENTION
        conversation.attention_reason = "BUSINESS_SUSPENDED"
        message.processing_status = ProcessingStatus.DONE
        message.processing_error = "Компания приостановлена"
        _log(
            db,
            audit_service.EventType.MESSAGE_SKIPPED,
            "Компания приостановлена: сообщение сохранено, AI не запускался",
            business_id=business_id,
            level=LogLevel.WARNING,
            payload=log_context,
        )
        db.commit()
        _auto_reply(
            db,
            conversation,
            REPLY_STAFF_WILL_ANSWER,
            reason="BUSINESS_SUSPENDED",
            payload=log_context,
        )
        return

    # Срок trial или оплаченного периода истёк (этап 8): как при suspended —
    # AI не вызывается, диалог у менеджера, клиенту — шаблон «сотрудник ответит».
    if not subscription_service.ai_allowed(db, business_id):
        conversation.status = ConversationStatus.NEEDS_ATTENTION
        conversation.attention_reason = "SUBSCRIPTION_EXPIRED"
        message.processing_status = ProcessingStatus.DONE
        message.processing_error = "Срок подписки истёк"
        _log(
            db,
            audit_service.EventType.MESSAGE_SKIPPED,
            "Срок подписки истёк: сообщение сохранено, AI не запускался",
            business_id=business_id,
            level=LogLevel.WARNING,
            payload=log_context,
        )
        db.commit()
        _auto_reply(
            db,
            conversation,
            REPLY_STAFF_WILL_ANSWER,
            reason="SUBSCRIPTION_EXPIRED",
            payload=log_context,
        )
        return

    # Диалог ведёт менеджер (он уже отвечал клиенту вручную): AI молчит, чтобы не
    # перебивать человека (раздел 14). Сообщение сохранено, менеджер уведомлён
    # статусом «требует внимания» до отметки «решено».
    if conversation.handled_by_manager:
        conversation.status = ConversationStatus.NEEDS_ATTENTION
        conversation.attention_reason = "CUSTOMER_REPLIED"
        message.processing_status = ProcessingStatus.DONE
        message.processing_error = "Диалог ведёт менеджер"
        _log(
            db,
            audit_service.EventType.MESSAGE_SKIPPED,
            "Диалог ведёт менеджер: AI не отвечает, сообщение ждёт менеджера",
            business_id=business_id,
            level=LogLevel.INFO,
            payload={**log_context, "reason": "MANAGER_HANDLING"},
        )
        db.commit()
        return

    # Серия сообщений отвечается одним ответом: AI видит их текст целиком,
    # а история диалога берётся до начала серии.
    batch = [*_merged_batch(db, message), message]
    if len(batch) > 1:
        log_context["merged_message_ids"] = [m.id for m in batch[:-1]]
    history = _load_history(db, conversation_id, batch[0].id)
    ai_text = "\n".join(
        m.text for m in batch if m.content_type != "attachment" and (m.text or "").strip()
    )
    # Вне ТЗ (§22): расписание мастеров для AI-записи (None — запись выключена).
    customer = db.get_one(Customer, conversation.customer_id)
    schedule = booking_ai_provider.for_conversation(
        db,
        business,
        conversation_id=conversation_id,
        customer_id=customer.id,
        client_name=_client_name(customer),
    )

    # process_message освобождает транзакцию перед обращением к LLM (commit),
    # поэтому объекты ниже перечитываются из БД.
    result = ai_service.process_message(
        db, business, ai_text, history, log_context=log_context, schedule=schedule
    )

    message = db.get(Message, message_id)
    conversation = db.get(Conversation, conversation_id)
    if message is None or conversation is None:
        raise RuntimeError("Сообщение или диалог исчезли во время обработки")

    classification = result.classification
    message.intent = classification.intent.value
    new_priority = LeadPriority(classification.priority.value)
    # Внутри открытого диалога приоритет не падает: «горячая» запись остаётся горячей.
    conversation.priority = lead_service.max_priority(conversation.priority, new_priority)
    lead_service.register_classification(
        db,
        conversation,
        intent=classification.intent.value,
        priority=new_priority,
        reason=classification.reason,
        message_id=message.id,
    )
    # Вне ТЗ (§22): AI понял просьбу о записи, но бронь не поставил (нет расписания
    # или окон) — заявка видна администратору в «Записях».
    if result.booking_request is not None:
        booking_request_service.save_from_ai(
            db,
            business_id=business_id,
            conversation=conversation,
            client_name=_client_name(customer),
            draft=result.booking_request,
            text=ai_text,
            message_id=message.id,
        )

    escalated = result.decision is Decision.ESCALATE
    if escalated:
        reason = result.escalation_reason
        conversation.status = ConversationStatus.NEEDS_ATTENTION
        conversation.attention_reason = reason.value if reason else None
        # Клиент получает ответ на каждое сообщение (решение 2026-09-27); повтор
        # того же шаблона заменяется коротким «администратор уже видит запрос».
        reply_text = result.safe_reply
        if reply_text:
            reply_text = _dedupe_template(db, conversation_id, reply_text)
    else:
        reply_text = result.reply_text
    if result.booking is not None and result.booking.kind is BookingKind.HOLD:
        # Бронь поставлена — её должен подтвердить человек (решение заказчика).
        conversation.status = ConversationStatus.NEEDS_ATTENTION
        conversation.attention_reason = "BOOKING_PENDING"

    outgoing: Message | None = None
    if reply_text:
        outgoing = Message(
            business_id=business_id,
            conversation_id=conversation_id,
            sender_type=SenderType.AI,
            text=reply_text,
            delivery_status=DeliveryStatus.PENDING,
        )
        db.add(outgoing)
        db.flush()

    candidate = result.response.text if result.response else None
    if not escalated:
        status = AiResponseStatus.PENDING
    elif result.escalation_reason is EscalationReason.VALIDATION_FAILED:
        status = AiResponseStatus.BLOCKED
    else:
        status = AiResponseStatus.ESCALATED
    db.add(
        AiResponse(
            business_id=business_id,
            message_id=message.id,
            response_message_id=outgoing.id if outgoing else None,
            model=result.response.model if result.response else None,
            prompt_version=result.response.prompt_version if result.response else None,
            response_text=candidate or reply_text or "",
            latency_ms=result.response.latency_ms if result.response else result.latency_ms,
            status=status,
            decision=result.decision.value,
            escalation_reason=result.escalation_reason.value if result.escalation_reason else None,
            details=result.as_log_payload(),
        )
    )
    outgoing_id = outgoing.id if outgoing else None
    db.commit()  # решение и исходящее сообщение сохранены ДО обращения к Telegram

    delivery = deliver_outgoing(db, outgoing_id) if outgoing_id else None

    message = db.get(Message, message_id)
    if message is not None:
        message.processing_status = ProcessingStatus.DONE
        message.processing_error = None
    _log(
        db,
        audit_service.EventType.MESSAGE_PROCESSED,
        "Сообщение обработано: " + ("передано менеджеру" if escalated else "ответ AI подготовлен"),
        business_id=business_id,
        payload={
            **log_context,
            "decision": result.decision.value,
            "intent": classification.intent.value,
            "priority": classification.priority.value,
            "escalation_reason": result.escalation_reason.value
            if result.escalation_reason
            else None,
            "reply_sent_to_customer": delivery is DeliveryStatus.SENT,
            "delivery_status": delivery.value if delivery else None,
        },
    )
    db.commit()


# --------------------------------------------------------------------------- #
# Доставка исходящих сообщений
# --------------------------------------------------------------------------- #
def deliver_outgoing(db: Session, message_id: int) -> DeliveryStatus | None:
    """Отправить сохранённое исходящее сообщение клиенту.

    Возвращает итоговый delivery_status. Ошибка канала не бросается наружу:
    она фиксируется в сообщении и в system_logs, а диалог помечается «требует
    внимания» (сценарий C раздела 7). Временный сбой оставляет PENDING —
    повторит reprocess_pending() (раздел 18).
    """
    message = db.get(Message, message_id)
    if message is None or message.delivery_status is not DeliveryStatus.PENDING:
        return message.delivery_status if message else None

    conversation = db.get(Conversation, message.conversation_id)
    customer = db.get(Customer, conversation.customer_id) if conversation else None
    if conversation is None or customer is None:
        raise RuntimeError("Диалог или клиент исходящего сообщения не найдены")

    business_id = message.business_id
    ai_response = db.scalar(select(AiResponse).where(AiResponse.response_message_id == message.id))
    # message_id в логах всегда указывает на входящее сообщение клиента, чтобы
    # вся цепочка (приём → AI → отправка → итог) выбиралась по одному id.
    log_payload = {
        "message_id": ai_response.message_id if ai_response else message.id,
        "outgoing_message_id": message.id,
        "conversation_id": conversation.id,
        "sender_type": message.sender_type.value,
    }

    def fail(reason: str, *, final: bool, blocked: bool = False) -> DeliveryStatus | None:
        message.delivery_error = reason[:500]
        if blocked:
            customer.channel_blocked = True
        if final:
            message.delivery_status = DeliveryStatus.FAILED
            conversation.status = ConversationStatus.NEEDS_ATTENTION
            conversation.attention_reason = "DELIVERY_FAILED"
            if ai_response is not None and ai_response.status is AiResponseStatus.PENDING:
                ai_response.status = AiResponseStatus.FAILED
        _log(
            db,
            audit_service.EventType.MESSAGE_SEND_FAILED,
            "Не удалось отправить сообщение клиенту"
            + ("" if final else " (временная ошибка, будет повтор)"),
            business_id=business_id,
            level=LogLevel.ERROR if final else LogLevel.WARNING,
            payload={**log_payload, "attempt": message.delivery_attempts, "error": reason[:300]},
        )
        db.commit()
        return message.delivery_status

    if customer.channel_blocked:
        return fail("Клиент заблокировал бота", final=True)
    integration = integration_service.get_active_integration(db, business_id, conversation.channel)
    if integration is None:
        return fail("Интеграция канала отключена", final=True)
    try:
        client = integration_service.get_channel_client(integration)
    except secret_store.SecretStoreError as exc:
        return fail(str(exc), final=True)

    message.delivery_attempts += 1
    try:
        external_id = client.send_message(customer.external_id, message.text)
    except ChannelSendError as exc:
        exhausted = message.delivery_attempts >= settings.message_max_attempts
        final = exc.blocked_by_user or not exc.retryable or exhausted
        return fail(str(exc), final=final, blocked=exc.blocked_by_user)

    message.delivery_status = DeliveryStatus.SENT
    message.external_message_id = external_id or None
    message.delivery_error = None
    if ai_response is not None and ai_response.status is AiResponseStatus.PENDING:
        ai_response.status = AiResponseStatus.SENT
    _log(
        db,
        audit_service.EventType.MESSAGE_SENT,
        "Сообщение отправлено клиенту",
        business_id=business_id,
        payload={
            **log_payload,
            "external_message_id": external_id,
            "attempt": message.delivery_attempts,
        },
    )
    db.commit()
    return DeliveryStatus.SENT


# --------------------------------------------------------------------------- #
# Повторная обработка (раздел 18)
# --------------------------------------------------------------------------- #
def reprocess_pending(limit: int = 20) -> int:
    """Подхватить сообщения, застрявшие после сбоя AI, БД или Telegram.

    Вызывается фоновым циклом приложения. Возвращает число обработанных записей.
    """
    now = utcnow()
    grace = now - timedelta(seconds=_GRACE_SECONDS)
    stale = now - timedelta(seconds=settings.message_processing_timeout_seconds)

    with SessionLocal() as db:
        incoming_ids = list(
            db.scalars(
                select(Message.id)
                .where(
                    Message.sender_type == SenderType.CUSTOMER,
                    Message.processing_attempts < settings.message_max_attempts,
                    or_(
                        and_(
                            Message.processing_status == ProcessingStatus.PENDING,
                            Message.created_at < grace,
                        ),
                        and_(
                            Message.processing_status == ProcessingStatus.FAILED,
                            Message.processing_started_at < grace,
                        ),
                        and_(
                            Message.processing_status == ProcessingStatus.PROCESSING,
                            Message.processing_started_at < stale,
                        ),
                    ),
                )
                .order_by(Message.id)
                .limit(limit)
            )
        )
        outgoing_ids = list(
            db.scalars(
                select(Message.id)
                .where(
                    Message.sender_type != SenderType.CUSTOMER,
                    Message.delivery_status == DeliveryStatus.PENDING,
                    Message.delivery_attempts < settings.message_max_attempts,
                    Message.created_at < grace,
                )
                .order_by(Message.id)
                .limit(limit)
            )
        )

    handled = 0
    for message_id in incoming_ids:
        process_incoming_message(message_id)
        handled += 1
    for message_id in outgoing_ids:
        with SessionLocal() as db:
            try:
                deliver_outgoing(db, message_id)
            except Exception:  # noqa: BLE001 - один сбой не должен останавливать остальные
                db.rollback()
                logger.exception("Повторная отправка сообщения %s не удалась", message_id)
        handled += 1
    return handled + ensure_replies(limit)


def ensure_replies(limit: int = 20) -> int:
    """Контроль «на каждое сообщение клиента есть ответ» (решение 2026-09-27).

    Последнее сообщение клиента в диалоге, обработанное (или с исчерпанными
    попытками), но без ответа дольше reply_watchdog_seconds, получает шаблон
    REPLY_RECEIVED. Не трогает диалоги, которые ведёт менеджер, компании с
    выключенными автоответами и клиентов, заблокировавших бота. Повторно не
    срабатывает: после шаблона в диалоге уже есть более новое сообщение.
    """
    now = utcnow()
    older_than = now - timedelta(seconds=settings.reply_watchdog_seconds)
    newer_than = now - timedelta(minutes=settings.reply_watchdog_window_minutes)
    later = aliased(Message)
    with SessionLocal() as db:
        ids = list(
            db.scalars(
                select(Message.id)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .join(Business, Business.id == Message.business_id)
                .join(Customer, Customer.id == Conversation.customer_id)
                .where(
                    Message.sender_type == SenderType.CUSTOMER,
                    Message.created_at < older_than,
                    Message.created_at > newer_than,
                    or_(
                        Message.processing_status == ProcessingStatus.DONE,
                        and_(
                            Message.processing_status == ProcessingStatus.FAILED,
                            Message.processing_attempts >= settings.message_max_attempts,
                        ),
                    ),
                    Conversation.handled_by_manager.is_(False),
                    Conversation.status != ConversationStatus.RESOLVED,
                    Business.ai_auto_reply.is_(True),
                    Customer.channel_blocked.is_(False),
                    ~exists().where(
                        later.conversation_id == Message.conversation_id,
                        later.id > Message.id,
                    ),
                )
                .order_by(Message.id)
                .limit(limit)
            )
        )

    for message_id in ids:
        with SessionLocal() as db:
            try:
                message = db.get_one(Message, message_id)
                conversation = db.get_one(Conversation, message.conversation_id)
                payload = {
                    "message_id": message.id,
                    "conversation_id": conversation.id,
                    "processing_status": getattr(message.processing_status, "value", None),
                    "processing_error": message.processing_error,
                }
                _log(
                    db,
                    audit_service.EventType.REPLY_WATCHDOG,
                    "Сообщение клиента осталось без ответа — отправляем шаблон",
                    business_id=message.business_id,
                    level=LogLevel.WARNING,
                    payload=payload,
                )
                _auto_reply(db, conversation, REPLY_RECEIVED, reason="WATCHDOG", payload=payload)
                db.commit()
            except Exception:  # noqa: BLE001 - один сбой не должен останавливать остальные
                db.rollback()
                logger.exception("Контроль ответа: сообщение %s", message_id)
    return len(ids)


# --------------------------------------------------------------------------- #
# Ручной ответ менеджера (раздел 11: POST /conversations/{id}/reply, раздел 14)
# --------------------------------------------------------------------------- #
def send_manager_reply(
    db: Session, ctx: BusinessContext, conversation: Conversation, text: str
) -> Message:
    """Ручной ответ клиенту. conversation уже проверен зависимостью доступа.

    Сначала ответ СОХРАНЯЕТСЯ (PENDING), потом уходит в канал — тот же порядок,
    что у ответов AI (раздел 18). Побочные эффекты (раздел 14):
    * диалог переходит к менеджеру: AI больше не отвечает до «решено»;
    * закрытый диалог и лид переоткрываются;
    * лид NEW → IN_PROGRESS, первый ответивший становится ответственным.
    Ошибка доставки не бросается: статус виден в delivery_status сообщения.
    """
    lead = lead_service.get_lead_for_conversation(db, conversation.id)

    message = Message(
        business_id=ctx.business_id,
        conversation_id=conversation.id,
        sender_type=SenderType.MANAGER,
        text=text,
        content_type="text",
        author_user_id=ctx.user.id,
        delivery_status=DeliveryStatus.PENDING,
    )
    db.add(message)

    lead_service.reopen_conversation(conversation, lead)
    conversation.handled_by_manager = True
    if conversation.status is ConversationStatus.NEEDS_ATTENTION:
        conversation.status = ConversationStatus.OPEN
        conversation.attention_reason = None
    lead_service.assign_if_unassigned(lead, ctx)
    conversation.updated_at = utcnow()
    db.flush()

    message_id = message.id
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.MANAGER_REPLY,
        message="Менеджер ответил клиенту",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={
            "message_id": message_id,
            "conversation_id": conversation.id,
            "lead_id": lead.id if lead else None,
        },
    )
    db.commit()  # сохранено до обращения к Telegram

    deliver_outgoing(db, message_id)
    db.expire_all()
    return db.get_one(Message, message_id)


# --------------------------------------------------------------------------- #
# Чтение диалогов (раздел 11: GET /businesses/{id}/conversations, /conversations/{id})
# --------------------------------------------------------------------------- #
def _attach_last_message_and_lead(
    db: Session, ctx: BusinessContext, rows: list[tuple[Conversation, Customer]]
) -> list[tuple[Conversation, Customer, Message | None, Lead | None]]:
    """Последнее сообщение и лид для каждого диалога — двумя запросами, а не N+1."""
    ids = [conversation.id for conversation, _ in rows]
    last_by_conversation: dict[int, Message] = {}
    lead_by_conversation: dict[int, Lead] = {}
    if ids:
        newest = (
            select(func.max(Message.id))
            .where(Message.conversation_id.in_(ids), Message.business_id == ctx.business_id)
            .group_by(Message.conversation_id)
        )
        for message in db.scalars(select(Message).where(Message.id.in_(newest))):
            last_by_conversation[message.conversation_id] = message
        for lead in db.scalars(
            select(Lead).where(Lead.conversation_id.in_(ids), Lead.business_id == ctx.business_id)
        ):
            lead_by_conversation[lead.conversation_id] = lead
    return [
        (
            conversation,
            customer,
            last_by_conversation.get(conversation.id),
            lead_by_conversation.get(conversation.id),
        )
        for conversation, customer in rows
    ]


def list_conversations(
    db: Session,
    ctx: BusinessContext,
    *,
    status: ConversationStatus | None = None,
    priority: LeadPriority | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[tuple[Conversation, Customer, Message | None, Lead | None]]:
    """Диалоги компании (раздел 6.4: фильтры по статусу, приоритету и периоду)
    с клиентом, последним сообщением и лидом. Всегда фильтр по business_id."""
    stmt = (
        select(Conversation, Customer)
        .join(Customer, Customer.id == Conversation.customer_id)
        .where(Conversation.business_id == ctx.business_id)
    )
    if status is not None:
        stmt = stmt.where(Conversation.status == status)
    if priority is not None:
        stmt = stmt.where(Conversation.priority == priority)
    if date_from is not None:
        stmt = stmt.where(Conversation.updated_at >= date_from)
    if date_to is not None:
        stmt = stmt.where(Conversation.updated_at <= date_to)
    rows = db.execute(
        stmt.order_by(Conversation.updated_at.desc(), Conversation.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return _attach_last_message_and_lead(db, ctx, [(c, cu) for c, cu in rows])


def get_conversation_detail(
    db: Session, ctx: BusinessContext, conversation: Conversation
) -> tuple[Customer, Lead | None, list[Message], list[AiResponse]]:
    """Диалог целиком. conversation уже проверен зависимостью доступа."""
    customer = db.get(Customer, conversation.customer_id)
    if customer is None or customer.business_id != ctx.business_id:
        raise RuntimeError("Клиент диалога не найден")
    lead = db.scalar(
        select(Lead).where(
            Lead.conversation_id == conversation.id, Lead.business_id == ctx.business_id
        )
    )
    messages = list(
        db.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.business_id == ctx.business_id,
            )
            .order_by(Message.id)
        )
    )
    decisions: list[AiResponse] = []
    if messages:
        decisions = list(
            db.scalars(
                select(AiResponse)
                .where(
                    AiResponse.business_id == ctx.business_id,
                    AiResponse.message_id.in_([m.id for m in messages]),
                )
                .order_by(AiResponse.id)
            )
        )
    return customer, lead, messages, decisions


# --------------------------------------------------------------------------- #
# Клиенты и история обращений (раздел 13: «Клиенты», вне минимального списка §11)
# --------------------------------------------------------------------------- #
def list_customers(
    db: Session,
    ctx: BusinessContext,
    *,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[tuple[Customer, int, datetime | None]]:
    """Клиенты компании: число обращений и последняя активность."""
    stmt = (
        select(Customer, func.count(Conversation.id), func.max(Conversation.updated_at))
        .outerjoin(Conversation, Conversation.customer_id == Customer.id)
        .where(Customer.business_id == ctx.business_id)
        .group_by(Customer.id)
    )
    if search and search.strip():
        raw = search.strip()
        # SQLite сравнивает без учёта регистра только латиницу, поэтому кириллицу
        # ищем по нескольким вариантам регистра («иван», «Иван», «ИВАН»).
        variants = {raw, raw.lower(), raw.upper(), raw.capitalize(), raw.title()}
        conditions = []
        for variant in variants:
            escaped = variant.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            conditions.append(Customer.name.ilike(pattern, escape="\\"))
            conditions.append(Customer.username.ilike(pattern, escape="\\"))
        stmt = stmt.where(or_(*conditions))
    rows = db.execute(
        stmt.order_by(func.max(Conversation.updated_at).desc(), Customer.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return [(customer, count, last) for customer, count, last in rows]


def get_customer_history(
    db: Session, ctx: BusinessContext, customer: Customer
) -> list[tuple[Conversation, Customer, Message | None, Lead | None]]:
    """История обращений клиента: его диалоги, новые сверху. customer уже проверен."""
    conversations = db.scalars(
        select(Conversation)
        .where(
            Conversation.customer_id == customer.id,
            Conversation.business_id == ctx.business_id,
        )
        .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
    ).all()
    return _attach_last_message_and_lead(db, ctx, [(c, customer) for c in conversations])


def inbox_state(db: Session, ctx: BusinessContext) -> dict:
    """Лёгкий снимок для тихого опроса кабинета: id последнего сообщения и число
    диалогов, требующих внимания. Страница по нему решает, показать «Есть новые»."""
    last_id = db.scalar(select(func.max(Message.id)).where(Message.business_id == ctx.business_id))
    attention = db.scalar(
        select(func.count(Conversation.id)).where(
            Conversation.business_id == ctx.business_id,
            Conversation.status == ConversationStatus.NEEDS_ATTENTION,
        )
    )
    return {"last_message_id": int(last_id or 0), "attention": int(attention or 0)}


def set_customer_channel_blocked(
    db: Session, integration: Integration, external_chat_id: str, blocked: bool
) -> bool:
    """Клиент запретил или снова разрешил сообщения от компании (VK: message_deny /
    message_allow). Пока запрет действует, ответы клиенту не отправляются.
    Возвращает False, если такого клиента у компании ещё нет."""
    customer = db.scalar(
        select(Customer).where(
            Customer.business_id == integration.business_id,
            Customer.channel == integration.channel,
            Customer.external_id == external_chat_id,
        )
    )
    if customer is None:
        return False
    if customer.channel_blocked != blocked:
        customer.channel_blocked = blocked
        _log(
            db,
            audit_service.EventType.CUSTOMER_CHANNEL_BLOCKED
            if blocked
            else audit_service.EventType.CUSTOMER_CHANNEL_UNBLOCKED,
            "Клиент запретил сообщения от компании"
            if blocked
            else "Клиент снова разрешил сообщения от компании",
            business_id=integration.business_id,
            payload={"customer_id": customer.id, "channel": integration.channel.value},
        )
    db.commit()
    return True


# --------------------------------------------------------------------------- #
# Решение по брони → сообщение клиенту (вне ТЗ, §22)
# --------------------------------------------------------------------------- #
def _client_name(customer: Customer) -> str:
    if customer.name:
        return customer.name
    return f"@{customer.username}" if customer.username else "Клиент"


def _booking_text(db: Session, booking: Booking, event: str) -> str | None:
    business = db.get_one(Business, booking.business_id)
    tz = schedule_service.business_tz(business)
    starts = (
        booking.starts_at if booking.starts_at.tzinfo else booking.starts_at.replace(tzinfo=UTC)
    )
    local = starts.astimezone(tz)
    when = format_when(local, datetime.now(tz).date())
    master = db.get(Master, booking.master_id)
    service = db.get(Service, booking.service_id) if booking.service_id else None
    service_name = service.name if service else "услуга"
    what = f"«{service_name}» у мастера {master.display_name if master else ''}".strip()
    if event == "created":
        # Сотрудник записал клиента по его заявке (запись связана с диалогом).
        address = f" Адрес: {business.address}." if business.address else ""
        return (
            f"Готово, вы записаны: {what}, {when}. Ждём вас!{address} "
            "Если планы изменятся — просто напишите сюда."
        )
    if event == "confirmed":
        # Решение заказчика 2026-09-28: «вы записаны» клиент получил сразу при брони
        # (ai/booking.py) — подтверждение администратора второй раз ему не пишем.
        return None
    if event == "rejected":
        # Клиент уже считал себя записанным — извиняемся и обещаем другое время.
        return (
            f"Извините, не получилось сохранить вашу запись: {what}, {when}. "
            "Администратор напишет вам здесь и предложит другое время."
        )
    if event == "cancelled":
        return f"Ваша запись {when} ({what}) отменена. Напишите, если хотите выбрать другое время."
    if event == "rescheduled":
        address = f" Адрес: {business.address}." if business.address else ""
        return (
            f"Ваша запись перенесена: {what}, {when}. Ждём вас!{address} "
            "Если время не подходит — напишите сюда."
        )
    return None


def _booking_client_message(db: Session, booking: Booking, event: str) -> Callable[[], None] | None:
    """Хук booking_service: сообщение клиенту в его канал. Сохраняется в транзакции
    решения, отправляется после коммита (сначала сохранить — раздел 18)."""
    if booking.conversation_id is None:
        return None
    text = _booking_text(db, booking, event)
    conversation = db.get(Conversation, booking.conversation_id)
    if text is None or conversation is None:
        return None
    outgoing = Message(
        business_id=booking.business_id,
        conversation_id=conversation.id,
        sender_type=SenderType.MANAGER,
        text=text,
        content_type="text",
        author_user_id=booking.decided_by_user_id or booking.created_by_user_id,
        delivery_status=DeliveryStatus.PENDING,
    )
    db.add(outgoing)
    conversation.updated_at = utcnow()
    db.flush()
    message_id = outgoing.id

    def send() -> None:
        deliver_outgoing(db, message_id)

    return send


booking_service.on_booking_event.append(_booking_client_message)
