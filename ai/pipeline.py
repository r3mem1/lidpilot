"""
AI pipeline — раздел 12.1 ТЗ и Приложение B:

    MESSAGE → Normalize → Intent + Priority → Retrieve business context
            → Generate response → Validate → Send / Escalate

Модуль не знает ни про БД, ни про Telegram: на вход поступают текст, история
и BusinessKnowledge, на выход — решение. Благодаря этому этап 3 (webhook) и
этап 4 (диалоги/лиды) подключаются к готовому и протестированному ядру, а сам
pipeline проверяется без сети и без Telegram.
"""

from __future__ import annotations

import enum
import logging
import re
import time
from dataclasses import dataclass, replace

from ai.booking import (
    BOOKING_PROMPT_VERSION,
    BookingEngine,
    BookingKind,
    BookingOutcome,
    ScheduleProvider,
)
from ai.classifier import Classification, Intent, MessageClassifier, Priority
from ai.context import BusinessKnowledge, HistoryTurn
from ai.llm_client import LLMClient, LLMError, get_llm_client
from ai.responder import GeneratedResponse, Responder, ResponseSource
from ai.validator import ResponseValidator, ValidationResult

logger = logging.getLogger("leadpilot.ai.pipeline")

# Ограничение длины входа: защита от «простыней» и от раздувания промпта.
MAX_INPUT_CHARS = 4000

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\u200b-\u200f\ufeff]")
_WS_RE = re.compile(r"[ \t\u00a0]+")
_NEWLINES_RE = re.compile(r"\n{3,}")


class Decision(str, enum.Enum):
    SEND = "SEND"  # ответ можно отправлять клиенту
    ESCALATE = "ESCALATE"  # диалог передаётся менеджеру (раздел 6.7)


class EscalationReason(str, enum.Enum):
    """Причины передачи менеджеру — перечень раздела 6.7 ТЗ."""

    COMPLAINT = "COMPLAINT"  # жалоба клиента
    AMBIGUOUS_REQUEST = "AMBIGUOUS_REQUEST"  # неоднозначный запрос
    MISSING_DATA = "MISSING_DATA"  # нет необходимых данных
    AUTO_REPLY_DISABLED = "AUTO_REPLY_DISABLED"  # владелец отключил автоответы
    ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"  # действие вне прав AI
    HOT_LEAD_CONFIRMATION = "HOT_LEAD_CONFIRMATION"  # горячий лид, нужно подтверждение
    EXTERNAL_API_ERROR = "EXTERNAL_API_ERROR"  # ошибка внешнего API
    VALIDATION_FAILED = "VALIDATION_FAILED"  # ответ не прошёл проверку
    SPAM_SUSPECTED = "SPAM_SUSPECTED"  # похоже на спам


@dataclass(frozen=True)
class PipelineResult:
    """Полный след обработки: по нему видно, что и почему решила система
    (требование раздела 17)."""

    decision: Decision
    normalized_text: str
    classification: Classification
    reply_text: str | None = None
    response: GeneratedResponse | None = None
    validation: ValidationResult | None = None
    escalation_reason: EscalationReason | None = None
    escalation_detail: str | None = None
    latency_ms: int = 0
    # Вне ТЗ (§22): итог работы системы записи (бронь, предложенные окна).
    booking: BookingOutcome | None = None
    # Шаблон для частного случая причины (отмена записи, нет окон, переспрос…);
    # None — шаблон причины из _SAFE_REPLIES.
    reply_override: str | None = None
    # Владелец выключил автоответы: клиенту не уходит ничего, даже шаблон.
    client_reply_allowed: bool = True

    @property
    def needs_manager(self) -> bool:
        return self.decision is Decision.ESCALATE

    @property
    def safe_reply(self) -> str | None:
        """Безопасный ответ клиенту при передаче менеджеру (раздел 6.6:
        «безопасный ответ с предложением уточнить данные», сценарий C раздела 7).
        Готовый шаблон без фактов о компании. Решение 2026-09-27 (отступление от
        §6.7, согласовано): клиент получает ответ на каждое сообщение, None —
        только при выключенных автоответах или при SEND (ответ уходит как обычный)."""
        if self.decision is not Decision.ESCALATE or self.escalation_reason is None:
            return None
        if not self.client_reply_allowed:
            return None
        return self.reply_override or safe_reply_for(self.escalation_reason)

    def as_log_payload(self) -> dict:
        """Компактное представление для system_logs (раздел 17)."""
        payload: dict = {
            "decision": self.decision.value,
            "intent": self.classification.intent.value,
            "priority": self.classification.priority.value,
            "classification_source": self.classification.source.value,
            "reason": self.classification.reason,
            "latency_ms": self.latency_ms,
        }
        if self.escalation_reason:
            payload["escalation_reason"] = self.escalation_reason.value
            payload["escalation_detail"] = self.escalation_detail
            # На уровне pipeline это только «для эскалации есть шаблон ответа».
            # Доставлен ли он клиенту, видно в более позднем событии
            # MESSAGE_PROCESSED.reply_sent_to_customer (message_service может
            # заменить повтор того же шаблона коротким REPLY_REPEAT).
            payload["safe_reply_available"] = self.safe_reply is not None
            if self.reply_override:
                payload["reply_override"] = self.reply_override
        if self.response:
            payload["model"] = self.response.model
            payload["prompt_version"] = self.response.prompt_version
            payload["response_latency_ms"] = self.response.latency_ms
        if self.validation:
            payload["validation"] = self.validation.as_dict()
        if self.booking:
            payload["booking"] = self.booking.as_dict()
        return payload


def normalize(text: str) -> str:
    """Шаг Normalize раздела 12.1: убрать управляющие символы, свернуть
    пробелы, ограничить длину. Смысл сообщения не меняется."""
    cleaned = _CONTROL_RE.sub("", text or "")
    cleaned = _WS_RE.sub(" ", cleaned)
    cleaned = _NEWLINES_RE.sub("\n\n", cleaned)
    cleaned = "\n".join(line.strip() for line in cleaned.splitlines()).strip()
    if len(cleaned) > MAX_INPUT_CHARS:
        cleaned = cleaned[:MAX_INPUT_CHARS].rstrip() + "…"
    return cleaned


# Шаблоны ответов клиенту, когда отвечает не LLM. Без цен, времени, адресов
# и обещаний записи — валидатор пропускает их по построению (проверяется тестом).
# Решение заказчика 2026-09-27 (отступление от §6.7, согласовано): клиент получает
# ответ на каждое сообщение; «передаю администратору» — только если AI не понял
# запрос; молчание — только после вмешательства менеджера или при выключенных
# автоответах.
REPLY_RECEIVED = "Спасибо, сообщение получили — ответим в ближайшее время."
REPLY_STAFF_WILL_ANSWER = "Спасибо за сообщение! Сотрудник ответит вам в ближайшее время."
REPLY_REPEAT = "Администратор уже видит ваш запрос и ответит здесь в ближайшее время."
REPLY_NO_INFO = "Точной информации об этом у меня нет — уточню у администратора, он ответит здесь."
REPLY_CLARIFY = "Уточните, пожалуйста, что вас интересует: цены, запись или что-то другое?"
REPLY_UNCLEAR_HANDOFF = "Передаю ваш вопрос администратору — он ответит здесь в ближайшее время."
REPLY_TEXT_ONLY = "Пока я понимаю только текст — напишите, пожалуйста, словами, что вас интересует."
REPLY_BOOKING_REQUEST = (
    "С радостью запишем! Подскажите, на какую услугу и какой день и время вам удобны? "
    "Администратор подтвердит запись здесь."
)
REPLY_NO_SLOTS = (
    "На ближайшие две недели свободных окон нет. "
    "Администратор подберёт для вас время и напишет здесь."
)
REPLY_CANCEL = (
    "Поняли, передали администратору — он отменит или перенесёт запись и подтвердит здесь."
)
REPLY_SPAM = (
    "Здравствуйте! Если у вас вопрос об услугах или записи — напишите, пожалуйста, подробнее."
)

_SAFE_REPLIES: dict[EscalationReason, str | None] = {
    EscalationReason.HOT_LEAD_CONFIRMATION: REPLY_BOOKING_REQUEST,
    EscalationReason.COMPLAINT: (
        "Сожалеем, что так вышло. Передали ваше обращение ответственному сотруднику — "
        "он свяжется с вами."
    ),
    EscalationReason.ACTION_NOT_ALLOWED: (
        "С этим вопросом я помочь не могу — передаю его сотруднику, он ответит вам."
    ),
    EscalationReason.MISSING_DATA: REPLY_NO_INFO,
    EscalationReason.VALIDATION_FAILED: REPLY_NO_INFO,
    EscalationReason.AMBIGUOUS_REQUEST: REPLY_CLARIFY,
    EscalationReason.EXTERNAL_API_ERROR: REPLY_RECEIVED,
    EscalationReason.SPAM_SUSPECTED: REPLY_SPAM,
    # Владелец выключил автоответы — никаких ответов, даже шаблонов.
    EscalationReason.AUTO_REPLY_DISABLED: None,
}

# Все шаблоны — для проверки валидатором в тестах.
ALL_TEMPLATES: tuple[str, ...] = (
    REPLY_RECEIVED,
    REPLY_STAFF_WILL_ANSWER,
    REPLY_REPEAT,
    REPLY_NO_INFO,
    REPLY_CLARIFY,
    REPLY_UNCLEAR_HANDOFF,
    REPLY_TEXT_ONLY,
    REPLY_BOOKING_REQUEST,
    REPLY_NO_SLOTS,
    REPLY_CANCEL,
    REPLY_SPAM,
)


def safe_reply_for(reason: EscalationReason) -> str | None:
    return _SAFE_REPLIES.get(reason)


def _last_ai_text(history: list[HistoryTurn]) -> str | None:
    """Текст последнего содержательного ответа AI клиенту — чтобы не переспрашивать
    дважды подряд. Короткий REPLY_REPEAT пропускается; ответ менеджера сбрасывает."""
    for turn in reversed(history):
        if turn.role.value == "CUSTOMER" or turn.text == REPLY_REPEAT:
            continue
        return turn.text if turn.role.value == "AI" else None
    return None


# Вне ТЗ (§22): просьбы отменить или перенести запись движок записи не обрабатывает.
_CANCEL_RE = re.compile(r"отмен|перенес|перенос|не приду|не смогу прийти|не успеваю", re.IGNORECASE)

# Причина эскалации, когда человек нужен ещё до генерации ответа.
_PRE_GENERATION_REASONS = {
    Intent.COMPLAINT: EscalationReason.COMPLAINT,
    Intent.BOOKING: EscalationReason.HOT_LEAD_CONFIRMATION,
    Intent.SPAM: EscalationReason.SPAM_SUSPECTED,
}


class AIPipeline:
    """Сборка шагов раздела 12.1."""

    def __init__(
        self,
        client: LLMClient | None = None,
        *,
        classifier: MessageClassifier | None = None,
        responder: Responder | None = None,
        validator: ResponseValidator | None = None,
    ) -> None:
        self._client = client or get_llm_client()
        self._classifier = classifier or MessageClassifier(self._client)
        self._responder = responder or Responder(self._client)
        self._validator = validator or ResponseValidator()
        self._booking = BookingEngine(self._client)

    def process(
        self,
        text: str,
        knowledge: BusinessKnowledge,
        history: list[HistoryTurn] | None = None,
        schedule: ScheduleProvider | None = None,
    ) -> PipelineResult:
        result = self._decide(text, knowledge, history or [], schedule)
        # Владелец выключил автоответы (раздел 13): AI только классифицирует,
        # клиенту не уходит ничего — ни ответ, ни шаблон при эскалации.
        if not knowledge.auto_reply and result.decision is Decision.ESCALATE:
            return replace(result, client_reply_allowed=False)
        return result

    def _decide(
        self,
        text: str,
        knowledge: BusinessKnowledge,
        history: list[HistoryTurn],
        schedule: ScheduleProvider | None,
    ) -> PipelineResult:
        started = time.monotonic()
        normalized = normalize(text)

        def elapsed() -> int:
            return int((time.monotonic() - started) * 1000)

        # Пустое или нетекстовое сообщение (стикер, фото): просим написать словами.
        if not normalized:
            classification = Classification(
                intent=Intent.OTHER,
                priority=Priority.COLD,
                needs_manager=True,
                reason="Сообщение без текста",
            )
            return PipelineResult(
                decision=Decision.ESCALATE,
                normalized_text=normalized,
                classification=classification,
                escalation_reason=EscalationReason.AMBIGUOUS_REQUEST,
                escalation_detail="Сообщение не содержит текста для обработки",
                latency_ms=elapsed(),
                reply_override=REPLY_TEXT_ONLY,
            )

        # Шаги Intent + Priority classification.
        classification = self._classifier.classify(normalized, history, knowledge)

        # Вне ТЗ (§22): запись по расписанию. Жалоба, спам и попытка обхода правил
        # по-прежнему уходят человеку; время и окна — только из расписания в БД.
        if schedule is not None and self._booking_applies(
            normalized, classification, knowledge, schedule
        ):
            return self._handle_booking(
                normalized, history, classification, schedule, elapsed, knowledge.address
            )

        def escalate(
            reason: EscalationReason, override: str | None = None, detail: str | None = None
        ) -> PipelineResult:
            return PipelineResult(
                decision=Decision.ESCALATE,
                normalized_text=normalized,
                classification=classification,
                escalation_reason=reason,
                escalation_detail=detail or classification.reason,
                latency_ms=elapsed(),
                reply_override=override,
            )

        rules_fallback = classification.source.value == "RULES_FALLBACK"
        special = classification.action_not_allowed or classification.intent in (
            Intent.COMPLAINT,
            Intent.SPAM,
            Intent.BOOKING,
        )

        # AI не понял, чего хочет клиент: один раз переспрашиваем, при повторе —
        # «передаю администратору» (единственный случай этой фразы, решение 2026-09-27).
        if classification.unclear and not special and not rules_fallback:
            # Уже переспрашивали или уже передали — дальше только «передаю»
            # (повтор message_service заменит коротким REPLY_REPEAT).
            asked = _last_ai_text(history) in (REPLY_CLARIFY, REPLY_UNCLEAR_HANDOFF)
            return escalate(
                EscalationReason.AMBIGUOUS_REQUEST, REPLY_UNCLEAR_HANDOFF if asked else None
            )

        # Запись, которую не взял движок записи: время называет только движок из БД
        # (инвариант 2), поэтому LLM-ответ здесь не генерируется никогда.
        if classification.intent is Intent.BOOKING and not classification.action_not_allowed:
            if _CANCEL_RE.search(normalized):
                return escalate(EscalationReason.HOT_LEAD_CONFIRMATION, REPLY_CANCEL)
            return escalate(EscalationReason.HOT_LEAD_CONFIRMATION)

        # Человек нужен ещё до генерации: жалоба, спам, ошибка API, запрос вне
        # прав AI, нет данных (раздел 6.7). Клиент получает шаблон своей причины.
        if classification.needs_manager:
            reason = _PRE_GENERATION_REASONS.get(classification.intent)
            if classification.action_not_allowed:
                reason = EscalationReason.ACTION_NOT_ALLOWED
            if reason is None:
                reason = (
                    EscalationReason.EXTERNAL_API_ERROR
                    if rules_fallback
                    else EscalationReason.MISSING_DATA
                )
            return escalate(reason)

        # Владелец отключил автоответы (раздел 13: разрешённые действия): AI только
        # классифицировал обращение, отвечает менеджер.
        if not knowledge.auto_reply:
            return PipelineResult(
                decision=Decision.ESCALATE,
                normalized_text=normalized,
                classification=classification,
                escalation_reason=EscalationReason.AUTO_REPLY_DISABLED,
                escalation_detail="Автоответы отключены в настройках компании",
                latency_ms=elapsed(),
            )

        # Шаг Generate response. Данные компании уже получены вызывающим слоем
        # (services/ai_service.py) — шаг Retrieve business context.
        try:
            response = self._responder.generate(normalized, history, knowledge, classification)
        except LLMError as exc:
            # Раздел 6.7 и сценарий C раздела 7: ошибка внешнего API → менеджер.
            logger.warning("Генерация ответа не удалась: %s", exc)
            return PipelineResult(
                decision=Decision.ESCALATE,
                normalized_text=normalized,
                classification=classification,
                escalation_reason=EscalationReason.EXTERNAL_API_ERROR,
                escalation_detail=str(exc),
                latency_ms=elapsed(),
            )

        # Шаг Validate.
        validation = self._validator.validate(response, knowledge)
        if not validation.ok:
            return PipelineResult(
                decision=Decision.ESCALATE,
                normalized_text=normalized,
                classification=classification,
                response=response,
                validation=validation,
                escalation_reason=EscalationReason.VALIDATION_FAILED,
                escalation_detail=validation.reason,
                latency_ms=elapsed(),
            )
        if validation.escalate:
            return PipelineResult(
                decision=Decision.ESCALATE,
                normalized_text=normalized,
                classification=classification,
                response=response,
                validation=validation,
                escalation_reason=EscalationReason.MISSING_DATA,
                escalation_detail=validation.reason,
                latency_ms=elapsed(),
            )

        # Шаг Send: ответ проверен и может уйти клиенту (отправка — этап 3).
        return PipelineResult(
            decision=Decision.SEND,
            normalized_text=normalized,
            classification=classification,
            reply_text=response.text,
            response=response,
            validation=validation,
            latency_ms=elapsed(),
        )

    # ------------------------------------------------------------------ #
    # Запись по расписанию (вне ТЗ, §22)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _booking_applies(
        text: str,
        classification: Classification,
        knowledge: BusinessKnowledge,
        schedule: ScheduleProvider | None,
    ) -> bool:
        if schedule is None or not knowledge.has_schedule_integration or not knowledge.auto_reply:
            return False
        if classification.action_not_allowed or classification.intent in (
            Intent.COMPLAINT,
            Intent.SPAM,
        ):
            return False
        # Отмена и перенос существующей записи — решает человек (движок только записывает).
        if _CANCEL_RE.search(text):
            return False
        if not (classification.intent is Intent.BOOKING or schedule.in_booking_dialog()):
            return False
        # Ни одна услуга не привязана к мастеру — записывать не на что: иначе движок
        # спросил бы «На какую услугу? Есть: .» с пустым списком.
        return bool(schedule.services())

    def _handle_booking(
        self,
        normalized: str,
        history: list[HistoryTurn],
        classification: Classification,
        schedule: ScheduleProvider,
        elapsed,
        address: str | None = None,
    ) -> PipelineResult:
        started = time.monotonic()
        outcome = self._booking.handle(normalized, history, schedule, address=address)
        classification = replace(
            classification,
            intent=Intent.BOOKING,
            priority=Priority.HOT,
            needs_manager=outcome.kind in (BookingKind.HOLD, BookingKind.NO_SLOTS),
            reason=f"{classification.reason}; запись по расписанию: {outcome.kind.value}",
        )
        if outcome.kind is BookingKind.NO_SLOTS or not outcome.reply:
            return PipelineResult(
                decision=Decision.ESCALATE,
                normalized_text=normalized,
                classification=classification,
                escalation_reason=EscalationReason.HOT_LEAD_CONFIRMATION,
                escalation_detail="Свободного времени по расписанию не найдено",
                latency_ms=elapsed(),
                booking=outcome,
                reply_override=REPLY_NO_SLOTS,
            )
        response = GeneratedResponse(
            text=outcome.reply,
            model="booking-engine" if outcome.source == "RULES" else "booking-engine+llm",
            prompt_version=BOOKING_PROMPT_VERSION,
            latency_ms=int((time.monotonic() - started) * 1000),
            source=ResponseSource.BOOKING_ENGINE,
        )
        # Валидатор LLM-текста не применяется: ответ собран шаблоном только из
        # данных расписания в БД, свободное время в нём — результат запроса к БД.
        return PipelineResult(
            decision=Decision.SEND,
            normalized_text=normalized,
            classification=classification,
            reply_text=outcome.reply,
            response=response,
            latency_ms=elapsed(),
            booking=outcome,
        )
