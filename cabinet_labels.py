"""Русские подписи значений для кабинета (раздел 13 ТЗ). Ключи — значения enum из models/ai."""

from __future__ import annotations

LABELS: dict[str, dict[str, str]] = {
    "priority": {"HOT": "Горячий", "WARM": "Тёплый", "COLD": "Холодный"},
    "conversation_status": {
        "OPEN": "Открыт",
        "NEEDS_ATTENTION": "Требует внимания",
        "RESOLVED": "Решён",
    },
    "lead_status": {
        "NEW": "Новый",
        "IN_PROGRESS": "В работе",
        "RESOLVED": "Решён",
        "LOST": "Потерян",
    },
    "intent": {
        "PRICE": "Цена",
        "BOOKING": "Запись",
        "QUESTION": "Вопрос",
        "COMPLAINT": "Жалоба",
        "OTHER": "Другое",
        "SPAM": "Спам",
        "UNKNOWN": "Не определено",
    },
    # Почему диалог требует внимания менеджера (раздел 6.7)
    "attention": {
        "HOT_LEAD_CONFIRMATION": "Запись: нужно подтвердить время",
        "COMPLAINT": "Жалоба клиента",
        "MISSING_DATA": "Не хватило данных для ответа",
        "AMBIGUOUS_REQUEST": "Неясный запрос",
        "ACTION_NOT_ALLOWED": "Просьба вне прав AI",
        "EXTERNAL_API_ERROR": "Сбой AI-сервиса",
        "VALIDATION_FAILED": "Ответ AI не прошёл проверку",
        "SPAM_SUSPECTED": "Похоже на спам",
        "AUTO_REPLY_DISABLED": "Автоответы отключены",
        "DELIVERY_FAILED": "Сообщение не доставлено",
        "PROCESSING_FAILED": "Не удалось обработать",
        "BUSINESS_SUSPENDED": "Компания приостановлена",
        "CUSTOMER_REPLIED": "Клиент написал после вашего ответа",
    },
    "delivery": {"PENDING": "Отправляется", "SENT": "Доставлено", "FAILED": "Не доставлено"},
    "sender": {"CUSTOMER": "Клиент", "AI": "AI-ассистент", "MANAGER": "Менеджер"},
    "role": {"OWNER": "Владелец", "MANAGER": "Менеджер", "ADMIN": "Администратор"},
    "tone": {"FRIENDLY": "Дружелюбный", "FORMAL": "Официальный", "BRIEF": "Краткий"},
    "business_status": {
        "TRIAL": "Пробный период",
        "ACTIVE": "Активна",
        "SUSPENDED": "Приостановлена",
    },
    "plan": {"TRIAL": "Пробный", "START": "Старт", "PRO": "Про"},
    "subscription_status": {"ACTIVE": "Действует", "CANCELED": "Отменена"},
    "log_level": {
        "INFO": "Информация",
        "WARNING": "Предупреждение",
        "ERROR": "Ошибка",
        "CRITICAL": "Критично",
    },
    "integration_status": {"ACTIVE": "Подключён", "DISABLED": "Отключён", "ERROR": "Ошибка"},
    "ai_status": {
        "PENDING": "Ожидает отправки",
        "SENT": "Ответ отправлен",
        "FAILED": "Не удалось отправить",
        "ESCALATED": "Передано менеджеру",
        "BLOCKED": "Ответ остановлен проверкой",
    },
}

TONE_HINTS = {
    "FRIENDLY": "Тёплый разговорный язык: «Здравствуйте! Стрижка стоит 1 500 ₽».",
    "FORMAL": "Обращение на «вы», без сленга: «Добрый день. Стоимость стрижки — 1 500 ₽».",
    "BRIEF": "Одно-два предложения по делу: «Стрижка — 1 500 ₽».",
}
