"""
Имя и телефон клиента из его сообщения — вне ТЗ (§22), решение заказчика
2026-10-01: при записи собирать минимум имя и номер телефона.

Модуль, как и весь ai/, не знает о БД: только разбирает текст. Сохраняет
контакты в карточку клиента message_service. Номер не выдумывается: в карточку
попадает только то, что клиент написал сам (нормализованное к +7…).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CONTACT_PROMPT_VERSION = "contact-v1"

# Просьба оставить контакты (после брони) и дозапрос номера. По концу последнего
# ответа AI pipeline понимает, что следующее сообщение — контакты.
CONTACT_ASK = (
    "Оставьте, пожалуйста, ваше имя и номер телефона — на случай, если мастеру "
    "нужно будет с вами связаться."
)
CONTACT_ASK_PHONE = "Напишите, пожалуйста, ещё номер телефона — например, +7 900 123-45-67."

_PHONE_RE = re.compile(r"(?<![\d])(\+?\d[\d\s\-().]{8,20}\d)(?![\d])")
_NAME_INTRO_RE = re.compile(
    r"(?:меня зовут|мо[её] имя|имя)\s*[:\-—]?\s*([А-Яа-яЁёA-Za-z-]{2,30}(?:\s+[А-Яа-яЁёA-Za-z-]{2,30})?)",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[А-Яа-яЁёA-Za-z-]{2,30}")
# Слова, которые не имя: вводные, вежливость, «номер», «телефон».
_NOT_NAME = {
    "меня", "зовут", "имя", "мое", "моё", "мой", "моя", "номер", "телефон", "телефона",
    "тел", "мобильный", "сотовый", "это", "вот", "да", "нет", "не", "надо", "ок", "окей",
    "хорошо", "конечно", "ладно", "понял", "поняла", "отлично", "супер", "давайте",
    "пожалуйста", "спасибо", "здравствуйте", "привет", "добрый", "день", "вечер", "утро",
    "держите", "пишу", "записывайте", "запишите", "можно", "звоните", "пишите", "мне",
    "на", "по", "наш", "и", "или", "а", "ватсап", "whatsapp", "telegram", "телеграм",
}  # fmt: skip


@dataclass(frozen=True)
class Contact:
    name: str | None = None
    phone: str | None = None  # +79001234567

    @property
    def empty(self) -> bool:
        return not (self.name or self.phone)


def normalize_phone(raw: str) -> str | None:
    """«8 (900) 123-45-67», «+7 900 1234567», «9001234567» → «+79001234567».
    Не похоже на телефон — None."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        return "+7" + digits
    if raw.strip().startswith("+") and 11 <= len(digits) <= 15:
        return "+" + digits
    return None


def format_phone(phone: str | None) -> str:
    """+79001234567 → «+7 900 123-45-67» (прочие номера — как есть)."""
    if not phone:
        return ""
    if phone.startswith("+7") and len(phone) == 12:
        d = phone[2:]
        return f"+7 {d[:3]} {d[3:6]}-{d[6:8]}-{d[8:]}"
    return phone


def _clean_name(words: list[str]) -> str | None:
    words = [w for w in words if w.lower() not in _NOT_NAME]
    if not 1 <= len(words) <= 2:
        return None
    return " ".join(w[:1].upper() + w[1:].lower() for w in words)


def extract_contact(text: str, *, expect_name: bool = False) -> Contact:
    """Телефон — из любого сообщения. Имя — если клиент представился («меня зовут
    Олег») или если от него ждут контакты (expect_name) и кроме номера в
    сообщении одно-два слова («Олег, 8 900 123-45-67»)."""
    phone = None
    rest = text
    for match in _PHONE_RE.finditer(text):
        phone = normalize_phone(match.group(1))
        if phone:
            rest = text[: match.start()] + " " + text[match.end() :]
            break
    name = None
    intro = _NAME_INTRO_RE.search(rest)
    if intro:
        name = _clean_name(_WORD_RE.findall(intro.group(1)))
    elif expect_name:
        name = _clean_name(_WORD_RE.findall(rest))
    return Contact(name=name, phone=phone)
