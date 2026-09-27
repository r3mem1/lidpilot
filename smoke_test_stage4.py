"""
Проверочный скрипт этапа 4 — CRM-ядро (НЕ часть приложения, можно удалить).

Покрывает разделы 6.5, 11, 13, 14, 16, 17 и критерии приёмки 10, 11, 13, 15 ТЗ:
лиды и приоритет, фильтры, статусы и ответственные, ручной ответ менеджера,
передача диалога человеку, отметка «решено», клиенты и история обращений.
Telegram подменяется httpx.MockTransport, AI работает офлайн (AI_PROVIDER=stub).

Запуск:  python smoke_test_stage4.py
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sqlite3
import sys

BASE = pathlib.Path(__file__).resolve().parent
DB = BASE / "test_smoke_stage4.db"
if DB.exists():
    DB.unlink()

os.environ.update(
    DATABASE_URL=f"sqlite:///{DB}",
    AUTO_CREATE_TABLES="true",
    JWT_SECRET="smoke-test-secret-key-at-least-32-characters-long",
    AUTH_COOKIE_SECURE="false",
    ENVIRONMENT="development",
    AI_PROVIDER="stub",
    PUBLIC_BASE_URL="https://leadpilot.test",
    REPROCESS_INTERVAL_SECONDS="0",
    REPLY_DEBOUNCE_SECONDS="0",  # пауза серии сообщений — в тестах без ожидания
    TELEGRAM_MAX_RETRIES="2",
    BOOTSTRAP_ADMIN_EMAIL="admin@example.com",
    BOOTSTRAP_ADMIN_PASSWORD="Adm1n-Pass-123!",
)
sys.path.insert(0, str(BASE))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ai.context import HistoryRole  # noqa: E402
from database import SessionLocal  # noqa: E402
from integrations.telegram import TelegramClient  # noqa: E402
from main import app  # noqa: E402
from services import integration_service, message_service  # noqa: E402

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


def tg_ok(result: object) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def tg_err(code: int, description: str) -> httpx.Response:
    return httpx.Response(code, json={"ok": False, "error_code": code, "description": description})


class FakeTelegram:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.send_plan: list = []
        self._message_id = 7000

    def handler(self, request: httpx.Request) -> httpx.Response:
        match = re.match(r"^/bot([^/]+)/(\w+)$", request.url.path)
        assert match, request.url.path
        token, method = match.group(1), match.group(2)
        body = json.loads(request.content) if request.content else {}
        self.calls.append((token, method, body))
        if method == "getMe":
            bot_id = int(token.split(":")[0])
            return tg_ok({"id": bot_id, "is_bot": True, "username": f"bot{bot_id}"})
        if method in ("setWebhook", "deleteWebhook"):
            return tg_ok(True)
        if method == "sendMessage":
            if self.send_plan:
                return self.send_plan.pop(0)
            self._message_id += 1
            return tg_ok({"message_id": self._message_id})
        return tg_err(404, "Not Found")

    def sent(self, token: str | None = None) -> list[dict]:
        return [b for t, m, b in self.calls if m == "sendMessage" and (token is None or t == token)]

    def webhook_secret(self, token: str) -> str:
        return [b["secret_token"] for t, m, b in self.calls if m == "setWebhook" and t == token][-1]


fake = FakeTelegram()
integration_service.build_telegram_client = lambda token: TelegramClient(
    token,
    base_url="https://api.telegram.test",
    transport=httpx.MockTransport(fake.handler),
    sleep=lambda seconds: None,
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


def upd(update_id: int, chat: int, text: str | None) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat, "type": "private"},
            "from": {"id": chat, "is_bot": False, "first_name": "Иван", "username": f"user{chat}"},
            "text": text,
        },
    }


def logs(event_type: str) -> list[dict]:
    rows = db_rows(
        "SELECT level, message, metadata FROM system_logs WHERE event_type = ? ORDER BY id",
        event_type,
    )
    return [
        {"level": r["level"], "message": r["message"], "meta": json.loads(r["metadata"] or "{}")}
        for r in rows
    ]


PASSWORD = "Str0ng-Pass-1"
_uid = {"n": 0}


def next_update() -> int:
    _uid["n"] += 1
    return 1000 + _uid["n"]


with TestClient(app) as c:
    # ----------------------------------------------------------------------- #
    print("\n=== 0. Подготовка ===")
    emails = (
        "owner_a@example.com",
        "manager1@example.com",
        "manager2@example.com",
        "owner_b@example.com",
    )
    for email in emails:
        c.post("/auth/register", json={"email": email, "password": PASSWORD})

    def login(email: str, password: str | None = None) -> dict:
        token = c.post(
            "/auth/login", json={"email": email, "password": password or PASSWORD}
        ).json()["access_token"]
        c.cookies.clear()
        return {"Authorization": f"Bearer {token}"}

    ha, hm1, hm2, hb = (login(e) for e in emails)
    hadmin = login("admin@example.com", "Adm1n-Pass-123!")
    ids = {
        name: c.get("/me", headers=h).json()["user"]["id"]
        for name, h in (("owner_a", ha), ("m1", hm1), ("m2", hm2), ("owner_b", hb))
    }
    biz_a = c.post(
        "/businesses",
        headers=ha,
        json={
            "name": "Барбершоп «Бритва»",
            "phone": "+7 999 000-00-00",
            "working_hours": "пн-сб 10:00-21:00",
        },
    ).json()["id"]
    biz_b = c.post("/businesses", headers=hb, json={"name": "Салон «Лилия»"}).json()["id"]
    for name, price in (("Стрижка", "1500.00"), ("Борода", "1000.00")):
        c.post(f"/businesses/{biz_a}/services", headers=ha, json={"name": name, "price": price})
    c.post(
        f"/businesses/{biz_b}/services", headers=hb, json={"name": "Маникюр", "price": "2000.00"}
    )
    c.post(
        f"/businesses/{biz_a}/members",
        headers=ha,
        json={"email": "manager1@example.com", "role": "MANAGER"},
    )
    c.post(
        f"/businesses/{biz_a}/members",
        headers=ha,
        json={"email": "manager2@example.com", "role": "MANAGER"},
    )
    c.post(f"/businesses/{biz_a}/integrations/telegram", headers=ha, json={"bot_token": TOKEN_A})
    c.post(f"/businesses/{biz_b}/integrations/telegram", headers=hb, json={"bot_token": TOKEN_B})
    HDR_A = {"X-Telegram-Bot-Api-Secret-Token": fake.webhook_secret(TOKEN_A)}
    HDR_B = {"X-Telegram-Bot-Api-Secret-Token": fake.webhook_secret(TOKEN_B)}
    check("две компании, менеджеры и боты подключены", biz_a != biz_b and len(ids) == 4)

    def say(chat: int, text: str, hdr=None) -> None:
        r = c.post("/webhooks/telegram", json=upd(next_update(), chat, text), headers=hdr or HDR_A)
        assert r.status_code == 200, r.text

    def conv_of(chat: int, business: int = biz_a) -> sqlite3.Row:
        return db_rows(
            "SELECT c.* FROM conversations c JOIN customers u ON u.id = c.customer_id "
            "WHERE u.external_id = ? AND c.business_id = ? ORDER BY c.id DESC LIMIT 1",
            str(chat),
            business,
        )[0]

    def lead_of(chat: int, business: int = biz_a) -> sqlite3.Row | None:
        rows = db_rows(
            "SELECT l.* FROM leads l JOIN conversations c ON c.id = l.conversation_id "
            "JOIN customers u ON u.id = c.customer_id WHERE u.external_id = ? AND l.business_id = ? "
            "ORDER BY l.id DESC",
            str(chat),
            business,
        )
        return rows[0] if rows else None

    # ----------------------------------------------------------------------- #
    print("\n=== 1. Автоматическое создание лидов (разделы 6.5, 7) ===")
    say(3001, "Сколько стоит стрижка?")
    lead = lead_of(3001)
    check(
        "вопрос о цене → лид WARM/NEW",
        lead is not None
        and lead["priority"] == "WARM"
        and lead["status"] == "NEW"
        and lead["intent"] == "PRICE",
    )
    check(
        "причина классификации сохранена в лиде",
        bool(lead["reason"]) and "цен" in lead["reason"].lower(),
        str(lead["reason"]),
    )
    check("ответственного нет", lead["assigned_to"] is None)
    check(
        "событие LEAD_CREATED в логе",
        len(logs("LEAD_CREATED")) == 1 and logs("LEAD_CREATED")[0]["meta"]["lead_id"] == lead["id"],
    )
    say(3001, "Хочу записаться на завтра")
    lead2 = lead_of(3001)
    check(
        "запись повышает тот же лид до HOT (не создаёт второй)",
        lead2["id"] == lead["id"]
        and lead2["priority"] == "HOT"
        and lead2["intent"] == "BOOKING"
        and db_rows("SELECT COUNT(*) AS n FROM leads WHERE business_id = ?", biz_a)[0]["n"] == 1,
    )
    check(
        "причина обновилась вместе с приоритетом",
        "запис" in lead2["reason"].lower(),
        lead2["reason"],
    )
    say(3001, "Спасибо!")
    check("приоритет лида не падает после «Спасибо!»", lead_of(3001)["priority"] == "HOT")
    check("приоритет диалога тоже не падает", conv_of(3001)["priority"] == "HOT")

    say(3002, "Заработок в крипте, казино и ставки на спорт")
    check(
        "спам не создаёт лид",
        lead_of(3002) is None and conv_of(3002)["attention_reason"] == "SPAM_SUSPECTED",
    )
    say(3003, "Сколько стоит борода?")
    say(3004, "Привет")
    say(3005, "Ужасный сервис, верните деньги")
    check("общее сообщение → лид COLD", lead_of(3004)["priority"] == "COLD")
    check("жалоба → лид HOT", lead_of(3005)["priority"] == "HOT")
    check(
        "всего 4 лида (без спама)",
        db_rows("SELECT COUNT(*) AS n FROM leads WHERE business_id = ?", biz_a)[0]["n"] == 4,
    )

    say(9001, "Сколько стоит маникюр?", HDR_B)
    check(
        "лид компании B создан отдельно",
        lead_of(9001, biz_b) is not None and lead_of(9001, biz_b)["business_id"] == biz_b,
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 2. GET /businesses/{id}/leads (критерий 11, раздел 13) ===")
    url = f"/businesses/{biz_a}/leads"
    r = c.get(url, headers=ha)
    items = r.json()
    check("владелец получает список лидов", r.status_code == 200 and len(items) == 4)
    order = [i["lead"]["priority"] for i in items]
    check(
        "порядок: горячие → тёплые → холодные", order == ["HOT", "HOT", "WARM", "COLD"], str(order)
    )
    check(
        "в элементе лид, диалог и клиент",
        set(items[0]) == {"lead", "conversation", "customer"}
        and items[0]["customer"]["username"].startswith("user"),
    )
    hot = c.get(url, headers=ha, params={"priority": "HOT"}).json()
    check(
        "отдельный список горячих: priority=HOT",
        len(hot) == 2 and all(i["lead"]["priority"] == "HOT" for i in hot),
    )
    check("priority=WARM", len(c.get(url, headers=ha, params={"priority": "WARM"}).json()) == 1)
    check("priority=COLD", len(c.get(url, headers=ha, params={"priority": "COLD"}).json()) == 1)
    check("status=NEW: все 4", len(c.get(url, headers=ha, params={"status": "NEW"}).json()) == 4)
    check(
        "status=IN_PROGRESS: пока пусто",
        c.get(url, headers=ha, params={"status": "IN_PROGRESS"}).json() == [],
    )
    check(
        "unassigned=true: все 4",
        len(c.get(url, headers=ha, params={"unassigned": "true"}).json()) == 4,
    )
    check(
        "assigned_to=менеджер: пусто",
        c.get(url, headers=ha, params={"assigned_to": ids["m1"]}).json() == [],
    )
    check(
        "период: до 2000 года пусто",
        c.get(url, headers=ha, params={"date_to": "2000-01-01T00:00:00"}).json() == [],
    )
    check(
        "период: с 2000 года все",
        len(c.get(url, headers=ha, params={"date_from": "2000-01-01T00:00:00"}).json()) == 4,
    )
    check(
        "пагинация limit/offset",
        len(c.get(url, headers=ha, params={"limit": 1}).json()) == 1
        and c.get(url, headers=ha, params={"limit": 2, "offset": 3}).json()[0]["lead"]["priority"]
        == "COLD",
    )
    check(
        "невалидные фильтры → 422",
        c.get(url, headers=ha, params={"status": "BOGUS"}).status_code == 422
        and c.get(url, headers=ha, params={"priority": "URGENT"}).status_code == 422
        and c.get(url, headers=ha, params={"limit": 0}).status_code == 422,
    )
    check("MANAGER видит лиды своей компании", len(c.get(url, headers=hm1).json()) == 4)
    check("без токена → 401", c.get(url).status_code == 401)
    check("чужая компания → 404", c.get(url, headers=hb).status_code == 404)
    b_items = c.get(f"/businesses/{biz_b}/leads", headers=hb).json()
    a_ids = {i["lead"]["id"] for i in items}
    check(
        "владелец B видит только свой лид",
        len(b_items) == 1
        and b_items[0]["lead"]["id"] not in a_ids
        and b_items[0]["lead"]["business_id"] == biz_b,
    )
    check("ADMIN видит лиды любой компании", len(c.get(url, headers=hadmin).json()) == 4)

    # ----------------------------------------------------------------------- #
    print("\n=== 3. PATCH /leads/{id}: статус и ответственный (раздел 14) ===")
    lead_id = lead_of(3003)["id"]  # тёплый лид «Сколько стоит борода?»
    purl = f"/leads/{lead_id}"
    r = c.patch(purl, headers=hm1, json={"assigned_to": ids["m1"]})
    check(
        "MANAGER назначает ответственного → 200",
        r.status_code == 200 and r.json()["assigned_to"] == ids["m1"],
        r.text[:80],
    )
    upd_log = logs("LEAD_UPDATED")[-1]
    check(
        "LEAD_UPDATED: кто и что изменил",
        upd_log["meta"]["actor_user_id"] == ids["m1"]
        and upd_log["meta"]["assigned_to"]["to"] == ids["m1"],
    )
    check(
        "assigned_to=сотрудник другой компании → 422",
        c.patch(purl, headers=ha, json={"assigned_to": ids["owner_b"]}).status_code == 422,
    )
    check(
        "assigned_to=несуществующий → 422",
        c.patch(purl, headers=ha, json={"assigned_to": 99999}).status_code == 422,
    )
    check("назначение не изменилось после отказа", lead_of(3003)["assigned_to"] == ids["m1"])
    check(
        "смена ответственного на другого менеджера",
        c.patch(purl, headers=ha, json={"assigned_to": ids["m2"]}).json()["assigned_to"]
        == ids["m2"],
    )
    r = c.patch(purl, headers=ha, json={"assigned_to": None})
    check(
        "assigned_to=null снимает ответственного",
        r.status_code == 200 and r.json()["assigned_to"] is None,
    )
    check(
        "статус → IN_PROGRESS",
        c.patch(purl, headers=ha, json={"status": "IN_PROGRESS"}).json()["status"] == "IN_PROGRESS",
    )
    check("пустое тело → 400", c.patch(purl, headers=ha, json={}).status_code == 400)
    check("status=null → 422", c.patch(purl, headers=ha, json={"status": None}).status_code == 422)
    check(
        "неизвестный статус → 422",
        c.patch(purl, headers=ha, json={"status": "BOGUS"}).status_code == 422,
    )
    r = c.patch(
        purl, headers=ha, json={"status": "IN_PROGRESS", "business_id": 999, "priority": "COLD"}
    )
    check(
        "business_id и priority через PATCH не меняются",
        r.status_code == 200
        and lead_of(3003)["business_id"] == biz_a
        and lead_of(3003)["priority"] == "WARM",
    )
    check(
        "чужой владелец → 404",
        c.patch(purl, headers=hb, json={"status": "LOST"}).status_code == 404,
    )
    check(
        "несуществующий лид → 404",
        c.patch("/leads/99999", headers=ha, json={"status": "LOST"}).status_code == 404,
    )
    check("без токена → 401", c.patch(purl, json={"status": "LOST"}).status_code == 401)
    check("лид остался нетронутым после отказов", lead_of(3003)["status"] == "IN_PROGRESS")

    conv3003 = conv_of(3003)["id"]
    r = c.patch(purl, headers=hm2, json={"status": "RESOLVED"})
    check("RESOLVED → лид закрыт", r.status_code == 200 and r.json()["status"] == "RESOLVED")
    check(
        "RESOLVED закрывает диалог",
        conv_of(3003)["status"] == "RESOLVED" and conv_of(3003)["handled_by_manager"] == 0,
    )
    c.patch(purl, headers=ha, json={"status": "IN_PROGRESS"})
    check(
        "возврат в IN_PROGRESS переоткрывает диалог",
        conv_of(3003)["status"] == "OPEN" and conv_of(3003)["id"] == conv3003,
    )
    check(
        "LOST закрывает диалог",
        c.patch(purl, headers=ha, json={"status": "LOST"}).json()["status"] == "LOST"
        and conv_of(3003)["status"] == "RESOLVED",
    )
    check(
        "ADMIN может менять лид",
        c.patch(purl, headers=hadmin, json={"status": "NEW"}).status_code == 200,
    )
    check(
        "фильтр по ответственному после назначения",
        (
            c.patch(purl, headers=ha, json={"assigned_to": ids["m2"]})
            and len(c.get(url, headers=ha, params={"assigned_to": ids["m2"]}).json()) == 1
        ),
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 4. Ручной ответ менеджера (критерий 10, раздел 11) ===")
    conv5 = conv_of(3005)
    rurl = f"/conversations/{conv5['id']}/reply"
    check(
        "до ответа: жалоба ждёт менеджера, AI-шаблон уже отправлен",
        conv5["status"] == "NEEDS_ATTENTION" and conv5["handled_by_manager"] == 0,
    )
    text = "Здравствуйте! Приносим извинения, разберёмся и вернёмся с ответом сегодня."
    sent_before = len(fake.sent(TOKEN_A))
    r = c.post(rurl, headers=hm1, json={"text": text})
    check("ручной ответ → 201", r.status_code == 201, r.text[:120])
    reply = r.json()
    check(
        "ответ в теле: MANAGER, автор, доставлен",
        reply["sender_type"] == "MANAGER"
        and reply["author_user_id"] == ids["m1"]
        and reply["delivery_status"] == "SENT"
        and reply["text"] == text,
    )
    sent = fake.sent(TOKEN_A)
    check(
        "клиенту в Telegram ушёл именно этот текст в его чат",
        len(sent) == sent_before + 1 and sent[-1]["chat_id"] == "3005" and sent[-1]["text"] == text,
    )
    row = db_rows("SELECT * FROM messages WHERE id = ?", reply["id"])[0]
    check(
        "сообщение сохранено: sender MANAGER, external id Telegram",
        row["sender_type"] == "MANAGER"
        and row["external_message_id"]
        and row["business_id"] == biz_a,
    )
    conv5 = conv_of(3005)
    lead5 = lead_of(3005)
    check(
        "диалог у менеджера: OPEN, AI отключён",
        conv5["status"] == "OPEN"
        and conv5["handled_by_manager"] == 1
        and conv5["attention_reason"] is None,
    )
    check(
        "лид → IN_PROGRESS, ответственный = ответивший",
        lead5["status"] == "IN_PROGRESS" and lead5["assigned_to"] == ids["m1"],
    )
    ev = logs("MANAGER_REPLY")[-1]["meta"]
    check(
        "MANAGER_REPLY в логе: автор, диалог, лид",
        ev["actor_user_id"] == ids["m1"]
        and ev["conversation_id"] == conv5["id"]
        and ev["lead_id"] == lead5["id"],
    )
    c.post(rurl, headers=hm2, json={"text": "Дополню: мы свяжемся по телефону."})
    check(
        "второй менеджер не перехватывает ответственность",
        lead_of(3005)["assigned_to"] == ids["m1"],
    )

    check("пустой текст → 422", c.post(rurl, headers=hm1, json={"text": ""}).status_code == 422)
    check(
        "текст из пробелов → 422",
        c.post(rurl, headers=hm1, json={"text": "   \n "}).status_code == 422,
    )
    check(
        "текст длиннее 4000 → 422",
        c.post(rurl, headers=hm1, json={"text": "а" * 4001}).status_code == 422,
    )
    check("без тела → 422", c.post(rurl, headers=hm1).status_code == 422)
    check("без токена → 401", c.post(rurl, json={"text": "x"}).status_code == 401)
    check("чужой владелец → 404", c.post(rurl, headers=hb, json={"text": "x"}).status_code == 404)
    check(
        "несуществующий диалог → 404",
        c.post("/conversations/99999/reply", headers=hm1, json={"text": "x"}).status_code == 404,
    )
    check("отказы не отправляли сообщений", len(fake.sent(TOKEN_A)) == sent_before + 2)
    check(
        "владелец тоже может ответить",
        c.post(rurl, headers=ha, json={"text": "Это владелец."}).status_code == 201,
    )

    # доставка не удалась (клиент заблокировал бота)
    say(3006, "Сколько стоит стрижка?")
    c6 = conv_of(3006)["id"]
    fake.send_plan = [tg_err(403, "Forbidden: bot was blocked by the user")]
    r = c.post(f"/conversations/{c6}/reply", headers=hm1, json={"text": "Добрый день!"})
    check(
        "403 Telegram: ответ сохранён, статус FAILED (201)",
        r.status_code == 201
        and r.json()["delivery_status"] == "FAILED"
        and r.json()["delivery_error"],
        r.text[:120],
    )
    check(
        "клиент помечен, диалог у менеджера с причиной",
        db_rows("SELECT channel_blocked FROM customers WHERE external_id='3006'")[0][0] == 1
        and conv_of(3006)["status"] == "NEEDS_ATTENTION"
        and conv_of(3006)["attention_reason"] == "DELIVERY_FAILED",
    )
    # временный сбой → PENDING → sweeper
    say(3007, "Сколько стоит стрижка?")
    c7 = conv_of(3007)["id"]
    fake.send_plan = [tg_err(502, "Bad Gateway")] * 3
    r = c.post(f"/conversations/{c7}/reply", headers=hm1, json={"text": "Уточню и отвечу."})
    check(
        "502 Telegram: ответ ждёт повтора (PENDING), запрос успешен",
        r.status_code == 201 and r.json()["delivery_status"] == "PENDING",
    )
    message_service._GRACE_SECONDS = 0
    handled = message_service.reprocess_pending()
    check(
        "sweeper доставил отложенный ответ менеджера",
        handled >= 1
        and db_rows("SELECT delivery_status FROM messages WHERE id = ?", r.json()["id"])[0][0]
        == "SENT",
    )
    message_service._GRACE_SECONDS = 30

    # компания приостановлена
    db_exec("UPDATE businesses SET status = 'SUSPENDED' WHERE id = ?", biz_a)
    s_before = len(fake.sent(TOKEN_A))
    check(
        "приостановленная компания: ответ менеджера → 403",
        c.post(rurl, headers=hm1, json={"text": "x"}).status_code == 403,
    )
    check(
        "приостановленная компания: ADMIN может отвечать",
        c.post(rurl, headers=hadmin, json={"text": "Служба поддержки LeadPilot."}).status_code
        == 201
        and len(fake.sent(TOKEN_A)) == s_before + 1,
    )
    db_exec("UPDATE businesses SET status = 'TRIAL' WHERE id = ?", biz_a)

    # ----------------------------------------------------------------------- #
    print("\n=== 5. Передача диалога менеджеру: AI молчит (раздел 14) ===")
    s_before = len(fake.sent(TOKEN_A))
    ai_before = db_rows("SELECT COUNT(*) AS n FROM ai_responses")[0]["n"]
    say(3005, "Сколько стоит стрижка?")
    mid = db_rows(
        "SELECT id FROM messages WHERE sender_type='CUSTOMER' AND conversation_id = ? ORDER BY id DESC LIMIT 1",
        conv5["id"],
    )[0]["id"]
    m = db_rows("SELECT * FROM messages WHERE id = ?", mid)[0]
    check(
        "клиент пишет, диалог ведёт менеджер: AI не отвечает",
        len(fake.sent(TOKEN_A)) == s_before
        and db_rows("SELECT COUNT(*) AS n FROM ai_responses")[0]["n"] == ai_before,
    )
    check(
        "сообщение сохранено и обработано (DONE), intent не проставлен",
        m["processing_status"] == "DONE" and m["intent"] is None,
    )
    check(
        "диалог снова требует внимания: CUSTOMER_REPLIED",
        conv_of(3005)["status"] == "NEEDS_ATTENTION"
        and conv_of(3005)["attention_reason"] == "CUSTOMER_REPLIED"
        and conv_of(3005)["handled_by_manager"] == 1,
    )
    skip = [e for e in logs("MESSAGE_SKIPPED") if e["meta"].get("message_id") == mid]
    check(
        "в логе MESSAGE_SKIPPED: MANAGER_HANDLING",
        len(skip) == 1 and skip[0]["meta"]["reason"] == "MANAGER_HANDLING",
    )
    check(
        "лид не пострадал",
        lead_of(3005)["status"] == "IN_PROGRESS" and lead_of(3005)["assigned_to"] == ids["m1"],
    )
    with SessionLocal() as db:
        history = message_service._load_history(db, conv5["id"], mid)
    check(
        "для AI история включает ответы менеджера",
        HistoryRole.MANAGER in {turn.role for turn in history}
        and HistoryRole.CUSTOMER in {turn.role for turn in history},
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 6. «Решено» (раздел 14) ===")
    surl = f"/conversations/{conv5['id']}/resolve"
    check("без токена → 401", c.post(surl).status_code == 401)
    check("чужой владелец → 404", c.post(surl, headers=hb).status_code == 404)
    check(
        "несуществующий диалог → 404",
        c.post("/conversations/99999/resolve", headers=hm1).status_code == 404,
    )
    check("отказы не закрыли диалог", conv_of(3005)["status"] == "NEEDS_ATTENTION")
    r = c.post(surl, headers=hm1)
    body = r.json()
    check(
        "resolve → 200: диалог RESOLVED, лид RESOLVED",
        r.status_code == 200
        and body["conversation"]["status"] == "RESOLVED"
        and body["lead"]["status"] == "RESOLVED"
        and body["conversation"]["handled_by_manager"] is False,
    )
    check(
        "resolve идемпотентен",
        c.post(surl, headers=hm1).status_code == 200
        and len(
            [
                e
                for e in logs("CONVERSATION_RESOLVED")
                if e["meta"]["conversation_id"] == conv5["id"]
            ]
        )
        == 1,
    )
    s_before = len(fake.sent(TOKEN_A))
    say(3005, "Сколько стоит стрижка?")
    new_conv = conv_of(3005)
    check(
        "после «решено» новое сообщение открывает НОВЫЙ диалог",
        new_conv["id"] != conv5["id"]
        and new_conv["status"] in ("OPEN", "NEEDS_ATTENTION")
        and new_conv["handled_by_manager"] == 0,
    )
    check(
        "AI снова отвечает клиенту",
        len(fake.sent(TOKEN_A)) == s_before + 1 and fake.sent(TOKEN_A)[-1]["chat_id"] == "3005",
    )
    new_lead = lead_of(3005)
    check(
        "создан новый лид (старый закрыт)",
        new_lead["conversation_id"] == new_conv["id"]
        and new_lead["status"] == "NEW"
        and db_rows(
            "SELECT COUNT(*) AS n FROM leads l JOIN conversations c ON c.id=l.conversation_id "
            "JOIN customers u ON u.id=c.customer_id WHERE u.external_id='3005' AND l.business_id=?",
            biz_a,
        )[0]["n"]
        == 2,
    )
    # ответ менеджера в закрытый диалог переоткрывает его
    c.post(
        f"/conversations/{conv5['id']}/reply",
        headers=hm2,
        json={"text": "Вернулся к вашему вопросу."},
    )
    check(
        "ответ в закрытый диалог переоткрывает диалог и лид",
        db_rows("SELECT status, handled_by_manager FROM conversations WHERE id = ?", conv5["id"])[
            0
        ][:]
        == ("OPEN", 1)
        and db_rows("SELECT status FROM leads WHERE conversation_id = ?", conv5["id"])[0][0]
        == "IN_PROGRESS",
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 7. Диалоги: лид и признаки для рабочего места менеджера ===")
    lst = c.get(f"/businesses/{biz_a}/conversations", headers=hm1).json()
    check(
        "в списке диалогов есть лид и признак handled_by_manager",
        all("lead" in i and "handled_by_manager" in i["conversation"] for i in lst)
        and any(i["lead"] and i["lead"]["priority"] == "HOT" for i in lst),
    )
    spam_item = next(i for i in lst if i["customer"]["username"] == "user3002")
    check("у спама лида нет", spam_item["lead"] is None)
    check("последнее сообщение в списке", all(i["last_message"] is not None for i in lst))
    detail = c.get(f"/conversations/{conv5['id']}", headers=hm1).json()
    check(
        "карточка: лид с причиной, сообщения клиента/AI/менеджера",
        detail["lead"]["reason"]
        and {"CUSTOMER", "AI", "MANAGER"} <= {m["sender_type"] for m in detail["messages"]},
    )
    check(
        "карточка: автор ручного ответа виден",
        any(
            m["sender_type"] == "MANAGER"
            and m["author_user_id"] in (ids["m1"], ids["m2"], ids["owner_a"])
            for m in detail["messages"]
        ),
    )
    check(
        "карточка: решение AI (классификация и причина эскалации)",
        any(d["escalation_reason"] == "COMPLAINT" for d in detail["ai_decisions"]),
    )
    q = c.get(
        f"/businesses/{biz_a}/conversations", headers=hm1, params={"status": "NEEDS_ATTENTION"}
    ).json()
    check(
        "очередь «требует внимания» фильтруется по статусу",
        len(q) >= 1 and all(i["conversation"]["status"] == "NEEDS_ATTENTION" for i in q),
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 8. Клиенты и история обращений (раздел 13) ===")
    curl = f"/businesses/{biz_a}/customers"
    cust = c.get(curl, headers=ha).json()
    check("список клиентов компании A", len(cust) == 7, str(len(cust)))
    c3005 = next(x for x in cust if x["customer"]["username"] == "user3005")
    check(
        "у клиента 3005 два обращения и время активности",
        c3005["conversations_count"] == 2 and c3005["last_activity_at"],
    )
    check(
        "поиск по username",
        [
            x["customer"]["username"]
            for x in c.get(curl, headers=ha, params={"search": "user3005"}).json()
        ]
        == ["user3005"],
    )
    check("поиск по имени", len(c.get(curl, headers=ha, params={"search": "иван"}).json()) == 7)
    check(
        "подстановочные символы в поиске не работают как маски",
        c.get(curl, headers=ha, params={"search": "%"}).json() == []
        and c.get(curl, headers=ha, params={"search": "_"}).json() == [],
    )
    check("пагинация клиентов", len(c.get(curl, headers=ha, params={"limit": 2}).json()) == 2)
    check(
        "клиенты другой компании не видны",
        len(c.get(f"/businesses/{biz_b}/customers", headers=hb).json()) == 1
        and c.get(curl, headers=hb).status_code == 404
        and c.get(curl).status_code == 401,
    )
    cid = c3005["customer"]["id"]
    hist = c.get(f"/customers/{cid}", headers=hm1)
    check(
        "история клиента: оба диалога с лидами",
        hist.status_code == 200
        and len(hist.json()["conversations"]) == 2
        and all(i["lead"] for i in hist.json()["conversations"]),
    )
    stamps = [i["conversation"]["updated_at"] for i in hist.json()["conversations"]]
    check(
        "история: сверху последняя активность", stamps == sorted(stamps, reverse=True), str(stamps)
    )
    check("клиент другой компании → 404", c.get(f"/customers/{cid}", headers=hb).status_code == 404)
    check(
        "клиент без токена → 401, несуществующий → 404",
        c.get(f"/customers/{cid}").status_code == 401
        and c.get("/customers/99999", headers=ha).status_code == 404,
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 9. Изоляция компаний и секреты (критерий 13, раздел 16) ===")
    conv_b = db_rows("SELECT id FROM conversations WHERE business_id = ?", biz_b)[0]["id"]
    lead_b = lead_of(9001, biz_b)["id"]
    check(
        "владелец A не читает диалог B",
        c.get(f"/conversations/{conv_b}", headers=ha).status_code == 404,
    )
    check(
        "владелец A не отвечает в диалог B",
        c.post(f"/conversations/{conv_b}/reply", headers=ha, json={"text": "x"}).status_code == 404,
    )
    check(
        "владелец A не закрывает диалог B",
        c.post(f"/conversations/{conv_b}/resolve", headers=ha).status_code == 404,
    )
    check(
        "владелец A не правит лид B",
        c.patch(f"/leads/{lead_b}", headers=ha, json={"status": "LOST"}).status_code == 404,
    )
    customer_b = db_rows("SELECT id FROM customers WHERE business_id = ?", biz_b)[0]["id"]
    check(
        "владелец A не видит клиента B",
        c.get(f"/customers/{customer_b}", headers=ha).status_code == 404,
    )
    check(
        "данные компании B не изменились",
        lead_of(9001, biz_b)["status"] == "NEW"
        and db_rows(
            "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ? AND sender_type='MANAGER'",
            conv_b,
        )[0]["n"]
        == 0,
    )
    check("попытки записаны как ACCESS_DENIED", len(logs("ACCESS_DENIED")) >= 5)
    everything = json.dumps(
        [
            c.get(f"/businesses/{biz_a}/leads", headers=ha).json(),
            c.get(f"/conversations/{conv5['id']}", headers=ha).json(),
            c.get(curl, headers=ha).json(),
        ],
        ensure_ascii=False,
    )
    check(
        "в ответах API нет токенов ботов и секретов",
        TOKEN_A not in everything
        and "credentials" not in everything
        and HDR_A["X-Telegram-Bot-Api-Secret-Token"] not in everything,
    )

print("\n" + "=" * 70)
print(f"ИТОГО: пройдено {len(PASSED)}, провалено {len(FAILED)}")
if FAILED:
    print("Провалены:")
    for name in FAILED:
        print("  -", name)
    sys.exit(1)
