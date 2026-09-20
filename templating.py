"""
Jinja2-шаблоны кабинета — раздел 9 ТЗ (HTML/CSS/JavaScript + Jinja2, без SPA).

Единый экземпляр Jinja2Templates: автоэкранирование включено для всех .html —
текст клиентов и AI выводится как данные, а не как разметка (XSS, раздел 16).
Все динамические значения в шаблонах выводятся через {{ ... }} без |safe.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.templating import Jinja2Templates

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


def clean_reason(value: str | None) -> str:
    """Причина классификации для человека: без служебного префикса «Правила: »
    и без хвоста «; LLM: …» (полный текст остаётся в данных лида)."""
    if not value:
        return ""
    text = value.split("; LLM:")[0].strip()
    if text.startswith("Правила:"):
        text = text[len("Правила:") :].strip()
    return text[:1].upper() + text[1:] if text else ""


templates.env.filters["reason"] = clean_reason
templates.env.filters["dt"] = format_dt
templates.env.filters["iso"] = iso_dt
templates.env.filters["money"] = format_money
templates.env.globals["label"] = label
