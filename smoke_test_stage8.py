"""
Проверочный скрипт этапа 8 — SaaS-автоматизация без платежей (НЕ часть приложения, можно удалить).

Покрывает разделы 15, 18, 20 ТЗ: чек-лист onboarding на дашборде (шаги по фактическим
данным, только владельцу, у каждой компании свой), срок подписки (истёкший trial,
отменённая подписка, бессрочная), выключение AI по окончании срока без потери сообщений,
ручной ответ менеджера при истёкшем сроке, баннеры в кабинете, блок «Тариф» в настройках,
продление администратором. Telegram подменяется httpx.MockTransport, AI офлайн (stub).

Запуск:  python smoke_test_stage8.py
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import re
import sqlite3
import sys
from datetime import UTC, datetime, timedelta

BASE = pathlib.Path(__file__).resolve().parent
DB = BASE / "test_smoke_stage8.db"
if DB.exists():
    DB.unlink()

BILLING = "billing@leadpilot.test"
os.environ.update(
    DATABASE_URL=f"sqlite:///{DB}",
    AUTO_CREATE_TABLES="true",
    JWT_SECRET="smoke-test-secret-key-at-least-32-characters-long",
    AUTH_COOKIE_SECURE="false",
    ENVIRONMENT="development",
    AI_PROVIDER="stub",
    AI_PREVIEW_ENABLED="true",
    PUBLIC_BASE_URL="https://leadpilot.test",
    REPROCESS_INTERVAL_SECONDS="0",
    SYSTEM_LOGS_PURGE_INTERVAL_HOURS="0",
    AUTH_RATE_LIMIT_ATTEMPTS="1000",
    BOOTSTRAP_ADMIN_EMAIL="admin@example.com",
    BOOTSTRAP_ADMIN_PASSWORD="Adm1n-Pass-123!",
    TRIAL_DAYS="14",
    SUBSCRIPTION_WARNING_DAYS="3",
    BILLING_CONTACT=BILLING,
)
sys.path.insert(0, str(BASE))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from integrations.telegram import TelegramClient  # noqa: E402
from main import app  # noqa: E402
from services import integration_service  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, extra: str = "") -> None:
    (PASSED if condition else FAILED).append(f"{name} {extra}".strip())
    print(("  OK  " if condition else "FAIL  ") + name + (f"  [{extra}]" if extra else ""))


# --------------------------------------------------------------------------- #
# Фейковый Telegram Bot API
# --------------------------------------------------------------------------- #
TOKEN_A = "111111:" + "A" * 35
TOKEN_B = "222222:" + "B" * 35


class FakeTelegram:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._id = 9000

    def handler(self, request: httpx.Request) -> httpx.Response:
        match = re.match(r"^/bot([^/]+)/(\w+)$", request.url.path)
        assert match
        token, method = match.group(1), match.group(2)
        body = json.loads(request.content) if request.content else {}
        self.calls.append((token, method, body))
        if method == "getMe":
            bot_id = int(token.split(":")[0])
            result: object = {"id": bot_id, "is_bot": True, "username": f"bot{bot_id}"}
        elif method == "sendMessage":
            self._id += 1
            result = {"message_id": self._id}
        else:
            result = True
        return httpx.Response(200, json={"ok": True, "result": result})

    def sent(self, token: str, chat: int | None = None) -> list[dict]:
        return [
            b
            for t, m, b in self.calls
            if m == "sendMessage"
            and t == token
            and (chat is None or b.get("chat_id") in (chat, str(chat)))
        ]

    def secret(self, token: str) -> str:
        return [b["secret_token"] for t, m, b in self.calls if m == "setWebhook" and t == token][-1]


fake = FakeTelegram()
integration_service.build_telegram_client = lambda token: TelegramClient(
    token,
    base_url="https://api.telegram.test",
    transport=httpx.MockTransport(fake.handler),
    sleep=lambda _seconds: None,
)


def db_rows(sql: str, *params) -> list[sqlite3.Row]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def db_exec(sql: str, *params) -> None:
    conn = sqlite3.connect(DB)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def set_expires(business_id: int, value: datetime | None) -> None:
    stored = value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f") if value else None
    db_exec("UPDATE subscriptions SET expires_at = ? WHERE business_id = ?", stored, business_id)


PWD = "Str0ng-Pass-1"
ADMIN_PWD = "Adm1n-Pass-123!"
_uid = {"n": 0}


def upd(chat: int, text: str) -> dict:
    _uid["n"] += 1
    n = _uid["n"]
    return {
        "update_id": n,
        "message": {
            "message_id": n,
            "chat": {"id": chat, "type": "private"},
            "from": {"id": chat, "is_bot": False, "first_name": "Иван", "username": f"user{chat}"},
            "text": text,
        },
    }


def progress(html: str) -> str | None:
    match = re.search(r'<span class="tag">(\d) из 4</span>', html)
    return match.group(1) if match else None


with TestClient(app) as c:
    # ----------------------------------------------------------------------- #
    print("\n=== 0. Подготовка ===")
    for name in ("owner_a", "manager_a", "owner_b"):
        c.post("/auth/register", json={"email": f"{name}@example.com", "password": PWD})

    def bearer(email: str, password: str = PWD) -> dict:
        token = c.post("/auth/login", json={"email": email, "password": password}).json()[
            "access_token"
        ]
        c.cookies.clear()
        return {"Authorization": f"Bearer {token}"}

    def page_as(email: str, path: str, password: str = PWD) -> httpx.Response:
        c.cookies.clear()
        c.post("/auth/login", json={"email": email, "password": password})
        response = c.get(path)
        c.cookies.clear()
        return response

    H = {name: bearer(f"{name}@example.com") for name in ("owner_a", "manager_a", "owner_b")}
    HA = bearer("admin@example.com", ADMIN_PWD)
    biz_a = c.post("/businesses", headers=H["owner_a"], json={"name": "Барбершоп «Бритва»"}).json()[
        "id"
    ]
    biz_b = c.post("/businesses", headers=H["owner_b"], json={"name": "Салон «Лилия»"}).json()["id"]
    c.post(
        f"/businesses/{biz_a}/members",
        headers=H["owner_a"],
        json={"email": "manager_a@example.com", "role": "MANAGER"},
    )
    dash_a, dash_b = f"/cabinet/{biz_a}", f"/cabinet/{biz_b}"

    # ----------------------------------------------------------------------- #
    print("\n=== 1. Onboarding: шаги по фактическим данным ===")
    html = page_as("owner_a@example.com", dash_a).text
    check(
        "новая компания: чек-лист показан, 0 из 4",
        "Осталось настроить" in html and progress(html) == "0",
    )
    check("у невыполненных шагов есть кнопки", ">К настройкам<" in html and ">К услугам<" in html)
    html_m = page_as("manager_a@example.com", dash_a).text
    check("менеджер чек-лист не видит", "Осталось настроить" not in html_m)

    c.put(f"/businesses/{biz_a}", headers=H["owner_a"], json={"address": "ул. Ленина, 1"})
    html = page_as("owner_a@example.com", dash_a).text
    check("частично заполненный профиль шаг не закрывает", progress(html) == "0")
    c.put(
        f"/businesses/{biz_a}",
        headers=H["owner_a"],
        json={"phone": "+7 900 000-00-00", "working_hours": "10:00–21:00"},
    )
    html = page_as("owner_a@example.com", dash_a).text
    check("адрес, телефон и график → 1 из 4", progress(html) == "1")

    svc = c.post(
        f"/businesses/{biz_a}/services",
        headers=H["owner_a"],
        json={"name": "Стрижка", "price": "1500", "active": False},
    ).json()
    html = page_as("owner_a@example.com", dash_a).text
    check("неактивная услуга шаг не закрывает", progress(html) == "1")
    c.put(f"/services/{svc['id']}", headers=H["owner_a"], json={"active": True})
    html = page_as("owner_a@example.com", dash_a).text
    check("активная услуга → 2 из 4", progress(html) == "2" and ">К услугам<" not in html)

    c.post(
        f"/businesses/{biz_a}/integrations/telegram",
        headers=H["owner_a"],
        json={"bot_token": TOKEN_A},
    )
    HOOK_A = {"X-Telegram-Bot-Api-Secret-Token": fake.secret(TOKEN_A)}
    html = page_as("owner_a@example.com", dash_a).text
    check("бот подключён → 3 из 4", progress(html) == "3")

    html_b = page_as("owner_b@example.com", dash_b).text
    check("у другой компании свой чек-лист (0 из 4)", progress(html_b) == "0")

    r = c.post("/webhooks/telegram", json=upd(101, "Сколько стоит стрижка?"), headers=HOOK_A)
    html = page_as("owner_a@example.com", dash_a).text
    check(
        "первое сообщение → чек-лист исчез",
        r.status_code == 200 and "Осталось настроить" not in html,
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 2. Срок действует: AI отвечает, баннеров нет ===")
    check("по trial AI ответил клиенту", len(fake.sent(TOKEN_A, 101)) >= 1)
    check(
        "при действующем сроке баннера нет",
        "notice--error" not in html and "notice--warn" not in html,
    )
    settings_html = page_as("owner_a@example.com", f"{dash_a}/settings").text
    check(
        "в настройках блок «Тариф»: цены и контакт для оплаты",
        "<h2>Тариф</h2>" in settings_html
        and "1990 ₽" in settings_html
        and "4990 ₽" in settings_html
        and BILLING in settings_html,
    )
    check(
        "в настройках виден пробный тариф",
        "Текущий тариф: <strong>Пробный</strong>" in settings_html,
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 3. Скоро закончится: предупреждение ===")
    set_expires(biz_a, datetime.now(UTC) + timedelta(days=2, hours=-1))
    html = page_as("owner_a@example.com", dash_a).text
    check(
        "за 2 дня — предупреждение с контактом",
        "notice--warn" in html and "осталось 2 дн." in html and BILLING in html,
    )
    html_m = page_as("manager_a@example.com", f"{dash_a}/messages").text
    check("предупреждение видит и менеджер", "осталось 2 дн." in html_m)
    set_expires(biz_a, datetime.now(UTC) + timedelta(days=10))
    html = page_as("owner_a@example.com", dash_a).text
    check("за 10 дней предупреждения нет", "notice--warn" not in html)

    # ----------------------------------------------------------------------- #
    print("\n=== 4. Срок истёк: AI выключен, сообщение не теряется ===")
    set_expires(biz_a, datetime.now(UTC) - timedelta(hours=1))
    before = len(fake.sent(TOKEN_A))
    r = c.post("/webhooks/telegram", json=upd(102, "Сколько стоит стрижка?"), headers=HOOK_A)
    check("webhook отвечает 200 (Telegram не повторяет)", r.status_code == 200)
    check("клиенту ничего не отправлено", len(fake.sent(TOKEN_A)) == before)
    msg = db_rows(
        "SELECT m.id, m.processing_status, m.processing_error, m.conversation_id FROM messages m "
        "JOIN conversations cv ON cv.id = m.conversation_id JOIN customers cu ON cu.id = cv.customer_id "
        "WHERE cu.external_id = '102' ORDER BY m.id"
    )
    check(
        "сообщение сохранено и обработано", len(msg) == 1 and msg[0]["processing_status"] == "DONE"
    )
    conv = db_rows(
        "SELECT status, attention_reason FROM conversations WHERE id = ?", msg[0]["conversation_id"]
    )[0]
    check(
        "диалог «требует внимания» с причиной SUBSCRIPTION_EXPIRED",
        conv["status"] == "NEEDS_ATTENTION" and conv["attention_reason"] == "SUBSCRIPTION_EXPIRED",
    )
    ai_rows = db_rows("SELECT COUNT(*) AS n FROM ai_responses WHERE message_id = ?", msg[0]["id"])
    check("AI не вызывался (нет ai_responses)", ai_rows[0]["n"] == 0)
    skipped = db_rows(
        "SELECT level, message FROM system_logs WHERE event_type = 'MESSAGE_SKIPPED' AND business_id = ?",
        biz_a,
    )
    check(
        "событие MESSAGE_SKIPPED (WARNING) о сроке подписки",
        any(row["level"] == "WARNING" and "Срок подписки" in row["message"] for row in skipped),
    )
    html = page_as("owner_a@example.com", dash_a).text
    check(
        "баннер: пробный период закончился + контакт",
        "notice--error" in html and "Пробный период закончился" in html and BILLING in html,
    )
    html_m = page_as("manager_a@example.com", f"{dash_a}/messages").text
    check("менеджер видит баннер и причину в диалогах", "Пробный период закончился" in html_m)
    r = c.post(
        f"/businesses/{biz_a}/ai/preview",
        headers=H["owner_a"],
        json={"text": "Сколько стоит стрижка?"},
    )
    check("проверка ответов AI при истёкшем сроке — 402", r.status_code == 402, str(r.status_code))
    r = c.post(
        f"/conversations/{msg[0]['conversation_id']}/reply",
        headers=H["manager_a"],
        json={"text": "Стрижка стоит 1500 ₽, ждём вас!"},
    )
    check(
        "менеджер отвечает вручную при истёкшем сроке",
        r.status_code == 201 and len(fake.sent(TOKEN_A, 102)) == 1,
        str(r.status_code),
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 5. Изоляция: срок одной компании не влияет на другую ===")
    c.post(
        f"/businesses/{biz_b}/services",
        headers=H["owner_b"],
        json={"name": "Маникюр", "price": "1200"},
    )
    c.post(
        f"/businesses/{biz_b}/integrations/telegram",
        headers=H["owner_b"],
        json={"bot_token": TOKEN_B},
    )
    HOOK_B = {"X-Telegram-Bot-Api-Secret-Token": fake.secret(TOKEN_B)}
    c.post("/webhooks/telegram", json=upd(201, "Сколько стоит маникюр?"), headers=HOOK_B)
    check("компания B с действующим trial получает ответ AI", len(fake.sent(TOKEN_B, 201)) >= 1)
    html_b = page_as("owner_b@example.com", dash_b).text
    check("у компании B баннера об истечении нет", "notice--error" not in html_b)

    # ----------------------------------------------------------------------- #
    print("\n=== 6. Продление администратором возвращает AI ===")
    r = c.put(
        f"/admin/businesses/{biz_a}/plan", headers=HA, json={"plan": "START", "extend_days": 30}
    )
    check(
        "ADMIN продлил на 30 дней с тарифом START",
        r.status_code == 200 and r.json()["plan"] == "START",
    )
    before = len(fake.sent(TOKEN_A, 103))
    c.post("/webhooks/telegram", json=upd(103, "Сколько стоит стрижка?"), headers=HOOK_A)
    check("после продления AI снова отвечает", len(fake.sent(TOKEN_A, 103)) > before)
    html = page_as("owner_a@example.com", dash_a).text
    check("после продления баннера нет", "notice--error" not in html and "notice--warn" not in html)
    settings_html = page_as("owner_a@example.com", f"{dash_a}/settings").text
    check("в настройках тариф «Старт»", "Текущий тариф: <strong>Старт</strong>" in settings_html)

    set_expires(biz_a, datetime.now(UTC) - timedelta(days=1))
    html = page_as("owner_a@example.com", dash_a).text
    check("истёкший платный тариф — «Срок тарифа закончился»", "Срок тарифа закончился" in html)

    # ----------------------------------------------------------------------- #
    print("\n=== 7. Отменённая и бессрочная подписка ===")
    set_expires(biz_a, None)
    c.put(f"/admin/businesses/{biz_a}/plan", headers=HA, json={"status": "CANCELED"})
    before = len(fake.sent(TOKEN_A))
    c.post("/webhooks/telegram", json=upd(104, "Сколько стоит стрижка?"), headers=HOOK_A)
    check("отменённая подписка — AI не отвечает", len(fake.sent(TOKEN_A)) == before)
    c.put(f"/admin/businesses/{biz_a}/plan", headers=HA, json={"status": "ACTIVE"})
    set_expires(biz_a, None)
    c.post("/webhooks/telegram", json=upd(105, "Сколько стоит стрижка?"), headers=HOOK_A)
    check("бессрочная подписка (без даты) — AI отвечает", len(fake.sent(TOKEN_A, 105)) >= 1)
    settings_html = page_as("owner_a@example.com", f"{dash_a}/settings").text
    check("в настройках «без ограничения срока»", "без ограничения срока" in settings_html)

    # ----------------------------------------------------------------------- #
    print("\n=== 8. Доступ ===")
    r = page_as("manager_a@example.com", f"{dash_a}/settings")
    check("менеджеру раздел «Настройки» (тариф) недоступен", "<h2>Тариф</h2>" not in r.text)
    r = page_as("owner_b@example.com", f"{dash_a}/settings")
    check("чужой владелец не видит тариф компании A", "<h2>Тариф</h2>" not in r.text)

with contextlib.suppress(PermissionError):
    DB.unlink(missing_ok=True)

print(f"\nИТОГО: пройдено {len(PASSED)}, провалено {len(FAILED)}")
if FAILED:
    print("Провалены:")
    for name in FAILED:
        print("  -", name)
    sys.exit(1)
