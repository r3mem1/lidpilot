"""
Контекст бизнеса для AI — шаг «Retrieve business context» раздела 12.1 ТЗ.

Здесь описан ЕДИНСТВЕННЫЙ набор сведений, которым AI разрешено пользоваться
(раздел 6.6: «использовать только достоверные сведения, внесённые владельцем
бизнеса»). Всё, чего нет в BusinessKnowledge, для AI не существует: валидатор
считает любую цену или обещание вне этих данных нарушением (раздел 12.3).

Модуль ai/ намеренно не зависит от SQLAlchemy и от БД: объект собирает
services/business_service.py по business_id пользователя, поэтому данные одной
компании физически не могут попасть в промпт другой (раздел 16).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from decimal import Decimal


class HistoryRole(str, enum.Enum):
    """Кто написал сообщение в истории диалога.

    Соответствует messages.sender_type раздела 10 ТЗ; сама таблица сообщений
    появится на этапе 3, поэтому на этапе 2 история передаётся вызывающим слоем.
    """

    CUSTOMER = "CUSTOMER"
    AI = "AI"
    MANAGER = "MANAGER"


@dataclass(frozen=True)
class HistoryTurn:
    role: HistoryRole
    text: str


@dataclass(frozen=True)
class ServiceInfo:
    """Услуга из прайса компании (раздел 6.3)."""

    name: str
    price: Decimal
    description: str | None = None
    duration: int | None = None  # минуты

    def as_line(self) -> str:
        parts = [f"{self.name} — {self.price:.2f}"]
        if self.duration:
            parts.append(f"длительность {self.duration} мин")
        if self.description:
            parts.append(self.description.strip())
        return "; ".join(parts)


@dataclass(frozen=True)
class BusinessKnowledge:
    """Достоверные данные конкретной компании.

    has_schedule_integration=False на всех этапах MVP: интеграции с календарём
    нет (раздел 19), поэтому AI не имеет права обещать конкретное время
    (разделы 6.6, 7-Б, 12.3).
    """

    business_id: int
    name: str
    category: str | None = None
    address: str | None = None
    phone: str | None = None
    working_hours: str | None = None
    description: str | None = None
    ai_rules: str | None = None
    escalation_contact: str | None = None
    services: tuple[ServiceInfo, ...] = field(default_factory=tuple)
    has_schedule_integration: bool = False

    # ------------------------------------------------------------------ #
    # Представление для промпта
    # ------------------------------------------------------------------ #
    def render_for_prompt(self) -> str:
        """Текст фактов для системного промпта.

        Отсутствующие сведения выводятся явно как «не указано»: модель должна
        видеть пробел в данных, а не додумывать его (раздел 6.6).
        """
        lines = [f"Название компании: {self.name}"]
        lines.append(f"Категория: {self.category or 'не указана'}")
        lines.append(f"Адрес: {self.address or 'НЕ УКАЗАН — называть нельзя'}")
        lines.append(f"Телефон: {self.phone or 'НЕ УКАЗАН — называть нельзя'}")
        lines.append(f"График работы: {self.working_hours or 'НЕ УКАЗАН — называть нельзя'}")
        if self.description:
            lines.append(f"О компании: {self.description.strip()}")

        if self.services:
            lines.append("Прайс (единственный допустимый источник цен):")
            lines.extend(f"  - {service.as_line()}" for service in self.services)
        else:
            lines.append("Прайс: услуг в базе нет — любые цены называть нельзя.")

        lines.append(
            "Запись/свободное время: "
            + (
                "расписание подключено"
                if self.has_schedule_integration
                else "расписание НЕ подключено — конкретное время и наличие мест обещать нельзя"
            )
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Данные для валидатора
    # ------------------------------------------------------------------ #
    def price_set(self) -> set[Decimal]:
        """Цены активных услуг — база для проверки чисел в ответе AI."""
        return {service.price for service in self.services}

    def service_names(self) -> set[str]:
        return {service.name.strip().lower() for service in self.services}

    def durations(self) -> set[int]:
        return {service.duration for service in self.services if service.duration}
