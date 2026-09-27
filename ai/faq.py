"""
Ответы на простые факты без LLM — решение заказчика 2026-09-28 по итогам аудита прода.

Адрес, телефон, график работы и цены уже есть в данных компании (раздел 6.3), поэтому
на такие вопросы отвечает шаблон: мгновенно и без риска, что модель ответит пусто,
медленно или её ответ не пройдёт валидатор (аудит: «Где вы находитесь?» получало
«нет информации» при заполненном адресе). Шаблон берёт факты только из
BusinessKnowledge — инвариант 2 соблюдается построением. Сложные и смешанные
сообщения, а также факты, которых в данных нет, по-прежнему решает LLM.
"""

from __future__ import annotations

import re
from decimal import Decimal

from ai.booking import BookableService, _match_service, _words
from ai.context import BusinessKnowledge, ServiceInfo

FAQ_PROMPT_VERSION = "faq-v1"
# Длинные сообщения обычно содержат несколько вопросов — их разбирает LLM.
MAX_FAQ_CHARS = 120

_ADDRESS_RE = re.compile(
    r"где\s+вы|где\s+наход|где\s+располож|ваш\s+адрес|какой\s+(у\s+вас\s+)?адрес|\bадрес\b"
    r"|как\s+(до\s+вас\s+)?добраться|как\s+вас\s+найти|куда\s+(подъехать|приходить|идти)",
    re.IGNORECASE,
)
_HOURS_RE = re.compile(
    r"до\s+скольки|во\s+сколько\s+(вы\s+)?(открыв|закрыв|работаете|начинаете)|график"
    r"|часы\s+работы|режим\s+работы|когда\s+(вы\s+)?(открыт|работаете)"
    r"|работаете\s+(ли\s+)?(сегодня|завтра|в\s+выходн|по\s+выходн|в\s+суббот|в\s+воскрес)"
    r"|вы\s+(сегодня\s+|завтра\s+)?работаете",
    re.IGNORECASE,
)
_PHONE_RE = re.compile(
    r"телефон|номер\s+для\s+связи|ваш\s+номер|позвонить|как\s+(с\s+вами\s+)?связаться",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(
    r"сколько\s+стоит|сколько\s+будет|сколько\s+за|цен[аыу]|стоимост|почем|по\s+чем|прайс",
    re.IGNORECASE,
)
_PRICE_LIST_RE = re.compile(
    r"\bцены\b|прайс|стоимость\s+услуг|сколько\s+стоят\s+услуги|какие\s+(у\s+вас\s+)?(цены|услуги)"
    r"|что\s+по\s+ценам|перечень\s+услуг",
    re.IGNORECASE,
)
# Темы, которых в шаблонах нет: такое сообщение целиком разбирает LLM.
_OTHER_TOPIC_RE = re.compile(
    r"карт|оплат|наличн|парков|скидк|акци|промокод|ребен|ребён|возраст|wi-?fi|кофе"
    r"|сертификат|подар|мастер|свободн|окошк|запис|запиш",
    re.IGNORECASE,
)
_HOW_MUCH_RE = re.compile(r"(?<![а-яё])сколько(?![а-яё])", re.IGNORECASE)
LATE_RE = re.compile(
    r"опозда|задерж\w*|буду\s+позже|подойду\s+позже|приду\s+позже|в\s+пробке|немного\s+позже",
    re.IGNORECASE,
)
DISCOUNT_RE = re.compile(r"скидк|акци|промокод|дешевле|студенч|пенсионер|льгот", re.IGNORECASE)


def _money(value: Decimal) -> str:
    """1500 → «1 500 ₽», 1500.5 → «1 500,50 ₽» (неразрывные пробелы)."""
    amount = Decimal(str(value))
    text = f"{amount:,.2f}" if amount != amount.to_integral_value() else f"{amount:,.0f}"
    whole, _, fraction = text.partition(".")
    whole = whole.replace(",", " ")
    return f"{whole},{fraction} ₽" if fraction else f"{whole} ₽"


def _service_line(service: ServiceInfo) -> str:
    duration = f", {service.duration} мин" if service.duration else ""
    return f"«{service.name}» — {_money(service.price)}{duration}"


def discount_question(text: str, knowledge: BusinessKnowledge) -> bool:
    """Вопрос только о скидках/акциях, а владелец о них ничего не написал: отвечаем
    «уточню у администратора» сразу, без LLM (валидатор всё равно не пропустит
    слова о скидках, которых нет в данных). Если в сообщении есть цена или услуга
    из прайса — отвечает LLM: он назовёт цену и скажет, что скидок в прайсе нет."""
    if len(text) > MAX_FAQ_CHARS or not DISCOUNT_RE.search(text):
        return False
    if _PRICE_RE.search(text) or _HOW_MUCH_RE.search(text):
        return False
    bookable = [BookableService(i, s.name) for i, s in enumerate(knowledge.services, 1)]
    if bookable and _match_service(text, bookable) is not None:
        return False
    owner_text = " ".join(x for x in (knowledge.description, knowledge.ai_rules) if x)
    return not DISCOUNT_RE.search(owner_text)


# Служебные слова вопросов о фактах. Любое другое слово в сообщении (кроме названий
# услуг) значит, что клиент спрашивает что-то ещё — тогда отвечает LLM.
_FAQ_WORDS = frozenset(
    [
        "а",
        "и",
        "или",
        "но",
        "у",
        "вас",
        "вы",
        "мне",
        "нам",
        "по",
        "за",
        "на",
        "в",
        "во",
        "с",
        "со",
        "до",
        "от",
        "это",
        "ли",
        "же",
        "ну",
        "вот",
        "тогда",
        "еще",
        "также",
        "сколько",
        "стоит",
        "стоят",
        "стоимость",
        "цена",
        "цены",
        "цену",
        "ценам",
        "прайс",
        "почем",
        "чем",
        "будет",
        "выйдет",
        "какие",
        "какая",
        "какой",
        "какое",
        "каков",
        "что",
        "перечень",
        "услуги",
        "услуг",
        "узнать",
        "уточнить",
        "подскажите",
        "скажите",
        "подскажи",
        "скажи",
        "пожалуйста",
        "здравствуйте",
        "привет",
        "добрый",
        "доброе",
        "день",
        "вечер",
        "утро",
        "где",
        "находитесь",
        "находится",
        "расположены",
        "адрес",
        "ваш",
        "ваша",
        "как",
        "добраться",
        "найти",
        "вас",
        "куда",
        "подъехать",
        "приходить",
        "идти",
        "скольки",
        "работаете",
        "работает",
        "работать",
        "график",
        "часы",
        "работы",
        "режим",
        "когда",
        "открыты",
        "открываетесь",
        "закрываетесь",
        "открыто",
        "сегодня",
        "завтра",
        "выходные",
        "выходных",
        "выходным",
        "субботу",
        "воскресенье",
        "телефон",
        "номер",
        "позвонить",
        "связаться",
        "дайте",
        "связи",
        "для",
        "хочу",
        "интересует",
        "время",
        "времени",
        "минут",
        "мин",
        "долго",
        "длится",
        "идет",
        "делается",
        "спасибо",
    ]
)


def _service_words(name: str) -> set[str]:
    return {w[:5] for w in _words(name) if len(w) >= 3}


def _mentioned_services(text: str, services: list[ServiceInfo]) -> list[ServiceInfo]:
    """Услуги прайса, названные в сообщении: название целиком или все его слова
    (по основе). Если названа «Стрижка + борода», одиночные «Стрижка» и «Борода»
    не добавляются — клиент спрашивает о комплексе."""
    lowered = text.lower().replace("ё", "е")
    stems = {w[:5] for w in _words(text) if len(w) >= 3}
    found = []
    for service in services:
        name = service.name.lower().replace("ё", "е")
        words = _service_words(service.name)
        if name in lowered or (words and words <= stems):
            found.append(service)
    return [
        s
        for s in found
        if not any(
            other is not s and _service_words(s.name) < _service_words(other.name)
            for other in found
        )
    ]


def _has_other_words(text: str, services: list[ServiceInfo]) -> bool:
    service_stems = set().union(*(_service_words(s.name) for s in services)) if services else set()
    return any(
        len(word) >= 3 and word not in _FAQ_WORDS and word[:5] not in service_stems
        for word in _words(text)
    )


def faq_answer(text: str, knowledge: BusinessKnowledge) -> str | None:
    """Шаблонный ответ на вопрос об адресе, графике, телефоне или цене; None —
    вопрос не из этих, в нём есть что-то ещё или нужных данных нет (тогда LLM)."""
    if len(text) > MAX_FAQ_CHARS or _OTHER_TOPIC_RE.search(text):
        return None
    services = list(knowledge.services)
    mentioned = _mentioned_services(text, services)
    asked_address = bool(_ADDRESS_RE.search(text))
    asked_hours = bool(_HOURS_RE.search(text))
    asked_phone = bool(_PHONE_RE.search(text))
    # «А борода сколько?» — тоже вопрос о цене, если названа услуга из прайса.
    asked_price = bool(_PRICE_RE.search(text)) or bool(mentioned and _HOW_MUCH_RE.search(text))
    if not (asked_address or asked_hours or asked_phone or asked_price):
        return None
    # «Стрижка с окрашиванием», «как добраться от метро» — шаблон ответил бы не на всё.
    if _has_other_words(text, mentioned):
        return None

    parts: list[str] = []
    if asked_address:
        if not knowledge.address:
            return None
        parts.append(f"Наш адрес: {knowledge.address.strip()}.")
    if asked_hours:
        if not knowledge.working_hours:
            return None
        parts.append(f"Мы работаем: {knowledge.working_hours.strip()}.")
    if asked_phone:
        if not knowledge.phone:
            return None
        parts.append(f"Наш телефон: {knowledge.phone.strip()}.")
    if asked_price:
        if mentioned:
            parts.append("; ".join(_service_line(s) for s in mentioned) + ".")
        elif services and _PRICE_LIST_RE.search(text):
            parts.append("Наши цены: " + "; ".join(_service_line(s) for s in services) + ".")
        else:
            return None  # цена услуги, которой нет в прайсе, — решает LLM («нет данных»)
        parts.append("Если хотите записаться — напишите, на какой день и время.")
    return " ".join(parts)
