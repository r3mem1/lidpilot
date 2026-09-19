"""
Проверка ответа перед отправкой — разделы 6.6, 6.7 и 12.1 ТЗ (шаг Validate).

Промпт — это просьба к модели, валидатор — контроль. Проверки детерминированные
(без обращений к LLM) и сверяют текст ответа с данными компании из БД:

* любая денежная сумма в ответе должна существовать в прайсе (или быть суммой
  реальных цен — сценарий A раздела 7: «стрижка + борода»);
* нельзя обещать запись, свободное время и скидки без подтверждённых данных
  (разделы 6.6, 12.3);
* нельзя раскрывать инструкции и называть контакты/адреса, которых нет в БД;
* если модель сама сообщила missing_info/needs_manager — диалог уходит человеку.

Любое нарушение означает, что ответ клиенту НЕ отправляется, а диалог
передаётся менеджеру (раздел 6.7).
"""

from __future__ import annotations

import enum
import itertools
import re
from dataclasses import dataclass, field
from decimal import Decimal

from ai.context import BusinessKnowledge
from ai.responder import GeneratedResponse
from config import settings

# Допуск при сверке сумм: модель может округлить 2500.50 до 2500 или 2501.
# Держим допуск меньше рубля: при больших прайсах множество допустимых сумм
# (цены + их комбинации) плотное, и допуск в рубль пропускал бы выдуманные
# «почти совпадающие» цены вида 3999 при разрешённых 4000.
PRICE_TOLERANCE = Decimal("0.50")


class ViolationCode(str, enum.Enum):
    EMPTY = "EMPTY"
    TOO_LONG = "TOO_LONG"
    PRICE_NOT_IN_DB = "PRICE_NOT_IN_DB"
    BOOKING_PROMISE = "BOOKING_PROMISE"
    DISCOUNT_PROMISE = "DISCOUNT_PROMISE"
    INSTRUCTION_LEAK = "INSTRUCTION_LEAK"
    CONTACT_MISMATCH = "CONTACT_MISMATCH"
    ADDRESS_NOT_IN_DB = "ADDRESS_NOT_IN_DB"
    PLACEHOLDER = "PLACEHOLDER"
    PRICE_UNVERIFIABLE = "PRICE_UNVERIFIABLE"


@dataclass(frozen=True)
class Violation:
    code: ViolationCode
    detail: str

    def __str__(self) -> str:  # pragma: no cover - для логов
        return f"{self.code.value}: {self.detail}"


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    escalate: bool
    reason: str | None = None
    violations: tuple[Violation, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "escalate": self.escalate,
            "reason": self.reason,
            "violations": [v.code.value for v in self.violations],
        }


# --------------------------------------------------------------------------- #
# Регулярные выражения проверок
# --------------------------------------------------------------------------- #
# Число, похожее на сумму. Отсекаем время (10:00) и части других чисел.
_NUMBER_RE = re.compile(r"(?<![\d:,.])(\d{1,3}(?:[  ]\d{3})+|\d+)(?:[.,](\d{1,2}))?(?![\d:])")
# Единицы измерения, после которых число — не деньги.
_UNIT_AFTER_RE = re.compile(
    r"^\s*(?:мин|минут|час|ч\.|сек|дн|день|дня|дней|недел|месяц|лет|год|года|шт|раз|%)",
    re.IGNORECASE,
)
_CURRENCY_AFTER_RE = re.compile(r"^\s*(?:₽|руб|р\.|rub|rur)", re.IGNORECASE)
_CURRENCY_BEFORE_RE = re.compile(r"(?:₽|руб|от|за)\s*$", re.IGNORECASE)

_BOOKING_PROMISE_RE = re.compile(
    "|".join(
        [
            r"вы записан",
            r"записал[аи]? вас",
            r"записываю вас",
            r"я вас запишу",
            r"запись подтвержд",
            r"забронирова",
            r"бронь подтвержд",
            r"свободн[оыа][ей]? (?:время|окошк|мест|слот)",
            r"есть (?:свободн|мест|окошк)",
            r"ждём вас в \d",
            r"ждем вас в \d",
            r"приходите в \d",
            r"можете прийти в \d",
            r"у нас есть место",
            r"место свободно",
        ]
    ),
    re.IGNORECASE,
)
_DISCOUNT_RE = re.compile(r"скидк|акци|промокод|бесплатно|\d\s*%", re.IGNORECASE)
_LEAK_RE = re.compile(
    "|".join(
        [
            r"систем[а-я]* (?:промпт|сообщени|инструкц)",
            r"промпт",
            r"prompt",
            r"мои инструкции",
            r"согласно инструкц",
            r"языковая модель",
            r"как ии\b",
            r"как искусственный интеллект",
            r"нейросет",
            r"gpt",
            r"температур[аы] модели",
        ]
    ),
    re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile(r"\[[^\]]*\]|\{\{|<вставьте|todo|xxx", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?:\+?\d[\d\-\s()]{8,}\d)")
_STREET_RE = re.compile(
    r"\bул\.|\bулиц|\bпроспект|\bпр-?кт|\bпереул|\bшоссе|\bнабережн|\bплощад|\bд\.\s*\d",
    re.IGNORECASE,
)
_WORD_RE = re.compile(r"[а-яёa-z0-9]+", re.IGNORECASE)
# Цена, которую нельзя сверить с прайсом автоматически: прописью («тысяча двести
# рублей») или сокращением («1.5к», «2 тыс»). Для клиента это такая же цена,
# поэтому такой ответ не отправляется, а уходит менеджеру (раздел 6.6).
_WORD_PRICE_RE = re.compile(
    r"\b(?:тысяч\w*|сотн\w*|сто|двести|триста|четыреста|пятьсот|шестьсот|семьсот|"
    r"восемьсот|девятьсот|полторы|полтора|пятьдесят|сорок|тридцать|двадцать|"
    r"десять|пятнадцать)\b[^.!?\n]{0,40}?(?:руб|₽|р\.)",
    re.IGNORECASE,
)
_SHORT_PRICE_RE = re.compile(r"\d\s*(?:к|k|тыс)\b", re.IGNORECASE)


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _allowed_amounts(knowledge: BusinessKnowledge) -> set[Decimal]:
    """Допустимые суммы: цены услуг и их комбинации.

    Комбинации нужны для типового сценария раздела 7-A («стрижка + борода»):
    сумма реальных цен — не выдумка. Комбинаторика ограничена, чтобы на больших
    прайсах не выродиться в «любое число разрешено».
    """
    prices = sorted(knowledge.price_set())
    allowed = set(prices)
    if not prices:
        return allowed
    if len(prices) <= 10:
        for size in (2, 3):
            for combo in itertools.combinations_with_replacement(prices, size):
                allowed.add(sum(combo, Decimal(0)))
    elif len(prices) <= 25:
        for combo in itertools.combinations(prices, 2):
            allowed.add(sum(combo, Decimal(0)))
    return allowed


def _numbers_from_business_fields(knowledge: BusinessKnowledge) -> set[str]:
    """Числа из графика работы, телефона и адреса: их упоминание законно."""
    source = " ".join(
        part for part in (knowledge.working_hours, knowledge.phone, knowledge.address) if part
    )
    return set(re.findall(r"\d+", source))


def _phone_like_spans(text: str) -> list[tuple[int, int, str, str]]:
    """Участки текста, похожие на телефонные номера.

    Нужны дважды: чтобы проверить сам номер и чтобы цифры телефона
    («+7 999 000-00-00») не были приняты за цену. Последовательность цифр без
    разделителей телефоном не считается — иначе перечисление цен
    «1500 1000 2500» маскировало бы выдуманные суммы.
    """
    spans: list[tuple[int, int, str, str]] = []
    for match in _PHONE_RE.finditer(text):
        raw = match.group(0)
        digits = _digits(raw)
        if len(digits) < 10:
            continue
        looks_like_phone = bool(re.search(r"[+\-()]", raw)) or (
            len(digits) in (10, 11) and raw.lstrip()[0] in "+78"
        )
        if looks_like_phone:
            spans.append((match.start(), match.end(), raw, digits))
    return spans


def _extract_money_candidates(text: str) -> list[tuple[Decimal, str, bool]]:
    """Числа из ответа, которые выглядят как денежные суммы.

    Третий элемент — есть ли рядом явная валюта (₽, «руб», «от», «за»)."""
    phone_spans = [(start, end) for start, end, _, _ in _phone_like_spans(text)]
    candidates: list[tuple[Decimal, str, bool]] = []
    for match in _NUMBER_RE.finditer(text):
        if any(start <= match.start() < end for start, end in phone_spans):
            continue  # часть телефонного номера, а не цена
        whole = match.group(1).replace(" ", "").replace(" ", "")
        cents = match.group(2)
        tail = text[match.end() : match.end() + 14]
        head = text[max(0, match.start() - 8) : match.start()]

        if _UNIT_AFTER_RE.match(tail):
            continue  # минуты, часы, проценты, штуки

        has_currency = bool(_CURRENCY_AFTER_RE.match(tail) or _CURRENCY_BEFORE_RE.search(head))
        amount = Decimal(f"{whole}.{cents}") if cents else Decimal(whole)

        # Проверяем только то, что действительно похоже на цену:
        # либо рядом есть валюта, либо сумма трёхзначная и больше.
        if has_currency or amount >= 100:
            candidates.append((amount, match.group(0), has_currency))
    return candidates


# --------------------------------------------------------------------------- #
# Валидатор
# --------------------------------------------------------------------------- #
class ResponseValidator:
    """Шаг Validate раздела 12.1."""

    def validate(
        self, response: GeneratedResponse, knowledge: BusinessKnowledge
    ) -> ValidationResult:
        text = (response.text or "").strip()
        violations: list[Violation] = []

        if not text:
            return ValidationResult(
                ok=False,
                escalate=True,
                reason="Пустой ответ модели",
                violations=(Violation(ViolationCode.EMPTY, "ответ пуст"),),
            )

        if len(text) > settings.ai_max_response_chars:
            violations.append(
                Violation(
                    ViolationCode.TOO_LONG,
                    f"{len(text)} символов > {settings.ai_max_response_chars}",
                )
            )

        violations.extend(self._check_prices(text, knowledge))
        violations.extend(self._check_promises(text, knowledge))
        violations.extend(self._check_leak(text, knowledge))
        violations.extend(self._check_contacts(text, knowledge))

        if _PLACEHOLDER_RE.search(text):
            violations.append(Violation(ViolationCode.PLACEHOLDER, "в ответе остался шаблон"))

        if violations:
            return ValidationResult(
                ok=False,
                escalate=True,
                reason="; ".join(str(v) for v in violations),
                violations=tuple(violations),
            )

        # Нарушений нет, но модель сама попросила человека (раздел 6.6/6.7).
        if response.needs_manager or response.missing_info:
            return ValidationResult(
                ok=True,
                escalate=True,
                reason=response.reason or "Модель сообщила о нехватке данных",
            )

        return ValidationResult(ok=True, escalate=False)

    # ------------------------------------------------------------------ #
    def _check_prices(self, text: str, knowledge: BusinessKnowledge) -> list[Violation]:
        """Раздел 6.6: AI не должен придумывать цены."""
        violations: list[Violation] = []
        if _WORD_PRICE_RE.search(text) or _SHORT_PRICE_RE.search(text):
            violations.append(
                Violation(
                    ViolationCode.PRICE_UNVERIFIABLE,
                    "цена указана прописью или сокращением — сверить с прайсом нельзя",
                )
            )

        candidates = _extract_money_candidates(text)
        if not candidates:
            return violations

        allowed = _allowed_amounts(knowledge)
        business_numbers = _numbers_from_business_fields(knowledge)
        durations = {Decimal(d) for d in knowledge.durations()}

        for amount, raw, has_currency in candidates:
            # Числа из графика/телефона/адреса и длительность услуги оправдывают
            # число только БЕЗ валюты: «999 ₽» при телефоне +7 999 ... — это цена.
            if not has_currency:
                if raw.strip() in business_numbers or str(int(amount)) in business_numbers:
                    continue
                if amount in durations:
                    continue
            if any(abs(amount - allowed_amount) <= PRICE_TOLERANCE for allowed_amount in allowed):
                continue
            violations.append(
                Violation(
                    ViolationCode.PRICE_NOT_IN_DB,
                    f"сумма {raw.strip()} отсутствует в прайсе компании",
                )
            )
        return violations

    def _check_promises(self, text: str, knowledge: BusinessKnowledge) -> list[Violation]:
        """Разделы 6.6 и 12.3: запись, свободное время, скидки."""
        violations: list[Violation] = []

        if not knowledge.has_schedule_integration and _BOOKING_PROMISE_RE.search(text):
            violations.append(
                Violation(
                    ViolationCode.BOOKING_PROMISE,
                    "обещание записи/времени без подключённого расписания",
                )
            )

        if _DISCOUNT_RE.search(text):
            declared = " ".join(
                part for part in (knowledge.description, knowledge.ai_rules) if part
            ).lower()
            if not _DISCOUNT_RE.search(declared):
                violations.append(
                    Violation(
                        ViolationCode.DISCOUNT_PROMISE,
                        "скидка/акция не заявлена в данных компании",
                    )
                )
        return violations

    def _check_leak(self, text: str, knowledge: BusinessKnowledge) -> list[Violation]:
        """Раздел 12.3: не раскрывать внутренние инструкции."""
        if _LEAK_RE.search(text):
            return [Violation(ViolationCode.INSTRUCTION_LEAK, "упоминание инструкций/модели")]

        rules = (knowledge.ai_rules or "").strip()
        if len(rules) >= 40:
            # Дословный фрагмент правил в ответе клиенту — тоже утечка.
            normalized_reply = " ".join(_WORD_RE.findall(text.lower()))
            normalized_rules = " ".join(_WORD_RE.findall(rules.lower()))
            chunk = normalized_rules[:60]
            if chunk and chunk in normalized_reply:
                return [Violation(ViolationCode.INSTRUCTION_LEAK, "дословный фрагмент правил AI")]
        return []

    def _check_contacts(self, text: str, knowledge: BusinessKnowledge) -> list[Violation]:
        """Разделы 6.6 и 12.3: телефоны и адреса — только из БД."""
        violations: list[Violation] = []

        business_phone = _digits(knowledge.phone or "")
        for _start, _end, _raw, found in _phone_like_spans(text):
            if not business_phone or found[-10:] != business_phone[-10:]:
                violations.append(
                    Violation(
                        ViolationCode.CONTACT_MISMATCH,
                        "в ответе телефон, которого нет в данных компании",
                    )
                )
                break

        if _STREET_RE.search(text):
            address = (knowledge.address or "").strip()
            if not address:
                violations.append(
                    Violation(ViolationCode.ADDRESS_NOT_IN_DB, "адрес не указан в данных компании")
                )
            else:
                address_words = {w.lower() for w in _WORD_RE.findall(address) if len(w) > 3}
                reply_words = {w.lower() for w in _WORD_RE.findall(text)}
                if address_words and not (address_words & reply_words):
                    violations.append(
                        Violation(
                            ViolationCode.ADDRESS_NOT_IN_DB,
                            "адрес в ответе не совпадает с адресом компании",
                        )
                    )
        return violations
