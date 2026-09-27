"""
Классификация обращений — разделы 6.5 и 12.2 ТЗ.

Раздел 6.5 допускает классификацию «по правилам и/или AI с сохранением причины».
Реализованы оба слоя:

1. Правила (детерминированные, работают всегда и мгновенно) — базовый результат.
2. LLM — уточняет intent/priority для неочевидных сообщений.

Правила имеют приоритет для COMPLAINT и SPAM: это решения, которые нельзя
отдавать на усмотрение модели, и они напрямую ведут к эскалации (раздел 6.7).
Флаг needs_manager объединяется по «или»: если человек нужен хотя бы по одному
из слоёв, диалог уходит менеджеру.

При ошибке LLM API результат берётся из правил и помечается needs_manager=True —
раздел 6.7 требует передавать диалог менеджеру при ошибке внешнего API.
"""

from __future__ import annotations

import enum
import logging
import re
from dataclasses import dataclass, replace

from ai.context import BusinessKnowledge, HistoryTurn
from ai.llm_client import LLMClient, LLMError
from ai.prompts import CLASSIFIER_PROMPT_VERSION, build_classifier_messages
from config import settings

logger = logging.getLogger("leadpilot.ai.classifier")


class Intent(str, enum.Enum):
    """Минимальный набор намерений (раздел 6.5)."""

    PRICE = "PRICE"
    BOOKING = "BOOKING"
    QUESTION = "QUESTION"
    COMPLAINT = "COMPLAINT"
    OTHER = "OTHER"
    SPAM = "SPAM"


class Priority(str, enum.Enum):
    """Приоритет лида (раздел 6.5)."""

    HOT = "HOT"
    WARM = "WARM"
    COLD = "COLD"


class ClassificationSource(str, enum.Enum):
    """Чем получен результат — нужно для ответа на вопрос раздела 17."""

    RULES = "RULES"
    LLM = "LLM"
    RULES_FALLBACK = "RULES_FALLBACK"  # LLM недоступен


@dataclass(frozen=True)
class Classification:
    """Структурированный результат классификатора (раздел 12.2 ТЗ)."""

    intent: Intent
    priority: Priority
    needs_manager: bool
    reason: str
    source: ClassificationSource = ClassificationSource.RULES
    prompt_version: str | None = None
    # Клиент просит действие вне прав AI: раскрыть инструкции, обойти правила,
    # изменить данные бизнеса (разделы 6.7, 12.3).
    action_not_allowed: bool = False
    # Модель не поняла, чего хочет клиент (бессмыслица, обрывок фразы): AI
    # переспрашивает, а не отвечает «нет данных» (решение заказчика 2026-09-27).
    unclear: bool = False

    def as_dict(self) -> dict:
        """Ровно та структура, что описана в разделе 12.2 (плюс происхождение)."""
        return {
            "intent": self.intent.value,
            "priority": self.priority.value,
            "needs_manager": self.needs_manager,
            "reason": self.reason,
            "source": self.source.value,
            "unclear": self.unclear,
        }


# --------------------------------------------------------------------------- #
# Слой правил
# --------------------------------------------------------------------------- #
def _pattern(words: list[str]) -> re.Pattern[str]:
    return re.compile("|".join(words), re.IGNORECASE)


_COMPLAINT_RE = _pattern(
    [
        r"жалоб",
        r"пожалуюсь",
        r"верните деньги",
        r"возврат",
        r"испортил",
        r"испорчен",
        r"хамств",
        r"груб",
        r"недоволен",
        r"недовольна",
        r"отвратительн",
        r"ужасн",
        r"обман",
        r"некачественн",
        r"претензи",
        r"хуже некуда",
        r"больше не приду",
    ]
)
_SPAM_RE = _pattern(
    [
        r"заработок",
        r"зарабатыва",
        r"крипт",
        r"инвестиц",
        r"трейдинг",
        r"казино",
        r"ставк[аи] на спорт",
        r"продвижен",
        r"seo",
        r"накрут",
        r"подписчик",
        r"рассылк",
        r"вебинар",
        r"кредит под",
        r"займ",
        r"оптов",
        r"франшиз",
        r"сотрудничеств[оа] по бартеру",
    ]
)
_INJECTION_RE = _pattern(
    [
        r"игнорируй\w* (?:(?:все|вс[её]|предыдущ\w+|прошл\w+|прежн\w+|свои|твои) )*(?:инструкц|правил|указани)",
        r"забудь (?:(?:все|вс[её]|предыдущ\w+|прежн\w+) )*(?:инструкц|правил|указани|что тебе)",
        r"ignore (?:(?:all|any|the|previous|above|prior|your) )*(?:instructions|rules)",
        r"disregard (?:(?:all|the|previous|above|your) )*(?:instructions|rules)",
        r"систем\w* (?:промпт|сообщени|инструкц)",
        r"system prompt",
        r"твои инструкции",
        r"покажи (?:свои |твои )?(?:инструкц|правила|промпт)",
        r"режим (?:разработчика|администратора|бога)",
        r"jailbreak",
        r"измени (?:цену|прайс|услуг)",
        r"поменяй (?:цену|прайс)",
        r"удали (?:услуг|прайс)",
    ]
)
_URL_RE = re.compile(
    r"(https?://|www\.|t\.me/|\b[a-z0-9-]+\.(?:ru|com|net|org|io)\b)", re.IGNORECASE
)
_BOOKING_RE = _pattern(
    [
        r"запис",
        r"запиш",  # «запишите», «запишешь» — корень с «ш» (раньше не распознавался)
        r"записаться",
        r"забронир",
        r"бронь",
        r"хочу прийти",
        r"можно прийти",
        r"когда можно",
        r"есть ли (?:свободн|место|окошк)",
        r"свободн[оыа]",
        r"окошк",
        r"приду",
        r"во сколько можно",
        r"можно к вам",
    ]
)
_PRICE_RE = _pattern(
    [
        r"сколько стоит",
        r"сколько будет",
        r"сколько за",
        r"цена",
        r"цены",
        r"ценник",
        r"стоимост",
        r"прайс",
        r"почем",
        r"по чем",
        r"сколько у вас",
        r"дорого ли",
    ]
)
_URGENCY_RE = _pattern(
    [r"срочно", r"сегодня", r"прямо сейчас", r"сейчас же", r"побыстрее", r"завтра"]
)


def classify_by_rules(text: str) -> Classification:
    """Детерминированная классификация. Всегда доступна, не требует сети."""
    normalized = (text or "").strip()
    lowered = normalized.lower()

    # 0. Попытка обойти инструкции или изменить данные бизнеса: AI такого
    #    не выполняет, диалог видит человек (разделы 6.7, 12.3).
    if _INJECTION_RE.search(lowered):
        return Classification(
            intent=Intent.OTHER,
            priority=Priority.COLD,
            needs_manager=True,
            reason="Правила: запрос действия вне прав AI (обход инструкций/изменение данных)",
            action_not_allowed=True,
        )

    # 1. Жалоба — всегда горячая и всегда к менеджеру (раздел 6.7).
    if _COMPLAINT_RE.search(lowered):
        return Classification(
            intent=Intent.COMPLAINT,
            priority=Priority.HOT,
            needs_manager=True,
            reason="Правила: в сообщении признаки жалобы",
        )

    # 2. Спам: рекламные маркеры либо ссылка вместе с рекламной лексикой.
    spam_hits = len(_SPAM_RE.findall(lowered))
    if spam_hits >= 2 or (spam_hits >= 1 and _URL_RE.search(lowered)):
        return Classification(
            intent=Intent.SPAM,
            priority=Priority.COLD,
            # Автоответ не отправляем, но и молча не теряем: решает менеджер
            # (раздел 2 — не терять входящие обращения).
            needs_manager=True,
            reason="Правила: признаки рекламной рассылки",
        )

    # 3. Запись: горячий лид, который без подтверждённого расписания
    #    обязан подтвердить человек (разделы 6.7, 7-Б).
    if _BOOKING_RE.search(lowered):
        return Classification(
            intent=Intent.BOOKING,
            priority=Priority.HOT,
            needs_manager=True,
            reason="Правила: запрос на запись, требуется подтверждение времени человеком",
        )

    # 4. Вопрос о цене — типовой случай, отвечается автоматически (раздел 7-A).
    if _PRICE_RE.search(lowered):
        return Classification(
            intent=Intent.PRICE,
            priority=Priority.HOT if _URGENCY_RE.search(lowered) else Priority.WARM,
            needs_manager=False,
            reason="Правила: клиент спрашивает цену услуги",
        )

    if "?" in normalized:
        return Classification(
            intent=Intent.QUESTION,
            priority=Priority.WARM,
            needs_manager=False,
            reason="Правила: общий вопрос клиента",
        )

    return Classification(
        intent=Intent.OTHER,
        priority=Priority.COLD,
        needs_manager=False,
        reason="Правила: явное намерение не определено",
    )


# --------------------------------------------------------------------------- #
# Слой LLM
# --------------------------------------------------------------------------- #
# Решения, которые не передаются модели: их цена ошибки слишком высока.
# BOOKING включён потому, что раздел 7-Б ТЗ требует для запроса записи высокий
# приоритет и участие человека — модель не должна это отменять, понизив intent.
_RULE_WINS = (Intent.COMPLAINT, Intent.SPAM, Intent.BOOKING)


def _parse_llm_classification(data: dict, fallback: Classification) -> Classification:
    """Разбор ответа модели. Любое неизвестное значение заменяется результатом
    правил — модель не может «придумать» новый intent или priority."""
    try:
        intent = Intent(str(data.get("intent", "")).strip().upper())
    except ValueError:
        intent = fallback.intent
    try:
        priority = Priority(str(data.get("priority", "")).strip().upper())
    except ValueError:
        priority = fallback.priority

    needs_manager = data.get("needs_manager")
    if not isinstance(needs_manager, bool):
        needs_manager = fallback.needs_manager

    reason = str(data.get("reason") or "").strip() or fallback.reason
    return Classification(
        intent=intent,
        priority=priority,
        needs_manager=needs_manager,
        reason=reason[:500],
        source=ClassificationSource.LLM,
        prompt_version=CLASSIFIER_PROMPT_VERSION,
        unclear=data.get("unclear") is True,
    )


class MessageClassifier:
    """Классификатор раздела 12.1 (шаг Intent + Priority classification)."""

    def __init__(self, client: LLMClient) -> None:
        self._client = client

    def classify(
        self,
        text: str,
        history: list[HistoryTurn],
        knowledge: BusinessKnowledge,
    ) -> Classification:
        rules = classify_by_rules(text)

        if self._client.offline:
            return rules

        try:
            result = self._client.complete_json(
                build_classifier_messages(text, history, knowledge, settings.ai_history_turns),
                purpose="classify",
                model=settings.classifier_model,
            )
        except LLMError as exc:
            logger.warning("Классификация по правилам из-за ошибки LLM: %s", exc)
            return replace(
                rules,
                needs_manager=True,  # раздел 6.7: ошибка внешнего API → менеджер
                reason=f"{rules.reason}; LLM недоступен, нужен менеджер",
                source=ClassificationSource.RULES_FALLBACK,
            )

        llm = _parse_llm_classification(result.data, rules)

        if rules.intent in _RULE_WINS or rules.action_not_allowed:
            # Правила увидели жалобу, спам, запрос записи или попытку обхода
            # инструкций — модель это решение не отменяет.
            merged = replace(
                rules,
                needs_manager=rules.needs_manager or llm.needs_manager,
                reason=f"{rules.reason}; LLM: {llm.reason}",
                source=ClassificationSource.LLM,
                prompt_version=CLASSIFIER_PROMPT_VERSION,
            )
        else:
            merged = replace(llm, needs_manager=rules.needs_manager or llm.needs_manager)

        return self._enforce_booking_invariant(merged, knowledge)

    @staticmethod
    def _enforce_booking_invariant(
        classification: Classification, knowledge: BusinessKnowledge
    ) -> Classification:
        """Запрос записи без подключённого расписания всегда идёт человеку.

        Инвариант разделов 6.6, 6.7 и 7-Б ТЗ: пока нет интеграции с календарём,
        подтвердить время может только сотрудник. Срабатывает и в случае, когда
        BOOKING определила модель, а правила — нет.
        """
        if classification.intent is not Intent.BOOKING or knowledge.has_schedule_integration:
            return classification
        if classification.needs_manager and classification.priority is Priority.HOT:
            return classification
        return replace(
            classification,
            priority=Priority.HOT,
            needs_manager=True,
            reason=f"{classification.reason}; запись подтверждает сотрудник",
        )
