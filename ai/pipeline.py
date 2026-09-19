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
from dataclasses import dataclass

from ai.classifier import Classification, Intent, MessageClassifier, Priority
from ai.context import BusinessKnowledge, HistoryTurn
from ai.llm_client import LLMClient, LLMError, get_llm_client
from ai.responder import GeneratedResponse, Responder
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
    ACTION_NOT_ALLOWED = "ACTION_NOT_ALLOWED"  # действие вне прав AI
    HOT_LEAD_CONFIRMATION = "HOT_LEAD_CONFIRMATION"  # горячий лид, нужно подтверждение
    EXTERNAL_API_ERROR = "EXTERNAL_API_ERROR"  # ошибка внешнего API
    VALIDATION_FAILED = "VALIDATION_FAILED"  # ответ не прошёл проверку
    SPAM_SUSPECTED = "SPAM_SUSPECTED"  # похоже на спам, отвечать не нужно


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

    @property
    def needs_manager(self) -> bool:
        return self.decision is Decision.ESCALATE

    @property
    def safe_reply(self) -> str | None:
        """Безопасный ответ клиенту при передаче менеджеру (раздел 6.6:
        «безопасный ответ с предложением уточнить данные», сценарий C раздела 7).
        Готовый шаблон без фактов о компании; None — клиенту ничего не
        отправляется (подозрение на спам) либо ответ уходит как обычный (SEND)."""
        if self.decision is not Decision.ESCALATE or self.escalation_reason is None:
            return None
        return safe_reply_for(self.escalation_reason)

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
            payload["safe_reply_sent"] = self.safe_reply is not None
        if self.response:
            payload["model"] = self.response.model
            payload["prompt_version"] = self.response.prompt_version
            payload["response_latency_ms"] = self.response.latency_ms
        if self.validation:
            payload["validation"] = self.validation.as_dict()
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


# Шаблоны безопасных ответов клиенту при эскалации. Без цен, времени, адресов
# и обещаний записи — валидатор пропускает их по построению (проверяется тестом).
_HOLDING_DEFAULT = (
    "Спасибо за сообщение! Уточню детали у сотрудника — он ответит вам в ближайшее время."
)
_SAFE_REPLIES: dict[EscalationReason, str | None] = {
    EscalationReason.HOT_LEAD_CONFIRMATION: (
        "Спасибо за обращение! Передаю ваш запрос на запись администратору — "
        "он подтвердит время и свяжется с вами."
    ),
    EscalationReason.COMPLAINT: (
        "Сожалеем, что так вышло. Передали ваше обращение ответственному сотруднику — "
        "он свяжется с вами."
    ),
    EscalationReason.ACTION_NOT_ALLOWED: (
        "С этим вопросом я помочь не могу — передаю его сотруднику, он ответит вам."
    ),
    EscalationReason.MISSING_DATA: _HOLDING_DEFAULT,
    EscalationReason.AMBIGUOUS_REQUEST: _HOLDING_DEFAULT,
    EscalationReason.EXTERNAL_API_ERROR: _HOLDING_DEFAULT,
    EscalationReason.VALIDATION_FAILED: _HOLDING_DEFAULT,
    # Похоже на рекламу: отвечать боту-рассыльщику незачем, менеджер решит сам.
    EscalationReason.SPAM_SUSPECTED: None,
}


def safe_reply_for(reason: EscalationReason) -> str | None:
    return _SAFE_REPLIES.get(reason)


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

    def process(
        self,
        text: str,
        knowledge: BusinessKnowledge,
        history: list[HistoryTurn] | None = None,
    ) -> PipelineResult:
        started = time.monotonic()
        history = history or []
        normalized = normalize(text)

        def elapsed() -> int:
            return int((time.monotonic() - started) * 1000)

        # Пустое или нетекстовое сообщение (стикер, фото) — решает человек.
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
            )

        # Шаги Intent + Priority classification.
        classification = self._classifier.classify(normalized, history, knowledge)

        # Человек нужен ещё до генерации: жалоба, запись, спам, ошибка API,
        # неоднозначный запрос (раздел 6.7). Автоответ в этих случаях не даём.
        if classification.needs_manager:
            reason = _PRE_GENERATION_REASONS.get(classification.intent)
            if classification.action_not_allowed:
                reason = EscalationReason.ACTION_NOT_ALLOWED
            if reason is None:
                reason = (
                    EscalationReason.EXTERNAL_API_ERROR
                    if classification.source.value == "RULES_FALLBACK"
                    else EscalationReason.MISSING_DATA
                )
            return PipelineResult(
                decision=Decision.ESCALATE,
                normalized_text=normalized,
                classification=classification,
                escalation_reason=reason,
                escalation_detail=classification.reason,
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
