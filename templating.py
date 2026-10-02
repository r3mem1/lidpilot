"""
Jinja2-шаблоны кабинета — раздел 9 ТЗ (HTML/CSS/JavaScript + Jinja2, без SPA).

Единый экземпляр Jinja2Templates: автоэкранирование включено для всех .html —
текст клиентов и AI выводится как данные, а не как разметка (XSS, раздел 16).
Все динамические значения в шаблонах выводятся через {{ ... }} без |safe.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.templating import Jinja2Templates

from ai.contacts import format_phone
from cabinet_labels import LABELS

BASE_DIR = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def format_dt(value: datetime | None) -> str:
    """Запасной вид даты (UTC), пока JavaScript не заменил его на местное время."""
    if value is None:
        return "—"
    return _as_utc(value).strftime("%d.%m.%Y %H:%M")


def iso_dt(value: datetime | None) -> str:
    return _as_utc(value).isoformat() if value else ""


def format_money(value: Decimal | float | int | None) -> str:
    """1500 → «1 500 ₽», 1500.5 → «1 500,50 ₽» (неразрывные пробелы)."""
    if value is None:
        return "—"
    amount = Decimal(str(value))
    text = f"{amount:,.2f}" if amount != amount.to_integral_value() else f"{amount:,.0f}"
    whole, _, fraction = text.partition(".")
    whole = whole.replace(",", " ")
    return f"{whole},{fraction} ₽" if fraction else f"{whole} ₽"


def label(group: str, key: object) -> str:
    """Русская подпись значения перечисления: label('priority', 'HOT') → «Горячий»."""
    raw = getattr(key, "value", key)
    return LABELS.get(group, {}).get(str(raw), str(raw) if raw is not None else "—")


# Причины, сохранённые до 2026-10-01, содержат служебный код шага записи.
_BOOKING_STEP_CODES = {
    "HOLD": "поставлена бронь",
    "OFFER": "предложено свободное время",
    "ASK_SERVICE": "уточняется услуга",
    "WINDOWS": "названо свободное время мастеров",
    "NO_SLOTS": "свободного времени нет",
}
_BOOKING_STEP_RE = re.compile(r"(запись по расписанию: )([A-Z_]+)")


def clean_reason(value: str | None) -> str:
    """Причина классификации для человека: без служебного префикса «Правила: »,
    без хвоста «; LLM: …» и без кодов шага записи (полный текст — в данных лида)."""
    if not value:
        return ""
    text = value.split("; LLM:")[0].strip()
    text = _BOOKING_STEP_RE.sub(
        lambda m: m.group(1) + _BOOKING_STEP_CODES.get(m.group(2), m.group(2)), text
    )
    if text.startswith("Правила:"):
        text = text[len("Правила:") :].strip()
    return text[:1].upper() + text[1:] if text else ""


def pretty_json(value: object) -> str:
    """Данные события для панели администратора: читаемый JSON (кириллица без \\u-кодов).
    Возвращается обычной строкой — автоэкранирование Jinja остаётся включённым."""
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


_ASSET_VERSIONS: dict[str, str] = {}


def asset(path: str) -> str:
    """/static/<path>?v=<хеш содержимого>: после релиза браузер сразу берёт новые
    JS/CSS, а не держит старые из кеша (проверка сайта 2026-10-01: новая разметка
    работала со старым app.js). Хеш считается один раз на процесс."""
    version = _ASSET_VERSIONS.get(path)
    if version is None:
        try:
            content = (BASE_DIR / "static" / path).read_bytes()
            version = hashlib.sha256(content).hexdigest()[:10]
        except OSError:
            version = "0"
        _ASSET_VERSIONS[path] = version
    return f"/static/{path}?v={version}"


def quoted(name: str | None) -> str:
    """«Название» в кавычках — если в нём уже есть «», без вторых: не
    «Барбершоп «Борода»», а Барбершоп «Борода»."""
    text = (name or "").strip()
    return text if "«" in text or '"' in text else f"«{text}»"


templates.env.globals["asset"] = asset
templates.env.globals["quoted"] = quoted
templates.env.globals["format_phone"] = format_phone
templates.env.filters["pretty_json"] = pretty_json
templates.env.filters["reason"] = clean_reason
templates.env.filters["dt"] = format_dt
templates.env.filters["iso"] = iso_dt
templates.env.filters["money"] = format_money
templates.env.globals["label"] = label
