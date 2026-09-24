"""
Проверочный скрипт этапа 7 — мониторинг ошибок Sentry (НЕ часть приложения, можно удалить).

Покрывает разделы 16, 17 ТЗ: необработанная ошибка и ERROR-лог доходят до трекера,
при этом в событие не попадают тело запроса (текст клиента), заголовки (cookie,
secret_token webhook), query-строка, локальные переменные, токен бота из URL Bot API,
Bearer-токены и email. Без SENTRY_DSN трекер выключен. Сеть не нужна: события
перехватываются подменённым транспортом Sentry.

Запуск:  python smoke_test_stage7.py
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import pathlib
import sys

BASE = pathlib.Path(__file__).resolve().parent
DB = BASE / "test_smoke_stage7.db"
if DB.exists():
    DB.unlink()

os.environ.update(
    DATABASE_URL=f"sqlite:///{DB}",
    AUTO_CREATE_TABLES="true",
    JWT_SECRET="smoke-test-secret-key-at-least-32-characters-long",
    AUTH_COOKIE_SECURE="false",
    ENVIRONMENT="development",
    AI_PROVIDER="stub",
    REPROCESS_INTERVAL_SECONDS="0",
    SENTRY_DSN="https://publickey@sentry.invalid/1",
    SENTRY_TRACES_SAMPLE_RATE="0",
)
sys.path.insert(0, str(BASE))

import httpx  # noqa: E402
import sentry_sdk  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from sentry_sdk.transport import Transport  # noqa: E402

import main  # noqa: E402
import monitoring  # noqa: E402
from config import Settings, settings  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, extra: str = "") -> None:
    (PASSED if condition else FAILED).append(f"{name} {extra}".strip())
    print(("  OK  " if condition else "FAIL  ") + name + (f"  [{extra}]" if extra else ""))


BOT_TOKEN = "123456789:" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456_-"
CUSTOMER_TEXT = "Сколько стоит стрижка, мой телефон +79990001122"
WEBHOOK_SECRET = "webhook-secret-value-0123456789"
EMAIL = "client.person@example.com"
# Секреты задаются здесь, а не рядом с _boom: Sentry отправляет строки исходника
# вокруг места ошибки (context_line), и литерал в них дал бы ложное срабатывание.
QUERY_SECRET = "query-secret-value"


class CaptureTransport(Transport):
    """Транспорт Sentry без сети: складывает события в список."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict] = []

    def capture_envelope(self, envelope) -> None:  # type: ignore[override]
        for item in envelope.items:
            if item.type in ("event", "transaction"):
                self.events.append(json.loads(item.payload.get_bytes()))


def serialized(events: list[dict]) -> str:
    return json.dumps(events, ensure_ascii=False)


# --------------------------------------------------------------------------- #
print("\n[1] Включение и настройки")
check("SENTRY_DSN задан → трекер включён", main.MONITORING_ENABLED is True)
client = sentry_sdk.get_client()
check("клиент Sentry активен", client.is_active())
check("send_default_pii выключен", client.options["send_default_pii"] is False)
check("локальные переменные не отправляются", client.options["include_local_variables"] is False)
check("тела запросов не отправляются", client.options["max_request_body_size"] == "never")
check("environment из настроек", client.options["environment"] == settings.environment)

transport = CaptureTransport()
client.transport = transport

try:
    Settings(sentry_traces_sample_rate=2)  # pyright: ignore[reportCallIssue]
    check("SENTRY_TRACES_SAMPLE_RATE > 1 отклоняется", False)
except ValidationError:
    check("SENTRY_TRACES_SAMPLE_RATE > 1 отклоняется", True)

saved_dsn = settings.sentry_dsn
settings.sentry_dsn = None
check("без SENTRY_DSN трекер не включается", monitoring.init_monitoring() is False)
settings.sentry_dsn = saved_dsn

# --------------------------------------------------------------------------- #
print("\n[2] Очистка текста")
scrubbed = monitoring.scrub_text(f"GET https://api.telegram.org/bot{BOT_TOKEN}/sendMessage")
check("токен бота вырезан из URL", BOT_TOKEN not in scrubbed and "sendMessage" in scrubbed)
check("Bearer-токен вырезан", "eyJabc.def" not in monitoring.scrub_text("Bearer eyJabc.def"))
check("email вырезан", EMAIL not in monitoring.scrub_text(f"user {EMAIL} failed"))
check("обычный текст не меняется", monitoring.scrub_text("lead 42 HOT") == "lead 42 HOT")


# --------------------------------------------------------------------------- #
print("\n[3] Необработанная ошибка в запросе")


@main.app.post("/__stage7_boom")
def _boom() -> None:
    local_secret = BOT_TOKEN  # не должен попасть в событие как локальная переменная
    raise RuntimeError(f"сбой отправки bot{local_secret} для {EMAIL}")


with TestClient(main.app, raise_server_exceptions=False) as http:
    resp = http.post(
        f"/__stage7_boom?token={QUERY_SECRET}",
        content=json.dumps({"message": {"text": CUSTOMER_TEXT}}),
        headers={
            "Content-Type": "application/json",
            "X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET,
            "Authorization": "Bearer jwt-token-value-123",
            "Cookie": "leadpilot_access_token=cookie-session-value",
        },
    )
check("клиент получил 500 без деталей", resp.status_code == 500 and BOT_TOKEN not in resp.text)
sentry_sdk.flush()
check("событие ушло в Sentry", len(transport.events) >= 1, f"событий: {len(transport.events)}")
dump = serialized(transport.events)
check("в событии есть исключение", "RuntimeError" in dump)
check("токен бота вычищен", BOT_TOKEN not in dump)
check("email вычищен", EMAIL not in dump)
check("текст клиента не отправлен", CUSTOMER_TEXT not in dump)
check("secret_token webhook не отправлен", WEBHOOK_SECRET not in dump)
check("cookie сессии не отправлена", "cookie-session-value" not in dump)
check("Bearer из заголовка не отправлен", "jwt-token-value-123" not in dump)
check("query-строка не отправлена", QUERY_SECRET not in dump)
requests_data = [e.get("request", {}) for e in transport.events if e.get("request")]
check(
    "в request остались только method и url",
    bool(requests_data) and all(set(r) <= {"method", "url"} for r in requests_data),
    str([sorted(r) for r in requests_data]),
)

# --------------------------------------------------------------------------- #
print("\n[4] Ошибка канала: breadcrumb httpx с токеном в URL")
transport.events.clear()


def fake_bot_api(request: httpx.Request) -> httpx.Response:
    return httpx.Response(502, json={"ok": False})


with httpx.Client(transport=httpx.MockTransport(fake_bot_api)) as bot_client:
    bot_client.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"text": CUSTOMER_TEXT}
    )
logging.getLogger("leadpilot").error("Telegram вернул 502 при отправке ответа")
sentry_sdk.flush()
check("ERROR-лог стал событием", len(transport.events) == 1, f"событий: {len(transport.events)}")
dump = serialized(transport.events)
check("breadcrumb запроса к Bot API есть", "api.telegram.org" in dump)
check("токен бота в breadcrumb вычищен", BOT_TOKEN not in dump)
check("текст ответа клиенту не в событии", CUSTOMER_TEXT not in dump)

# --------------------------------------------------------------------------- #
sentry_sdk.get_client().close()
with contextlib.suppress(PermissionError):
    DB.unlink(missing_ok=True)

print(f"\nИТОГО: пройдено {len(PASSED)}, провалено {len(FAILED)}")
if FAILED:
    print("Провалены:")
    for name in FAILED:
        print("  -", name)
    sys.exit(1)
