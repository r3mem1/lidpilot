"""
Проверочный скрипт этапа 3 — Telegram (НЕ часть приложения, можно удалить).

Покрывает разделы 7, 11, 16, 17, 18 и критерии приёмки 2, 3, 4, 8, 13, 15 ТЗ
без обращений в интернет: Telegram Bot API подменяется httpx.MockTransport,
LLM работает в офлайн-режиме (AI_PROVIDER=stub) либо подменяется тестовым клиентом.

Запуск:  python smoke_test_stage3.py
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import sqlite3
import sys
import time
from unittest.mock import patch

BASE = pathlib.Path(__file__).resolve().parent
DB = BASE / "test_smoke_stage3.db"
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
    REPLY_DEBOUNCE_SECONDS="0",  # пауза серии сообщений — в тестах без ожидания  # sweeper вызывается в тестах вручную
    TELEGRAM_MAX_RETRIES="2",
    BOOTSTRAP_ADMIN_EMAIL="",
    BOOTSTRAP_ADMIN_PASSWORD="",
)
sys.path.insert(0, str(BASE))


# --------------------------------------------------------------------------- #
# Перехват логов: токен бота и секрет webhook не должны попасть ни в один лог
# --------------------------------------------------------------------------- #
class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


capture = Capture()
logging.getLogger().addHandler(capture)
logging.getLogger().setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(
    logging.INFO
)  # включаем ДО импорта приложения: оно обязано заглушить сам

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ai.llm_client import LLMResult, LLMUnavailable  # noqa: E402
from ai.pipeline import (  # noqa: E402
    REPLY_RECEIVED,
    REPLY_SPAM,
    AIPipeline,
)
from config import settings  # noqa: E402
from database import SessionLocal  # noqa: E402
from integrations import telegram  # noqa: E402
from integrations.telegram import IncomingMessage, TelegramClient  # noqa: E402
from main import app  # noqa: E402
from models import Integration  # noqa: E402
from services import (  # noqa: E402
    ai_service,
    integration_service,
    message_service,
    rate_limit_service,
    secret_store,  # noqa: E402
)

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
TOKEN_BAD = "999999:" + "X" * 35


def tg_ok(result: object) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def tg_err(code: int, description: str, **params) -> httpx.Response:
    body: dict = {"ok": False, "error_code": code, "description": description}
    if params:
        body["parameters"] = params
    return httpx.Response(code, json=body)


class FakeTelegram:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []  # (token, method, json)
        self.send_plan: list = []  # очередь ответов для sendMessage (Response)
        self.webhook_error: httpx.Response | None = None
        self.sleeps: list[float] = []
        self._message_id = 5000

    def handler(self, request: httpx.Request) -> httpx.Response:
        match = re.match(r"^/bot([^/]+)/(\w+)$", request.url.path)
        assert match, request.url.path
        token, method = match.group(1), match.group(2)
        body = json.loads(request.content) if request.content else {}
        self.calls.append((token, method, body))
        if token == TOKEN_BAD:
            return tg_err(401, "Unauthorized")
        if method == "getMe":
            bot_id = int(token.split(":")[0])
            return tg_ok({"id": bot_id, "is_bot": True, "username": f"bot{bot_id}"})
        if method == "setWebhook":
            return self.webhook_error or tg_ok(True)
        if method == "deleteWebhook":
            return tg_ok(True)
        if method == "sendMessage":
            if self.send_plan:
                return self.send_plan.pop(0)
            self._message_id += 1
            return tg_ok({"message_id": self._message_id})
        return tg_err(404, "Not Found")

    def sent(self, token: str | None = None) -> list[dict]:
        return [
            body
            for tok, method, body in self.calls
            if method == "sendMessage" and (token is None or tok == token)
        ]

    def webhook_secret(self, token: str) -> str:
        secrets_seen = [
            body["secret_token"]
            for tok, method, body in self.calls
            if method == "setWebhook" and tok == token
        ]
        return secrets_seen[-1]


fake = FakeTelegram()
integration_service.build_telegram_client = lambda token: TelegramClient(
    token,
    base_url="https://api.telegram.test",
    transport=httpx.MockTransport(fake.handler),
    sleep=fake.sleeps.append,
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


def update(update_id: int, chat_id: int, text: str | None = None, **extra) -> dict:
    message: dict = {
        "message_id": update_id,
        "from": {
            "id": chat_id,
            "is_bot": False,
            "first_name": "Иван",
            "username": f"user{chat_id}",
        },
        "chat": {"id": chat_id, "type": "private"},
        "date": 1758300000,
    }
    if text is not None:
        message["text"] = text
    message.update(extra)
    return {"update_id": update_id, "message": message}


def system_logs(event_type: str | None = None, message_id: int | None = None) -> list[dict]:
    rows = db_rows("SELECT event_type, level, message, metadata FROM system_logs ORDER BY id")
    result = []
    for row in rows:
        meta = json.loads(row["metadata"]) if row["metadata"] else {}
        if event_type and row["event_type"] != event_type:
            continue
        if message_id is not None and meta.get("message_id") != message_id:
            continue
        result.append(
            {
                "event": row["event_type"],
                "level": row["level"],
                "message": row["message"],
                "meta": meta,
            }
        )
    return result


def last_message_id(chat_id: int, business_id: int) -> int:
    row = db_rows(
        "SELECT m.id FROM messages m JOIN conversations c ON c.id = m.conversation_id "
        "JOIN customers cu ON cu.id = c.customer_id "
        "WHERE cu.external_id = ? AND m.business_id = ? AND m.sender_type = 'CUSTOMER' "
        "ORDER BY m.id DESC LIMIT 1",
        str(chat_id),
        business_id,
    )
    return row[0]["id"]


class FailingLLM:
    offline = False

    def complete_json(self, messages, *, purpose, model=None) -> LLMResult:
        raise LLMUnavailable("LLM недоступен (тест)")


PASSWORD = "Str0ng-Pass-1"

with TestClient(app) as c:
    # ----------------------------------------------------------------------- #
    print("\n=== 0. Подготовка: компании, сотрудники, услуги ===")
    for email in ("owner_a@example.com", "owner_b@example.com", "manager@example.com"):
        c.post("/auth/register", json={"email": email, "password": PASSWORD})

    def login(email: str) -> dict:
        token = c.post("/auth/login", json={"email": email, "password": PASSWORD}).json()[
            "access_token"
        ]
        c.cookies.clear()  # логин ставит cookie; тесты проверяют Bearer и запросы без токена
        return {"Authorization": f"Bearer {token}"}

    ha, hb, hm = (
        login("owner_a@example.com"),
        login("owner_b@example.com"),
        login("manager@example.com"),
    )
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
        json={"email": "manager@example.com", "role": "MANAGER"},
    )
    check("компании и услуги созданы", isinstance(biz_a, int) and isinstance(biz_b, int))

    # ----------------------------------------------------------------------- #
    print("\n=== 1. Подключение бота (критерий 2, раздел 16) ===")
    url = f"/businesses/{biz_a}/integrations/telegram"
    check(
        "без токена авторизации → 401", c.post(url, json={"bot_token": TOKEN_A}).status_code == 401
    )
    check(
        "MANAGER не подключает интеграции → 403",
        c.post(url, headers=hm, json={"bot_token": TOKEN_A}).status_code == 403,
    )
    check(
        "чужая компания → 404",
        c.post(url, headers=hb, json={"bot_token": TOKEN_A}).status_code == 404,
    )
    check(
        "MANAGER не видит список интеграций → 403",
        c.get(f"/businesses/{biz_a}/integrations", headers=hm).status_code == 403,
    )

    r = c.post(url, headers=ha, json={"bot_token": "tokentokentoken1"})
    check("короткий токен → 422", r.status_code == 422)
    check("токен не эхом в ответе валидации", "tokentokentoken1" not in r.text, r.text[:80])
    r = c.post(url, headers=ha, json={"bot_token": "не-токен-но-достаточно-длинная-строка"})
    check("неверный формат токена → 422", r.status_code == 422)

    r = c.post(url, headers=ha, json={"bot_token": TOKEN_BAD})
    check("Telegram отклонил токен → 400", r.status_code == 400, r.text[:100])
    check(
        "токен не сохранён после отказа",
        db_rows("SELECT COUNT(*) AS n FROM integrations")[0]["n"] == 0,
    )

    with patch.object(settings, "public_base_url", None):
        r = c.post(url, headers=ha, json={"bot_token": TOKEN_A})
    check("без PUBLIC_BASE_URL → 503 с понятной причиной", r.status_code == 503, r.text[:100])

    fake.webhook_error = tg_err(400, "Bad Request: bad webhook: HTTPS url must be provided")
    r = c.post(url, headers=ha, json={"bot_token": TOKEN_A})
    check("setWebhook не удался → 502", r.status_code == 502, r.text[:100])
    st = db_rows("SELECT status, last_error FROM integrations")[0]
    check(
        "интеграция помечена ERROR с причиной", st["status"] == "ERROR" and bool(st["last_error"])
    )
    fake.webhook_error = None

    r = c.post(url, headers=ha, json={"bot_token": TOKEN_A})
    check("подключение бота → 201", r.status_code == 201, r.text[:120])
    body = r.json()
    check(
        "ответ без токена и секрета",
        TOKEN_A not in r.text
        and "secret" not in r.text.lower()
        and "credentials" not in r.text.lower(),
    )
    check(
        "ответ: бот и адрес webhook",
        body["bot_username"] == "bot111111"
        and body["webhook_url"] == "https://leadpilot.test/webhooks/telegram",
    )
    SECRET_A = fake.webhook_secret(TOKEN_A)
    hook = [b for t, m, b in fake.calls if m == "setWebhook" and t == TOKEN_A][-1]
    check(
        "setWebhook: https-адрес, secret_token, только message",
        hook["url"] == "https://leadpilot.test/webhooks/telegram"
        and len(SECRET_A) >= 32
        and hook["allowed_updates"] == ["message"],
    )
    row = db_rows("SELECT * FROM integrations WHERE business_id = ?", biz_a)[0]
    check(
        "токен в БД зашифрован (credentials_ref = enc:…)",
        row["credentials_ref"].startswith("enc:") and TOKEN_A not in row["credentials_ref"],
    )
    check(
        "секрет webhook в БД только хешем",
        row["webhook_secret_hash"] != SECRET_A
        and SECRET_A not in json.dumps(dict(row), default=str),
    )
    check("статус интеграции ACTIVE", row["status"] == "ACTIVE")
    listing = c.get(f"/businesses/{biz_a}/integrations", headers=ha)
    check(
        "GET integrations: без секретов",
        listing.status_code == 200
        and TOKEN_A not in listing.text
        and SECRET_A not in listing.text
        and len(listing.json()) == 1,
    )
    check("событие INTEGRATION_CONNECTED записано", len(system_logs("INTEGRATION_CONNECTED")) >= 1)

    r = c.post(
        f"/businesses/{biz_b}/integrations/telegram", headers=hb, json={"bot_token": TOKEN_A}
    )
    check("тот же бот у другой компании → 409", r.status_code == 409, r.text[:100])
    r = c.post(
        f"/businesses/{biz_b}/integrations/telegram", headers=hb, json={"bot_token": TOKEN_B}
    )
    check("вторая компания подключает свой бот", r.status_code == 201)
    SECRET_B = fake.webhook_secret(TOKEN_B)

    # ----------------------------------------------------------------------- #
    print("\n=== 2. Аутентификация webhook (разделы 11, 16) ===")
    hook_url = "/webhooks/telegram"
    good = update(1, 1001, "Привет")
    check("без secret_token → 401", c.post(hook_url, json=good).status_code == 401)
    check(
        "неверный secret_token → 401",
        c.post(
            hook_url, json=good, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}
        ).status_code
        == 401,
    )
    check(
        "секрет чужой компании не даёт доступ к данным: сообщений ещё нет",
        db_rows("SELECT COUNT(*) AS n FROM messages")[0]["n"] == 0,
    )
    check(
        "отклонённые запросы записаны как WEBHOOK_REJECTED",
        len(system_logs("WEBHOOK_REJECTED")) >= 2,
    )
    statuses = {
        c.post(
            hook_url, json=good, headers={"X-Telegram-Bot-Api-Secret-Token": f"guess{i}"}
        ).status_code
        for i in range(40)
    }
    check("перебор секрета ограничен → 429", 429 in statuses, str(sorted(statuses)))
    rate_limit_service.reset()
    H_A = {"X-Telegram-Bot-Api-Secret-Token": SECRET_A}
    H_B = {"X-Telegram-Bot-Api-Secret-Token": SECRET_B}

    # ----------------------------------------------------------------------- #
    print("\n=== 3. Сценарий A: типовой вопрос о цене (раздел 7, критерии 3, 4, 5, 6, 8) ===")
    r = c.post(hook_url, json=update(10, 1001, "Сколько стоит стрижка + борода?"), headers=H_A)
    check("webhook принят → 200", r.status_code == 200 and r.json() == {"ok": True}, r.text)
    mid = last_message_id(1001, biz_a)
    msg = db_rows("SELECT * FROM messages WHERE id = ?", mid)[0]
    check(
        "входящее сохранено (CUSTOMER, external_message_id)",
        msg["sender_type"] == "CUSTOMER"
        and msg["external_message_id"] == "10"
        and "стрижка" in msg["text"],
    )
    check("intent записан в сообщение", msg["intent"] == "PRICE", str(msg["intent"]))
    check(
        "сообщение обработано (DONE)",
        msg["processing_status"] == "DONE" and msg["processing_attempts"] == 1,
    )
    cust = db_rows("SELECT * FROM customers WHERE business_id = ? AND external_id = '1001'", biz_a)[
        0
    ]
    check(
        "клиент создан по chat_id",
        cust["name"] == "Иван" and cust["username"] == "user1001" and cust["channel"] == "TELEGRAM",
    )
    conv = db_rows("SELECT * FROM conversations WHERE customer_id = ?", cust["id"])[0]
    check("диалог: OPEN, приоритет WARM", conv["status"] == "OPEN" and conv["priority"] == "WARM")
    sent = fake.sent(TOKEN_A)
    check(
        "клиенту отправлен ровно один ответ",
        len(sent) == 1 and sent[0]["chat_id"] == "1001",
        str(len(sent)),
    )
    check(
        "ответ содержит цены из БД",
        "1500" in sent[0]["text"].replace("\u00a0", "").replace(" ", "")
        and "1000" in sent[0]["text"].replace("\u00a0", "").replace(" ", ""),
        sent[0]["text"],
    )
    out = db_rows(
        "SELECT * FROM messages WHERE conversation_id = ? AND sender_type = 'AI'", conv["id"]
    )
    check(
        "ответ AI сохранён и доставлен (SENT, external id)",
        len(out) == 1 and out[0]["delivery_status"] == "SENT" and out[0]["external_message_id"],
    )
    air = db_rows("SELECT * FROM ai_responses WHERE message_id = ?", mid)[0]
    check(
        "ai_responses: SENT, модель, версия промпта, время",
        air["status"] == "SENT"
        and air["model"]
        and air["prompt_version"]
        and air["latency_ms"] is not None
        and air["decision"] == "SEND",
    )
    check(
        "ai_responses.response_message_id ссылается на ответ",
        air["response_message_id"] == out[0]["id"],
    )
    chain = [e["event"] for e in system_logs(message_id=mid)]
    check(
        "в логах вся цепочка по message_id: приём → AI → отправка → итог",
        all(
            e in chain
            for e in ("WEBHOOK_RECEIVED", "AI_RESPONSE_READY", "MESSAGE_SENT", "MESSAGE_PROCESSED")
        ),
        str(chain),
    )
    proc = [e for e in system_logs("MESSAGE_PROCESSED", mid)][0]["meta"]
    check(
        "лог итога объясняет решение",
        proc["decision"] == "SEND"
        and proc["reply_sent_to_customer"] is True
        and proc["intent"] == "PRICE",
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 4. Идемпотентность webhook (раздел 18, CLAUDE.md инвариант 4) ===")
    before_msgs = db_rows("SELECT COUNT(*) AS n FROM messages")[0]["n"]
    before_sent = len(fake.sent(TOKEN_A))
    r = c.post(hook_url, json=update(10, 1001, "Сколько стоит стрижка + борода?"), headers=H_A)
    check(
        "повтор того же update → 200 duplicate",
        r.status_code == 200 and r.json().get("duplicate") is True,
        r.text,
    )
    check(
        "дубликат не создаёт сообщений",
        db_rows("SELECT COUNT(*) AS n FROM messages")[0]["n"] == before_msgs,
    )
    check("дубликат не отправляет ответ повторно", len(fake.sent(TOKEN_A)) == before_sent)
    check("дубликат записан в лог", len(system_logs("MESSAGE_DUPLICATE")) == 1)
    check(
        "клиент и диалог не задублированы",
        db_rows("SELECT COUNT(*) AS n FROM customers WHERE business_id = ?", biz_a)[0]["n"] == 1
        and db_rows("SELECT COUNT(*) AS n FROM conversations WHERE business_id = ?", biz_a)[0]["n"]
        == 1,
    )
    # Гонка: сообщение вставлено «между» проверкой и записью — упирается в UNIQUE.
    with SessionLocal() as db:
        integration = db.query(Integration).filter(Integration.business_id == biz_a).one()
        dup = message_service.receive_incoming(
            db,
            integration,
            IncomingMessage(
                "TELEGRAM", "1001", "10", "Сколько стоит стрижка + борода?", "Иван", "user1001"
            ),
        )
    check(
        "receive_incoming на дубликат: duplicate=True без исключения",
        dup.duplicate and dup.message_id == mid,
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 5. Сценарий B: запись → менеджер (критерии 5, 8; разделы 6.7, 7-Б) ===")
    sent_before = len(fake.sent(TOKEN_A))
    c.post(hook_url, json=update(20, 1002, "Хочу записаться на завтра"), headers=H_A)
    mid = last_message_id(1002, biz_a)
    conv = db_rows(
        "SELECT c.* FROM conversations c JOIN customers u ON u.id = c.customer_id "
        "WHERE u.external_id = '1002' AND c.business_id = ?",
        biz_a,
    )[0]
    check(
        "BOOKING → диалог требует внимания, HOT",
        conv["status"] == "NEEDS_ATTENTION"
        and conv["priority"] == "HOT"
        and conv["attention_reason"] == "HOT_LEAD_CONFIRMATION",
        str(dict(conv)),
    )
    sent = fake.sent(TOKEN_A)
    check(
        "клиенту ушла заявка: понятый день, вопрос об услуге и времени, без «вы записаны»",
        len(sent) == sent_before + 1
        and "Приняли заявку" in sent[-1]["text"]
        and "завтра" in sent[-1]["text"]
        and "подтвердит" in sent[-1]["text"]
        and "вы записаны" not in sent[-1]["text"],
        sent[-1]["text"],
    )
    air = db_rows("SELECT * FROM ai_responses WHERE message_id = ?", mid)[0]
    check(
        "ai_responses: ESCALATED + причина",
        air["status"] == "ESCALATED"
        and air["decision"] == "ESCALATE"
        and air["escalation_reason"] == "HOT_LEAD_CONFIRMATION",
    )
    check("лог AI_ESCALATED привязан к сообщению", len(system_logs("AI_ESCALATED", mid)) == 1)
    c.post(hook_url, json=update(21, 1002, "А можно записаться на 18:00?"), headers=H_A)
    # Решение 2026-09-27: ответ на каждое сообщение; повтор шаблона — коротко.
    check(
        "уточнение «на 18:00» дополняет заявку: день из прошлого сообщения + время",
        len(fake.sent(TOKEN_A)) == sent_before + 2
        and "завтра" in fake.sent(TOKEN_A)[-1]["text"]
        and "18:00" in fake.sent(TOKEN_A)[-1]["text"],
        fake.sent(TOKEN_A)[-1]["text"],
    )
    mid2 = last_message_id(1002, biz_a)
    check(
        "...но сообщение сохранено и обработано",
        db_rows("SELECT processing_status FROM messages WHERE id = ?", mid2)[0]["processing_status"]
        == "DONE",
    )

    print("\n=== 6. Спам, вложение, служебные апдейты ===")
    sent_before = len(fake.sent(TOKEN_A))
    c.post(
        hook_url,
        json=update(30, 1003, "Заработок в крипте без вложений, казино и ставки на спорт"),
        headers=H_A,
    )
    check(
        "спам: клиенту короткий нейтральный ответ (решение 2026-09-27)",
        len(fake.sent(TOKEN_A)) == sent_before + 1 and fake.sent(TOKEN_A)[-1]["text"] == REPLY_SPAM,
    )
    spam_conv = db_rows(
        "SELECT c.* FROM conversations c JOIN customers u ON u.id = c.customer_id "
        "WHERE u.external_id = '1003'"
    )[0]
    check(
        "спам: диалог у менеджера, причина SPAM_SUSPECTED, приоритет COLD",
        spam_conv["status"] == "NEEDS_ATTENTION"
        and spam_conv["attention_reason"] == "SPAM_SUSPECTED"
        and spam_conv["priority"] == "COLD",
    )
    air = db_rows("SELECT * FROM ai_responses WHERE message_id = ?", last_message_id(1003, biz_a))[
        0
    ]
    check("спам: ai_response связан с исходящим ответом", air["response_message_id"] is not None)

    c.post(
        hook_url,
        json=update(40, 1004, None, photo=[{"file_id": "x", "width": 1, "height": 1}]),
        headers=H_A,
    )
    att = db_rows("SELECT * FROM messages WHERE id = ?", last_message_id(1004, biz_a))[0]
    check(
        "вложение сохранено (не теряется)",
        att["content_type"] == "attachment" and "вложение" in att["text"],
    )
    att_conv = db_rows(
        "SELECT c.* FROM conversations c JOIN customers u ON u.id = c.customer_id "
        "WHERE u.external_id = '1004'"
    )[0]
    check(
        "вложение: менеджеру, клиенту безопасный ответ",
        att_conv["status"] == "NEEDS_ATTENTION"
        and att_conv["attention_reason"] == "AMBIGUOUS_REQUEST"
        and fake.sent(TOKEN_A)[-1]["chat_id"] == "1004",
    )

    msgs_before = db_rows("SELECT COUNT(*) AS n FROM messages")[0]["n"]
    ignored = [
        (
            "группа",
            {
                "update_id": 50,
                "message": {
                    "message_id": 1,
                    "chat": {"id": -5, "type": "group"},
                    "from": {"id": 1, "is_bot": False},
                    "text": "hi",
                },
            },
        ),
        (
            "правка сообщения",
            {
                "update_id": 51,
                "edited_message": {
                    "message_id": 1,
                    "chat": {"id": 7, "type": "private"},
                    "text": "x",
                },
            },
        ),
        (
            "бот-отправитель",
            {
                "update_id": 52,
                "message": {
                    "message_id": 1,
                    "chat": {"id": 8, "type": "private"},
                    "from": {"id": 8, "is_bot": True},
                    "text": "x",
                },
            },
        ),
        ("my_chat_member", {"update_id": 53, "my_chat_member": {"chat": {"id": 9}}}),
        ("пустой update", {}),
    ]
    codes = [c.post(hook_url, json=body, headers=H_A).status_code for _, body in ignored]
    check(
        "служебные апдейты → 200 (Telegram не повторяет)", codes == [200] * len(ignored), str(codes)
    )
    check(
        "служебные апдейты ничего не сохраняют",
        db_rows("SELECT COUNT(*) AS n FROM messages")[0]["n"] == msgs_before,
    )
    check("игнорирование записано в лог", len(system_logs("WEBHOOK_IGNORED")) >= len(ignored))

    # ----------------------------------------------------------------------- #
    print("\n=== 7. Сбои Telegram при отправке (раздел 18, критерий 15) ===")
    # 7.1  403: клиент заблокировал бота — не ретраим, но и не теряем.
    fake.send_plan = [tg_err(403, "Forbidden: bot was blocked by the user")]
    calls_before = len(fake.sent(TOKEN_A))
    c.post(hook_url, json=update(60, 1005, "Сколько стоит стрижка?"), headers=H_A)
    check("403: одна попытка без ретраев", len(fake.sent(TOKEN_A)) == calls_before + 1)
    mid = last_message_id(1005, biz_a)
    conv = db_rows(
        "SELECT c.*, u.channel_blocked FROM conversations c JOIN customers u ON u.id = c.customer_id "
        "WHERE u.external_id = '1005'"
    )[0]
    check(
        "403: клиент помечен, диалог у менеджера (DELIVERY_FAILED)",
        conv["channel_blocked"] == 1
        and conv["status"] == "NEEDS_ATTENTION"
        and conv["attention_reason"] == "DELIVERY_FAILED",
    )
    out = db_rows(
        "SELECT * FROM messages WHERE conversation_id = ? AND sender_type = 'AI'", conv["id"]
    )[0]
    check(
        "403: ответ сохранён со статусом FAILED и причиной",
        out["delivery_status"] == "FAILED" and "blocked" in out["delivery_error"],
    )
    check(
        "403: ai_response FAILED",
        db_rows("SELECT status FROM ai_responses WHERE message_id = ?", mid)[0]["status"]
        == "FAILED",
    )
    check(
        "403: MESSAGE_SEND_FAILED (ERROR) в логе",
        any(e["level"] == "ERROR" for e in system_logs("MESSAGE_SEND_FAILED", mid)),
    )
    check(
        "403: входящее обработано (не «зависло»)",
        db_rows("SELECT processing_status FROM messages WHERE id = ?", mid)[0]["processing_status"]
        == "DONE",
    )

    # 7.2  429 с retry_after → пауза и успешный повтор.
    fake.sleeps.clear()
    fake.send_plan = [tg_err(429, "Too Many Requests: retry after 2", retry_after=2)]
    calls_before = len(fake.sent(TOKEN_A))
    c.post(hook_url, json=update(61, 1006, "Сколько стоит борода?"), headers=H_A)
    check(
        "429: повтор после retry_after",
        2.0 in fake.sleeps and len(fake.sent(TOKEN_A)) == calls_before + 2,
        str(fake.sleeps),
    )
    out = db_rows(
        "SELECT delivery_status FROM messages WHERE sender_type = 'AI' AND external_message_id IS NOT NULL "
        "ORDER BY id DESC LIMIT 1"
    )[0]
    check("429: в итоге доставлено (SENT)", out["delivery_status"] == "SENT")

    # 7.3  5xx: три попытки клиента, ответ остаётся PENDING, sweeper доставляет позже.
    fake.send_plan = [tg_err(502, "Bad Gateway")] * 3
    calls_before = len(fake.sent(TOKEN_A))
    c.post(hook_url, json=update(62, 1007, "Сколько стоит стрижка?"), headers=H_A)
    mid = last_message_id(1007, biz_a)
    check(
        "5xx: клиент Telegram повторил запрос (backoff)",
        len(fake.sent(TOKEN_A)) == calls_before + 3,
    )
    conv_id = db_rows("SELECT conversation_id AS c FROM messages WHERE id = ?", mid)[0]["c"]
    out = db_rows(
        "SELECT * FROM messages WHERE conversation_id = ? AND sender_type = 'AI'", conv_id
    )[0]
    check(
        "5xx: временная ошибка → ответ ждёт повтора (PENDING, не FAILED)",
        out["delivery_status"] == "PENDING" and out["delivery_attempts"] == 1,
    )
    check(
        "5xx: входящее не потеряно и обработано",
        db_rows("SELECT processing_status FROM messages WHERE id = ?", mid)[0]["processing_status"]
        == "DONE",
    )
    message_service._GRACE_SECONDS = 0
    handled = message_service.reprocess_pending()
    out = db_rows("SELECT * FROM messages WHERE id = ?", out["id"])[0]
    check(
        "sweeper: отложенный ответ доставлен",
        handled >= 1 and out["delivery_status"] == "SENT" and out["delivery_attempts"] == 2,
        str(dict(out)),
    )
    check(
        "sweeper: ai_response стал SENT",
        db_rows("SELECT status FROM ai_responses WHERE message_id = ?", mid)[0]["status"] == "SENT",
    )
    check("sweeper: второй запуск ничего не делает", message_service.reprocess_pending() == 0)

    # 7.4  Длинный ответ режется на части ≤ 4096 символов.
    chunks = telegram.split_text(("слово " * 900).strip())
    check(
        "split_text: части ≤ 4096 и без потерь",
        len(chunks) == 2
        and all(len(x) <= 4096 for x in chunks)
        and " ".join(" ".join(chunks).split()) == ("слово " * 900).strip(),
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 8. Сценарий C: сбой LLM и сбой обработки (критерий 15) ===")
    ai_service.reset_pipeline(AIPipeline(FailingLLM()))
    sent_before = len(fake.sent(TOKEN_A))
    c.post(hook_url, json=update(70, 1008, "Что входит в стрижку?"), headers=H_A)
    ai_service.reset_pipeline(None)
    mid = last_message_id(1008, biz_a)
    check(
        "LLM недоступен: клиенту безопасный ответ «сообщение получили»",
        len(fake.sent(TOKEN_A)) == sent_before + 1
        and fake.sent(TOKEN_A)[-1]["text"] == REPLY_RECEIVED,
    )
    ai_err = system_logs("AI_ERROR", mid)
    check(
        "LLM недоступен: событие AI_ERROR (ERROR) в логе",
        len(ai_err) == 1 and ai_err[0]["level"] == "ERROR",
    )
    conv = db_rows(
        "SELECT c.* FROM conversations c JOIN customers u ON u.id = c.customer_id "
        "WHERE u.external_id = '1008'"
    )[0]
    check(
        "LLM недоступен: диалог у менеджера (EXTERNAL_API_ERROR)",
        conv["status"] == "NEEDS_ATTENTION" and conv["attention_reason"] == "EXTERNAL_API_ERROR",
    )

    # 8.2  Падение обработки (например, БД): сообщение сохранено, потом обработано повторно.
    sent_before = len(fake.sent(TOKEN_A))
    with patch.object(ai_service, "process_message", side_effect=RuntimeError("db down")):
        r = c.post(hook_url, json=update(71, 1009, "Сколько стоит борода?"), headers=H_A)
    mid = last_message_id(1009, biz_a)
    row = db_rows("SELECT * FROM messages WHERE id = ?", mid)[0]
    check("сбой обработки: webhook всё равно 200 (сообщение сохранено)", r.status_code == 200)
    check(
        "сбой обработки: FAILED, попытка 1, причина записана",
        row["processing_status"] == "FAILED"
        and row["processing_attempts"] == 1
        and "db down" in row["processing_error"],
    )
    check(
        "сбой обработки: MESSAGE_PROCESSING_FAILED в логе (ERROR)",
        any(e["level"] == "ERROR" for e in system_logs("MESSAGE_PROCESSING_FAILED", mid)),
    )
    check("сбой обработки: клиенту пока ничего", len(fake.sent(TOKEN_A)) == sent_before)
    handled = message_service.reprocess_pending()
    row = db_rows("SELECT * FROM messages WHERE id = ?", mid)[0]
    check(
        "повторная обработка: DONE, ответ отправлен один раз",
        handled >= 1
        and row["processing_status"] == "DONE"
        and row["processing_attempts"] == 2
        and len(fake.sent(TOKEN_A)) == sent_before + 1,
    )
    check(
        "повторная обработка: один ai_response",
        db_rows("SELECT COUNT(*) AS n FROM ai_responses WHERE message_id = ?", mid)[0]["n"] == 1,
    )

    # 8.3  Попытки исчерпаны → диалог у менеджера, дальше не крутим.
    with patch.object(ai_service, "process_message", side_effect=RuntimeError("db down")):
        c.post(hook_url, json=update(72, 1010, "Сколько стоит борода?"), headers=H_A)
        message_service.reprocess_pending()
        message_service.reprocess_pending()
        idle = message_service.reprocess_pending()
    mid = last_message_id(1010, biz_a)
    row = db_rows("SELECT * FROM messages WHERE id = ?", mid)[0]
    conv = db_rows("SELECT * FROM conversations WHERE id = ?", row["conversation_id"])[0]
    check(
        "попытки исчерпаны: FAILED и attempts=MESSAGE_MAX_ATTEMPTS",
        row["processing_status"] == "FAILED"
        and row["processing_attempts"] == settings.message_max_attempts,
    )
    check(
        "попытки исчерпаны: диалог у менеджера (PROCESSING_FAILED), сообщение видно",
        conv["status"] == "NEEDS_ATTENTION" and conv["attention_reason"] == "PROCESSING_FAILED",
    )
    check("попытки исчерпаны: sweeper больше не берёт", idle == 0)
    check(
        "попытки исчерпаны: клиент всё равно получил «сообщение получили» (решение 2026-09-27)",
        [b["text"] for b in fake.sent(TOKEN_A) if str(b["chat_id"]) == "1010"][-1:]
        == [REPLY_RECEIVED],
    )

    # 8.4  Атомарный захват: двух обработчиков одного сообщения не бывает.
    with SessionLocal() as db:
        integration = db.query(Integration).filter(Integration.business_id == biz_a).one()
        fresh = message_service.receive_incoming(
            db, integration, IncomingMessage("TELEGRAM", "1011", "73", "Привет!", "Пётр", None)
        )
    with SessionLocal() as db1, SessionLocal() as db2:
        first = message_service._claim(db1, fresh.message_id)
        second = message_service._claim(db2, fresh.message_id)
    check("_claim: первый захватывает, второй — нет", first is True and second is False)
    message_service._GRACE_SECONDS = 30

    # ----------------------------------------------------------------------- #
    print("\n=== 8.5 Серия сообщений — один ответ (решение 2026-09-27) ===")

    def chat_texts(chat: str) -> list[str]:
        return [b["text"] for b in fake.sent(TOKEN_A) if str(b["chat_id"]) == chat]

    with SessionLocal() as db:
        integration = db.query(Integration).filter(Integration.business_id == biz_a).one()
        burst = [
            message_service.receive_incoming(
                db, integration, IncomingMessage("TELEGRAM", "1013", str(90 + i), t, "Ира", None)
            ).message_id
            for i, t in enumerate(["Здравствуйте", "Сколько стоит", "стрижка?"])
        ]
    # Паузу webhook имитируем обработкой после прихода всей серии.
    for mid_b in burst:
        message_service.process_incoming_message(mid_b)
    rows_b = db_rows(
        "SELECT id, processing_status, processing_error FROM messages "
        "WHERE id BETWEEN ? AND ? AND sender_type = 'CUSTOMER' ORDER BY id",
        burst[0],
        burst[-1],
    )
    check(
        "серия: ранние сообщения помечены «объединено», все DONE",
        [r["processing_status"] for r in rows_b] == ["DONE"] * 3
        and [r["processing_error"] for r in rows_b[:2]] == [message_service.MERGED_NOTE] * 2,
    )
    replies_b = chat_texts("1013")
    check("серия: клиент получил ровно один ответ", len(replies_b) == 1, str(replies_b))
    check(
        "серия: ответ учёл весь текст серии (цена стрижки из БД)",
        bool(replies_b) and "1500" in replies_b[0].replace(" ", "").replace(" ", ""),
        str(replies_b),
    )
    check("серия: событие MESSAGE_MERGED в логе", len(system_logs("MESSAGE_MERGED")) >= 2)

    # ----------------------------------------------------------------------- #
    print("\n=== 8.6 Контроль ответа: сообщение не остаётся без ответа ===")
    with SessionLocal() as db:
        integration = db.query(Integration).filter(Integration.business_id == biz_a).one()
        orphan = message_service.receive_incoming(
            db, integration, IncomingMessage("TELEGRAM", "1014", "95", "Есть кто?", "Лев", None)
        )
    # Сообщение «обработано», но ответ так и не ушёл (например, сбой между шагами).
    db_exec(
        "UPDATE messages SET processing_status = 'DONE', created_at = datetime('now', '-5 minutes') "
        "WHERE id = ?",
        orphan.message_id,
    )
    check("контроль: свежее сообщение не трогает (ждёт обработку)", chat_texts("1014") == [])
    fixed = message_service.ensure_replies()
    check(
        "контроль: сообщение без ответа 5 минут → шаблон «сообщение получили»",
        fixed == 1 and chat_texts("1014") == [REPLY_RECEIVED],
        f"{fixed} {chat_texts('1014')}",
    )
    check("контроль: событие REPLY_WATCHDOG в логе", len(system_logs("REPLY_WATCHDOG")) == 1)
    check("контроль: повторно не срабатывает", message_service.ensure_replies() == 0)

    with SessionLocal() as db:
        integration = db.query(Integration).filter(Integration.business_id == biz_a).one()
        quiet = message_service.receive_incoming(
            db, integration, IncomingMessage("TELEGRAM", "1015", "96", "Жду", "Ян", None)
        )
    db_exec(
        "UPDATE messages SET processing_status = 'DONE', created_at = datetime('now', '-5 minutes') "
        "WHERE id = ?",
        quiet.message_id,
    )
    db_exec("UPDATE conversations SET handled_by_manager = 1 WHERE id = ?", quiet.conversation_id)
    check(
        "контроль: диалог ведёт менеджер — AI молчит",
        message_service.ensure_replies() == 0 and chat_texts("1015") == [],
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 9. Приостановленная компания (раздел 15) ===")
    db_exec("UPDATE businesses SET status = 'SUSPENDED' WHERE id = ?", biz_a)
    sent_before = len(fake.sent(TOKEN_A))
    r = c.post(hook_url, json=update(80, 1012, "Сколько стоит стрижка?"), headers=H_A)
    db_exec("UPDATE businesses SET status = 'TRIAL' WHERE id = ?", biz_a)
    mid = last_message_id(1012, biz_a)
    check(
        "приостановлена: сообщение принято и сохранено",
        r.status_code == 200
        and db_rows("SELECT processing_status FROM messages WHERE id = ?", mid)[0][
            "processing_status"
        ]
        == "DONE",
    )
    check(
        # Проверка сайта 2026-10-02: сотрудники приостановленной компании ответить
        # не могут — не обещаем ответ в чате, а просим связаться по телефону.
        "приостановлена: AI не запускается, клиенту — «не можем ответить в чате», без обещания",
        len(fake.sent(TOKEN_A)) == sent_before + 1
        and "не можем ответить в этом чате" in fake.sent(TOKEN_A)[-1]["text"]
        and "администратор" not in fake.sent(TOKEN_A)[-1]["text"].lower()
        and "сотрудник ответит" not in fake.sent(TOKEN_A)[-1]["text"].lower()
        and db_rows("SELECT COUNT(*) AS n FROM ai_responses WHERE message_id = ?", mid)[0]["n"]
        == 0,
    )
    check("приостановлена: MESSAGE_SKIPPED в логе", len(system_logs("MESSAGE_SKIPPED", mid)) == 1)

    # ----------------------------------------------------------------------- #
    print("\n=== 10. Мультитенантность (критерий 13, раздел 16) ===")
    # Тот же chat_id пишет в бота второй компании → отдельные клиент и диалог.
    c.post(hook_url, json=update(90, 1001, "Сколько стоит маникюр?"), headers=H_B)
    cust_b = db_rows(
        "SELECT * FROM customers WHERE business_id = ? AND external_id = '1001'", biz_b
    )
    check(
        "тот же chat_id в другой компании → отдельный клиент",
        len(cust_b) == 1
        and db_rows("SELECT COUNT(*) AS n FROM customers WHERE external_id = '1001'")[0]["n"] == 2,
    )
    sent_b = fake.sent(TOKEN_B)
    check(
        "ответ ушёл через бота своей компании с её прайсом",
        len(sent_b) == 1
        and "2000" in sent_b[0]["text"].replace("\u00a0", "").replace(" ", "")
        and "1500" not in sent_b[0]["text"].replace("\u00a0", "").replace(" ", ""),
        str(sent_b),
    )
    check(
        "сообщение компании B не попало в компанию A",
        db_rows(
            "SELECT COUNT(*) AS n FROM messages WHERE business_id = ? AND text LIKE '%маникюр%'",
            biz_a,
        )[0]["n"]
        == 0,
    )

    lst_a = c.get(f"/businesses/{biz_a}/conversations", headers=ha)
    lst_b = c.get(f"/businesses/{biz_b}/conversations", headers=hb)
    check(
        "владелец A видит диалоги своей компании",
        lst_a.status_code == 200 and len(lst_a.json()) >= 8,
    )
    check(
        "владелец B видит только свой диалог", lst_b.status_code == 200 and len(lst_b.json()) == 1
    )
    check(
        "чужой список диалогов → 404",
        c.get(f"/businesses/{biz_a}/conversations", headers=hb).status_code == 404,
    )
    conv_a_id = lst_a.json()[0]["conversation"]["id"]
    conv_b_id = lst_b.json()[0]["conversation"]["id"]
    check(
        "чужой диалог по id → 404",
        c.get(f"/conversations/{conv_a_id}", headers=hb).status_code == 404,
    )
    check(
        "несуществующий и чужой диалог неразличимы",
        c.get("/conversations/99999", headers=hb).json()
        == c.get(f"/conversations/{conv_a_id}", headers=hb).json(),
    )
    check(
        "без токена → 401",
        c.get(f"/conversations/{conv_a_id}").status_code == 401
        and c.get(f"/businesses/{biz_a}/conversations").status_code == 401,
    )
    check(
        "MANAGER своей компании видит диалоги",
        c.get(f"/businesses/{biz_a}/conversations", headers=hm).status_code == 200
        and c.get(f"/conversations/{conv_a_id}", headers=hm).status_code == 200,
    )
    check(
        "владелец B видит свой диалог",
        c.get(f"/conversations/{conv_b_id}", headers=hb).status_code == 200,
    )

    hot = c.get(f"/businesses/{biz_a}/conversations", headers=ha, params={"priority": "HOT"}).json()
    check(
        "фильтр priority=HOT",
        len(hot) >= 1 and all(i["conversation"]["priority"] == "HOT" for i in hot),
    )
    attn = c.get(
        f"/businesses/{biz_a}/conversations", headers=ha, params={"status": "NEEDS_ATTENTION"}
    ).json()
    check(
        "фильтр status=NEEDS_ATTENTION",
        len(attn) >= 3 and all(i["conversation"]["status"] == "NEEDS_ATTENTION" for i in attn),
    )
    old = c.get(
        f"/businesses/{biz_a}/conversations", headers=ha, params={"date_to": "2000-01-01T00:00:00"}
    ).json()
    new = c.get(
        f"/businesses/{biz_a}/conversations",
        headers=ha,
        params={"date_from": "2000-01-01T00:00:00"},
    ).json()
    check("фильтр по периоду", old == [] and len(new) == len(lst_a.json()))
    check(
        "невалидный статус → 422",
        c.get(
            f"/businesses/{biz_a}/conversations", headers=ha, params={"status": "BOGUS"}
        ).status_code
        == 422,
    )
    check(
        "пагинация limit",
        len(c.get(f"/businesses/{biz_a}/conversations", headers=ha, params={"limit": 2}).json())
        == 2,
    )

    first_conv = next(i for i in lst_a.json() if i["customer"]["username"] == "user1001")
    detail = c.get(f"/conversations/{first_conv['conversation']['id']}", headers=ha).json()
    check(
        "карточка диалога: сообщения по порядку (клиент → AI)",
        [m["sender_type"] for m in detail["messages"]][:2] == ["CUSTOMER", "AI"],
    )
    check(
        "карточка диалога: решение AI с причиной и моделью",
        detail["ai_decisions"]
        and detail["ai_decisions"][0]["status"] == "SENT"
        and detail["ai_decisions"][0]["details"]["intent"] == "PRICE",
    )
    check(
        "в ответах API нет секретов",
        "credentials" not in json.dumps(detail) and TOKEN_A not in json.dumps(detail),
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 11. Отключение бота ===")
    r = c.delete(url, headers=hm)
    check("MANAGER не отключает → 403", r.status_code == 403)
    r = c.delete(url, headers=ha)
    check("отключение → 204", r.status_code == 204)
    check(
        "Telegram: webhook снят",
        any(m == "deleteWebhook" and t == TOKEN_A for t, m, _ in fake.calls),
    )
    row = db_rows("SELECT * FROM integrations WHERE business_id = ?", biz_a)[0]
    check(
        "интеграция DISABLED, секреты стёрты",
        row["status"] == "DISABLED"
        and row["credentials_ref"] == ""
        and row["webhook_secret_hash"] is None,
    )
    check(
        "старый секрет больше не работает → 401",
        c.post(hook_url, json=update(100, 1001, "Привет"), headers=H_A).status_code == 401,
    )
    check(
        "секрет другой компании работает",
        c.post(hook_url, json=update(101, 1001, "Привет"), headers=H_B).status_code == 200,
    )
    r = c.delete(url, headers=ha)
    check("повторное отключение не падает (204/404)", r.status_code in (204, 404))
    check("INTEGRATION_DISCONNECTED в логе", len(system_logs("INTEGRATION_DISCONNECTED")) >= 1)

# --------------------------------------------------------------------------- #
print("\n=== 11a. Фоновый цикл повторной обработки в lifespan приложения (раздел 18) ===")
# Сообщение сохранено, но обработка не запускалась (процесс упал после ответа Telegram).
with SessionLocal() as db:
    integration_b = db.query(Integration).filter(Integration.business_id == biz_b).one()
    stuck = message_service.receive_incoming(
        db,
        integration_b,
        IncomingMessage("TELEGRAM", "2001", "700", "Сколько стоит маникюр?", "Анна", None),
    )
stuck_row = db_rows("SELECT processing_status FROM messages WHERE id = ?", stuck.message_id)[0]
check(
    "до запуска цикла сообщение ждёт обработки (PENDING)",
    stuck_row["processing_status"] == "PENDING",
)
sent_before = len(fake.sent(TOKEN_B))
message_service._GRACE_SECONDS = 0
with patch.object(settings, "reprocess_interval_seconds", 1), TestClient(app):
    for _ in range(60):
        row = db_rows("SELECT processing_status FROM messages WHERE id = ?", stuck.message_id)[0]
        if row["processing_status"] == "DONE":
            break
        time.sleep(0.1)
check("цикл lifespan сам обработал застрявшее сообщение", row["processing_status"] == "DONE")
check("клиенту отправлен ровно один ответ", len(fake.sent(TOKEN_B)) == sent_before + 1)
check("выключение приложения не зависло на фоновой задаче", True)
message_service._GRACE_SECONDS = 30

print("\n=== 12. Секреты не попадают в логи (раздел 16) ===")
all_logs = "\n".join(capture.lines)
db_logs = json.dumps(system_logs(), ensure_ascii=False)
check(
    "токен бота A не в логах приложения",
    TOKEN_A not in all_logs and TOKEN_A.split(":")[1] not in all_logs,
)
check(
    "токен бота B не в логах приложения",
    TOKEN_B not in all_logs and TOKEN_B.split(":")[1] not in all_logs,
)
check(
    "секреты webhook не в логах приложения", SECRET_A not in all_logs and SECRET_B not in all_logs
)
check(
    "токены и секреты не в system_logs",
    TOKEN_A not in db_logs
    and TOKEN_B not in db_logs
    and SECRET_A not in db_logs
    and SECRET_B not in db_logs,
)
check(
    "httpx не печатает URL запросов (уровень WARNING)",
    logging.getLogger("httpx").level >= logging.WARNING
    and not any("/bot" in line for line in capture.lines),
)
check("repr клиента без токена", TOKEN_A not in repr(TelegramClient(TOKEN_A)))
whole_db = b"".join(p.read_bytes() for p in (DB,))
check(
    "в файле БД нет открытого токена",
    TOKEN_A.encode() not in whole_db and TOKEN_B.encode() not in whole_db,
)

# --------------------------------------------------------------------------- #
print("\n=== 13. Разбор Update и хранилище секретов ===")
msg, why = telegram.parse_update(update(1, 5, "Привет"))
check(
    "parse_update: текст",
    msg is not None
    and msg.text == "Привет"
    and msg.external_chat_id == "5"
    and msg.external_message_id == "1"
    and msg.content_type == "text"
    and msg.update_id == 1,
)
msg, why = telegram.parse_update(update(2, 5, None, voice={"file_id": "v"}))
check(
    "parse_update: вложение",
    msg is not None and msg.content_type == "attachment" and "voice" in msg.text,
)
msg, why = telegram.parse_update(update(3, 5, None, photo=[{}], caption="Хочу такую стрижку"))
check(
    "parse_update: подпись к фото = текст",
    msg is not None and msg.text == "Хочу такую стрижку" and msg.content_type == "attachment",
)
check(
    "parse_update: мусор игнорируется",
    telegram.parse_update({"message": "строка"})[1] == "unsupported_update_type"
    and telegram.parse_update({"message": {"chat": {"id": 1, "type": "private"}}})[1]
    == "no_message_id"
    and telegram.parse_update(
        {"message": {"message_id": 1, "chat": {"id": 1, "type": "private"}, "sticker_x": 1}}
    )[1]
    == "unsupported_message",
)

ref = secret_store.encrypt_secret("s3cret-token")
check(
    "secret_store: round-trip",
    ref.startswith("enc:")
    and "s3cret-token" not in ref
    and secret_store.resolve_secret(ref) == "s3cret-token",
)
os.environ["LP_TEST_TOKEN"] = "from-env"
check("secret_store: env-ссылка", secret_store.resolve_secret("env:LP_TEST_TOKEN") == "from-env")
try:
    secret_store.resolve_secret("env:LP_MISSING_VAR")
    check("secret_store: отсутствующая переменная → ошибка", False)
except secret_store.SecretStoreError:
    check("secret_store: отсутствующая переменная → ошибка", True)
with patch.object(settings, "secrets_encryption_key", "another-key-that-is-long-enough-32chars"):
    try:
        secret_store.resolve_secret(ref)
        check("secret_store: чужой ключ не расшифровывает", False)
    except secret_store.SecretStoreError as exc:
        check("secret_store: чужой ключ не расшифровывает", "s3cret-token" not in str(exc))

print("\n" + "=" * 70)
print(f"ИТОГО: пройдено {len(PASSED)}, провалено {len(FAILED)}")
if FAILED:
    print("Провалены:")
    for name in FAILED:
        print("  -", name)
    sys.exit(1)
