"""
Генерация ответа клиенту — разделы 6.6 и 12.1 ТЗ (шаг Generate response).

Ответ строится только из данных компании (BusinessKnowledge). Модель, кроме
текста, обязана вернуть структуру: какие суммы она назвала, хватило ли данных
и нужен ли менеджер — по этим полям валидатор выполняет проверку (раздел 6.6).

Офлайн-режим (AI_PROVIDER=stub) собирает ответ шаблоном из тех же данных БД:
это позволяет разрабатывать и тестировать pipeline без ключа API и никогда
не приводит к придуманным фактам. Ошибка реального API шаблоном НЕ подменяется —
по разделу 6.7 такой диалог уходит менеджеру.
"""

from __future__ import annotations

import enum
import logging
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from ai.classifier import Classification, Intent
from ai.context import BusinessKnowledge, HistoryTurn, ServiceInfo
from ai.faq import format_money
from ai.llm_client import LLMClient, LLMInvalidResponse
from ai.prompts import RESPONDER_PROMPT_VERSION, build_responder_messages
from config import settings

logger = logging.getLogger("leadpilot.ai.responder")


class ResponseSource(str, enum.Enum):
    LLM = "LLM"
    OFFLINE_TEMPLATE = "OFFLINE_TEMPLATE"
    # Вне ТЗ (§22): текст записи собран шаблоном из расписания в БД (ai/booking.py).
    BOOKING_ENGINE = "BOOKING_ENGINE"
    # Решение 2026-09-28: адрес, график, телефон, цены — шаблоном из данных (ai/faq.py).
    FAQ_TEMPLATE = "FAQ_TEMPLATE"


@dataclass(frozen=True)
class GeneratedResponse:
    """Кандидат ответа. Отправлять его можно только после валидации."""

    text: str
    model: str
    prompt_version: str
    latency_ms: int
    source: ResponseSource
    # Суммы, которые модель считает использованными — вход для валидатора.
    used_prices: tuple[Decimal, ...] = field(default_factory=tuple)
    missing_info: bool = False
    needs_manager: bool = False
    reason: str = ""


_WORD_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)


def _significant_words(text: str) -> set[str]:
    """Слова длиннее 3 символов — грубое сопоставление услуг по названию."""
    return {word.lower() for word in _WORD_RE.findall(text or "") if len(word) > 3}


def _match_services(text: str, knowledge: BusinessKnowledge) -> list[ServiceInfo]:
    """Услуги, упомянутые в сообщении клиента (по пересечению слов)."""
    words = _significant_words(text)
    if not words:
        return []
    matched = [
        service for service in knowledge.services if _significant_words(service.name) & words
    ]
    return matched


def _to_decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", ".").replace(" ", ""))
    except (InvalidOperation, ValueError, TypeError):
        return None


class Responder:
    """Шаг Generate response раздела 12.1."""

    def __init__(self, client: LLMClient) -> None:
        self._client = client

    def generate(
        self,
        text: str,
        history: list[HistoryTurn],
        knowledge: BusinessKnowledge,
        classification: Classification,
    ) -> GeneratedResponse:
        if self._client.offline:
            return self._offline_template(text, knowledge, classification)

        result = self._client.complete_json(
            build_responder_messages(
                text,
                history,
                knowledge,
                intent=classification.intent.value,
                priority=classification.priority.value,
                history_limit=settings.ai_history_turns,
                max_chars=settings.ai_max_response_chars,
            ),
            purpose="respond",
        )

        reply = str(result.data.get("reply") or "").strip()
        if not reply:
            # Пустой ответ — не повод молчать: pipeline передаст диалог менеджеру.
            raise LLMInvalidResponse("Модель вернула пустой reply")

        raw_prices = result.data.get("used_prices")
        used_prices: tuple[Decimal, ...] = ()
        if isinstance(raw_prices, list):
            parsed = [_to_decimal(item) for item in raw_prices]
            used_prices = tuple(price for price in parsed if price is not None)

        return GeneratedResponse(
            text=reply,
            model=result.model,
            prompt_version=RESPONDER_PROMPT_VERSION,
            latency_ms=result.latency_ms,
            source=ResponseSource.LLM,
            used_prices=used_prices,
            missing_info=bool(result.data.get("missing_info")),
            needs_manager=bool(result.data.get("needs_manager")),
            reason=str(result.data.get("reason") or "").strip()[:500],
        )

    # ------------------------------------------------------------------ #
    # Офлайн-режим разработки
    # ------------------------------------------------------------------ #
    def _offline_template(
        self, text: str, knowledge: BusinessKnowledge, classification: Classification
    ) -> GeneratedResponse:
        started = time.monotonic()
        needs_manager = True
        used_prices: tuple[Decimal, ...] = ()

        if classification.intent is Intent.PRICE:
            matched = _match_services(text, knowledge) or list(knowledge.services)
            if matched and len(matched) <= 5:
                listing = ", ".join(f"{s.name} — {format_money(s.price)}" for s in matched)
                reply = f"Актуальные цены: {listing}. Подскажите, что вас интересует?"
                used_prices = tuple(s.price for s in matched)
                needs_manager = False
            else:
                reply = (
                    "Уточню цены у сотрудника и вернусь с ответом — "
                    "подскажите, какая услуга вас интересует?"
                )
        elif classification.intent is Intent.BOOKING:
            reply = (
                "Передаю ваш запрос администратору: он подтвердит время записи и свяжется с вами."
            )
        elif classification.intent is Intent.COMPLAINT:
            reply = (
                "Сожалею, что так вышло. Передаю обращение ответственному сотруднику — "
                "он свяжется с вами."
            )
        else:
            reply = "Передал ваш вопрос сотруднику, он ответит в ближайшее время."

        return GeneratedResponse(
            text=reply,
            model="offline-template",
            prompt_version=RESPONDER_PROMPT_VERSION,
            latency_ms=int((time.monotonic() - started) * 1000),
            source=ResponseSource.OFFLINE_TEMPLATE,
            used_prices=used_prices,
            missing_info=needs_manager,
            needs_manager=needs_manager,
            reason="Офлайн-шаблон по данным компании (AI_PROVIDER=stub)",
        )
