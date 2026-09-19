"""
AI Service — слой между БД и AI-модулем (раздел 8 ТЗ: «AI Service»).

Обязанности:
* собрать контекст конкретной компании (шаг Retrieve business context, 12.1);
* запустить pipeline;
* записать в system_logs, что и почему решила система, включая время ответа AI
  и ошибки AI (раздел 17).

Границы этапа 2: сохранение сообщений, ai_responses и лидов появится вместе
с таблицами conversations/messages/leads (этапы 3 и 4). Сейчас результат
возвращается вызывающему слою и фиксируется в системных логах.
"""

from __future__ import annotations

import logging
import threading

from sqlalchemy.orm import Session

from ai.context import BusinessKnowledge, HistoryTurn
from ai.pipeline import AIPipeline, Decision, PipelineResult
from config import settings
from models import Business, LogLevel
from services import audit_service, business_service

logger = logging.getLogger("leadpilot.ai.service")

_pipeline: AIPipeline | None = None
_pipeline_lock = threading.Lock()


def get_pipeline() -> AIPipeline:
    """Один pipeline на процесс: настройки читаются один раз."""
    global _pipeline
    if _pipeline is None:
        with _pipeline_lock:
            if _pipeline is None:
                _pipeline = AIPipeline()
    return _pipeline


def reset_pipeline(pipeline: AIPipeline | None = None) -> None:
    """Подмена pipeline в тестах и после изменения конфигурации."""
    global _pipeline
    with _pipeline_lock:
        _pipeline = pipeline


def process_message(
    db: Session,
    business: Business,
    text: str,
    history: list[HistoryTurn] | None = None,
    *,
    actor_user_id: int | None = None,
    knowledge: BusinessKnowledge | None = None,
    log_context: dict | None = None,
) -> PipelineResult:
    """Обработать сообщение клиента для конкретной компании.

    business — уже проверенный объект (доступ проверен в access_service либо,
    на этапе 3, компания определена по интеграции), поэтому в контекст AI
    попадают данные только этой компании (раздел 16).
    """
    business_id = business.id
    knowledge = knowledge or business_service.build_ai_context(db, business)
    # Транзакция освобождается до обращения к внешнему LLM API: запрос может идти
    # десятки секунд, и открытая транзакция блокировала бы остальные записи
    # (в SQLite — любых писателей). Всё, что нужно AI, уже прочитано.
    db.commit()
    result = get_pipeline().process(text, knowledge, history)

    _log_result(
        db,
        business_id=business_id,
        result=result,
        actor_user_id=actor_user_id,
        log_context=log_context,
    )
    return result


def _log_result(
    db: Session,
    *,
    business_id: int,
    result: PipelineResult,
    actor_user_id: int | None,
    log_context: dict | None = None,
) -> None:
    """Раздел 17: время ответа AI, ошибки AI, причина решения.

    log_context (например, message_id и conversation_id) склеивает событие AI
    с конкретным сообщением клиента."""
    payload = {**(log_context or {}), **result.as_log_payload()}

    if result.decision is Decision.SEND:
        event_type = audit_service.EventType.AI_RESPONSE_READY
        level = LogLevel.INFO
        message = f"AI подготовил ответ (intent={payload['intent']})"
    else:
        reason = payload.get("escalation_reason", "UNKNOWN")
        if reason == "EXTERNAL_API_ERROR":
            event_type = audit_service.EventType.AI_ERROR
            level = LogLevel.ERROR
            message = "Ошибка AI/внешнего API, диалог передан менеджеру"
        elif reason == "VALIDATION_FAILED":
            event_type = audit_service.EventType.AI_VALIDATION_FAILED
            level = LogLevel.WARNING
            message = "Ответ AI не прошёл проверку, диалог передан менеджеру"
        else:
            event_type = audit_service.EventType.AI_ESCALATED
            level = LogLevel.INFO
            message = f"Диалог передан менеджеру ({reason})"

    audit_service.log_event(
        db,
        event_type=event_type,
        message=message,
        level=level,
        business_id=business_id,
        actor_user_id=actor_user_id,
        payload=payload,
        commit=True,
    )

    if settings.debug:
        logger.debug("AI pipeline: %s", payload)
