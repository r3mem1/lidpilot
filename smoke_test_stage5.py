"""
Проверочный скрипт этапа 5 — кабинет бизнеса (НЕ часть приложения, можно удалить).

Покрывает разделы 5, 9, 12, 13, 14, 16 ТЗ и критерии приёмки 9, 11, 13: страницы
кабинета и права ролей, защиту от XSS и CSRF, заголовки безопасности и CSP, настройки
AI компании, сотрудников и приглашения, аналитику. Браузер не нужен: страницы
проверяются через TestClient; настоящий браузер использовался отдельно (Playwright).
Telegram подменяется httpx.MockTransport, AI работает офлайн (AI_PROVIDER=stub).

Запуск:  python smoke_test_stage5.py
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sqlite3
import sys

BASE = pathlib.Path(__file__).resolve().parent
DB = BASE / "test_smoke_stage5.db"
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
    AUTH_RATE_LIMIT_ATTEMPTS="1000",  # тест делает десятки входов подряд
    BOOTSTRAP_ADMIN_EMAIL="admin@example.com",
    BOOTSTRAP_ADMIN_PASSWORD="Adm1n-Pass-123!",
)
sys.path.insert(0, str(BASE))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ai.context import BusinessKnowledge  # noqa: E402
from ai.prompts import build_responder_messages  # noqa: E402
from config import settings  # noqa: E402
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

    def sent(self, token: str) -> list[dict]:
        return [b for t, m, b in self.calls if m == "sendMessage" and t == token]

    def secret(self, token: str) -> str:
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


def events(event_type: str) -> list[dict]:
    rows = db_rows("SELECT metadata FROM system_logs WHERE event_type = ? ORDER BY id", event_type)
    return [json.loads(r["metadata"] or "{}") for r in rows]


PWD = "Str0ng-Pass-1"
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


with TestClient(app) as c:
    # ----------------------------------------------------------------------- #
    print("\n=== 0. Подготовка ===")
    people = ["owner_a", "owner_a2", "manager1", "owner_b", "outsider"]
    for name in people:
        c.post("/auth/register", json={"email": f"{name}@example.com", "password": PWD})

    def bearer(email: str, password: str | None = None) -> dict:
        token = c.post("/auth/login", json={"email": email, "password": password or PWD}).json()[
            "access_token"
        ]
        c.cookies.clear()
        return {"Authorization": f"Bearer {token}"}

    H = {name: bearer(f"{name}@example.com") for name in people}
    hadmin = bearer("admin@example.com", "Adm1n-Pass-123!")
    uid = {name: c.get("/me", headers=H[name]).json()["user"]["id"] for name in people}
    biz_a = c.post(
        "/businesses",
        headers=H["owner_a"],
        json={
            "name": "Барбершоп «Бритва»",
            "phone": "+7 999 000-00-00",
            "working_hours": "пн-сб 10:00-21:00",
        },
    ).json()["id"]
    biz_b = c.post("/businesses", headers=H["owner_b"], json={"name": "Салон «Лилия»"}).json()["id"]
    for n, pr in (("Стрижка", "1500.00"), ("Борода", "1000.00")):
        c.post(f"/businesses/{biz_a}/services", headers=H["owner_a"], json={"name": n, "price": pr})
    c.post(
        f"/businesses/{biz_b}/services",
        headers=H["owner_b"],
        json={"name": "Маникюр", "price": "2000.00"},
    )
    c.post(
        f"/businesses/{biz_a}/members",
        headers=H["owner_a"],
        json={"email": "owner_a2@example.com", "role": "OWNER"},
    )
    c.post(
        f"/businesses/{biz_a}/members",
        headers=H["owner_a"],
        json={"email": "manager1@example.com", "role": "MANAGER"},
    )
    c.post(
        f"/businesses/{biz_a}/integrations/telegram",
        headers=H["owner_a"],
        json={"bot_token": TOKEN_A},
    )
    c.post(
        f"/businesses/{biz_b}/integrations/telegram",
        headers=H["owner_b"],
        json={"bot_token": TOKEN_B},
    )
    HOOK_A = {"X-Telegram-Bot-Api-Secret-Token": fake.secret(TOKEN_A)}
    HOOK_B = {"X-Telegram-Bot-Api-Secret-Token": fake.secret(TOKEN_B)}

    def say(chat: int, text: str, hook: dict | None = None) -> None:
        r = c.post("/webhooks/telegram", json=upd(chat, text), headers=hook or HOOK_A)
        assert r.status_code == 200, r.text

    say(3001, "Сколько стоит стрижка?")
    say(3002, "Хочу записаться на завтра")
    say(3003, "Заработок в крипте, казино и ставки на спорт")
    say(3004, "Ужасный сервис, верните деньги")
    say(9001, "Сколько стоит маникюр?", HOOK_B)
    check(
        "компании, сотрудники, боты и диалоги подготовлены",
        len(db_rows("SELECT id FROM conversations")) == 5,
    )

    def conv_id(chat: int, biz: int = biz_a) -> int:
        return db_rows(
            "SELECT c.id FROM conversations c JOIN customers u ON u.id = c.customer_id "
            "WHERE u.external_id = ? AND c.business_id = ? ORDER BY c.id DESC LIMIT 1",
            str(chat),
            biz,
        )[0]["id"]

    # ----------------------------------------------------------------------- #
    print("\n=== 1. Настройки AI компании (раздел 13: стиль, разрешённые действия) ===")
    b = c.get(f"/businesses/{biz_a}", headers=H["owner_a"]).json()
    check(
        "по умолчанию: тон FRIENDLY, автоответ включён",
        b["ai_tone"] == "FRIENDLY" and b["ai_auto_reply"] is True,
    )
    r = c.put(
        f"/businesses/{biz_a}",
        headers=H["owner_a"],
        json={"ai_tone": "FORMAL", "ai_rules": "Только по делу"},
    )
    check(
        "PUT: стиль и правила сохраняются", r.status_code == 200 and r.json()["ai_tone"] == "FORMAL"
    )
    check(
        "неизвестный стиль → 422",
        c.put(f"/businesses/{biz_a}", headers=H["owner_a"], json={"ai_tone": "RUDE"}).status_code
        == 422,
    )
    check(
        "ai_tone=null → 422 (не 500)",
        c.put(f"/businesses/{biz_a}", headers=H["owner_a"], json={"ai_tone": None}).status_code
        == 422,
    )
    check(
        "name=null → 422 (не 500)",
        c.put(f"/businesses/{biz_a}", headers=H["owner_a"], json={"name": None}).status_code == 422,
    )
    check(
        "MANAGER не меняет настройки AI → 403",
        c.put(f"/businesses/{biz_a}", headers=H["manager1"], json={"ai_tone": "BRIEF"}).status_code
        == 403,
    )
    check(
        "тон не изменился после отказов",
        c.get(f"/businesses/{biz_a}", headers=H["owner_a"]).json()["ai_tone"] == "FORMAL",
    )

    k = BusinessKnowledge(business_id=1, name="Б", tone="BRIEF")
    prompt = build_responder_messages("привет", [], k, "OTHER", "COLD", 10, 700)[0]["content"]
    check("промпт: стиль BRIEF попадает в инструкции", "предельно краткий" in prompt)
    k2 = BusinessKnowledge(business_id=1, name="Б", tone="FORMAL")
    check(
        "промпт: стиль FORMAL — на «вы»",
        "на «вы»" in build_responder_messages("x", [], k2, "OTHER", "COLD", 10, 700)[0]["content"],
    )
    check(
        "промпт: ограничения не ослаблены стилем",
        "Запрещено называть цены" in prompt and "ТОЛЬКО факты" in prompt,
    )

    c.put(f"/businesses/{biz_a}", headers=H["owner_a"], json={"ai_auto_reply": False})
    sent_before = len(fake.sent(TOKEN_A))
    say(3010, "Сколько стоит стрижка?")
    conv10 = conv_id(3010)
    row = db_rows("SELECT * FROM conversations WHERE id = ?", conv10)[0]
    resp = db_rows(
        "SELECT * FROM ai_responses WHERE message_id = (SELECT id FROM messages WHERE conversation_id = ? AND sender_type='CUSTOMER')",
        conv10,
    )[0]
    # Решение 2026-09-27: автоответы выключены — клиенту не уходит ничего, даже шаблон.
    check(
        "автоответ выключен: клиенту ничего не отправлено",
        len(fake.sent(TOKEN_A)) == sent_before,
    )
    check(
        "автоответ выключен: исходящих сообщений AI в диалоге нет",
        not db_rows(
            "SELECT id FROM messages WHERE conversation_id = ? AND sender_type = 'AI'", conv10
        ),
    )
    check(
        "автоответ выключен: диалог у менеджера, причина AUTO_REPLY_DISABLED",
        row["status"] == "NEEDS_ATTENTION"
        and row["attention_reason"] == "AUTO_REPLY_DISABLED"
        and resp["escalation_reason"] == "AUTO_REPLY_DISABLED"
        and resp["status"] == "ESCALATED",
    )
    check(
        "автоответ выключен: лид всё равно создан",
        db_rows("SELECT priority FROM leads WHERE conversation_id = ?", conv10)[0]["priority"]
        == "WARM",
    )
    pv = c.post(
        f"/businesses/{biz_a}/ai/preview",
        headers=H["owner_a"],
        json={"text": "Сколько стоит стрижка?"},
    ).json()
    check(
        "проверка ответа учитывает настройку",
        pv["decision"] == "ESCALATE" and pv["escalation_reason"] == "AUTO_REPLY_DISABLED",
    )
    check("проверка ответа: клиенту ничего не отправится", pv["client_reply"] is None)
    c.put(f"/businesses/{biz_a}", headers=H["owner_a"], json={"ai_auto_reply": True})
    pv = c.post(
        f"/businesses/{biz_a}/ai/preview",
        headers=H["owner_a"],
        json={"text": "Сколько стоит стрижка?"},
    ).json()
    check(
        "автоответ включён обратно: цена из прайса",
        pv["decision"] == "SEND" and "1500" in pv["reply"].replace("\u00a0", "").replace(" ", ""),
    )
    check(
        "проверка ответа включена по умолчанию (AI_PREVIEW_ENABLED=true)",
        settings.ai_preview_enabled is True,
    )
    with __import__("unittest.mock", fromlist=["patch"]).patch.object(
        settings, "ai_preview_rate_limit_per_minute", 2
    ):
        codes = [
            c.post(
                f"/businesses/{biz_a}/ai/preview", headers=H["owner_a"], json={"text": "Привет?"}
            ).status_code
            for _ in range(5)
        ]
    check("проверка ответа ограничена по частоте (LLM платный) → 429", 429 in codes, str(codes))

    # ----------------------------------------------------------------------- #
    print("\n=== 2. Сотрудники: роли и удаление (раздел 13) ===")
    mem = f"/businesses/{biz_a}/members"
    check(
        "MANAGER не меняет роли → 403",
        c.patch(
            f"{mem}/{uid['owner_a2']}", headers=H["manager1"], json={"role": "MANAGER"}
        ).status_code
        == 403,
    )
    check(
        "чужой владелец → 404",
        c.patch(
            f"{mem}/{uid['owner_a2']}", headers=H["owner_b"], json={"role": "MANAGER"}
        ).status_code
        == 404,
    )
    check(
        "без токена → 401",
        c.patch(f"{mem}/{uid['owner_a2']}", json={"role": "MANAGER"}).status_code == 401,
    )
    check(
        "неизвестная роль → 422",
        c.patch(f"{mem}/{uid['manager1']}", headers=H["owner_a"], json={"role": "GOD"}).status_code
        == 422,
    )
    check(
        "несуществующий сотрудник → 404",
        c.patch(f"{mem}/99999", headers=H["owner_a"], json={"role": "MANAGER"}).status_code == 404,
    )
    r = c.patch(f"{mem}/{uid['manager1']}", headers=H["owner_a"], json={"role": "OWNER"})
    check(
        "повышение менеджера до владельца",
        r.status_code == 200
        and r.json()["role"] == "OWNER"
        and r.json()["email"] == "manager1@example.com",
    )
    r = c.patch(f"{mem}/{uid['manager1']}", headers=H["owner_a"], json={"role": "MANAGER"})
    check("возврат в менеджеры", r.status_code == 200 and r.json()["role"] == "MANAGER")
    check(
        "смена роли записана в аудит",
        len(events("BUSINESS_MEMBER_UPDATED")) == 2
        and events("BUSINESS_MEMBER_UPDATED")[-1]["actor_user_id"] == uid["owner_a"],
    )
    # владелец A2 понижается, потом единственный владелец защищён
    check(
        "понижение второго владельца",
        c.patch(
            f"{mem}/{uid['owner_a2']}", headers=H["owner_a"], json={"role": "MANAGER"}
        ).status_code
        == 200,
    )
    r = c.patch(f"{mem}/{uid['owner_a']}", headers=H["owner_a"], json={"role": "MANAGER"})
    check("единственного владельца понизить нельзя → 409", r.status_code == 409, r.text[:80])
    check(
        "единственного владельца удалить нельзя → 409",
        c.delete(f"{mem}/{uid['owner_a']}", headers=H["owner_a"]).status_code == 409,
    )
    check(
        "владелец остался владельцем",
        db_rows(
            "SELECT role FROM business_members WHERE business_id=? AND user_id=?",
            biz_a,
            uid["owner_a"],
        )[0]["role"]
        == "OWNER",
    )
    c.patch(f"{mem}/{uid['owner_a2']}", headers=H["owner_a"], json={"role": "OWNER"})

    # лид назначен менеджеру → при удалении возвращается в общую очередь
    lead_id = db_rows("SELECT id FROM leads WHERE conversation_id = ?", conv_id(3001))[0]["id"]
    c.patch(f"/leads/{lead_id}", headers=H["owner_a"], json={"assigned_to": uid["manager1"]})
    check(
        "MANAGER не удаляет сотрудников → 403",
        c.delete(f"{mem}/{uid['owner_a2']}", headers=H["manager1"]).status_code == 403,
    )
    check(
        "чужой владелец не удаляет → 404",
        c.delete(f"{mem}/{uid['manager1']}", headers=H["owner_b"]).status_code == 404,
    )
    check(
        "удаление менеджера → 204",
        c.delete(f"{mem}/{uid['manager1']}", headers=H["owner_a"]).status_code == 204,
    )
    check(
        "его лид без ответственного, но не потерян",
        db_rows("SELECT assigned_to, status FROM leads WHERE id = ?", lead_id)[0][:]
        == (None, "NEW"),
    )
    check(
        "после удаления доступ к компании закрыт (404)",
        c.get(f"/businesses/{biz_a}", headers=H["manager1"]).status_code == 404,
    )
    check(
        "удаление записано в аудит",
        events("BUSINESS_MEMBER_REMOVED")[-1]["member_user_id"] == uid["manager1"],
    )
    check(
        "удалённого нельзя удалить повторно → 404",
        c.delete(f"{mem}/{uid['manager1']}", headers=H["owner_a"]).status_code == 404,
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 3. Приглашения по ссылке (раздел 13) ===")
    inv = f"/businesses/{biz_a}/invitations"
    check(
        "MANAGER/чужой/аноним не создают приглашения",
        c.post(inv, headers=H["outsider"], json={"email": "x@example.com"}).status_code == 404
        and c.post(inv, json={"email": "x@example.com"}).status_code == 401,
    )
    check(
        "неверный email → 422",
        c.post(inv, headers=H["owner_a"], json={"email": "не-почта"}).status_code == 422,
    )
    check(
        "уже в компании → 409",
        c.post(inv, headers=H["owner_a"], json={"email": "owner_a2@example.com"}).status_code
        == 409,
    )
    r = c.post(inv, headers=H["owner_a"], json={"email": "manager1@example.com", "role": "MANAGER"})
    check(
        "приглашение создано (201) со ссылкой",
        r.status_code == 201 and "/invite/" in r.json()["invite_url"],
        r.text[:100],
    )
    link1 = r.json()["invite_url"]
    token1 = link1.rsplit("/", 1)[1]
    check("ссылка строится от PUBLIC_BASE_URL", link1.startswith("https://leadpilot.test/invite/"))
    check(
        "в БД только хеш токена, самого токена нет",
        token1
        not in json.dumps([dict(x) for x in db_rows("SELECT * FROM invitations")], default=str),
    )
    check(
        "токен не попадает в system_logs",
        token1
        not in json.dumps([dict(x) for x in db_rows("SELECT * FROM system_logs")], default=str),
    )
    check(
        "список действующих приглашений",
        len(c.get(inv, headers=H["owner_a"]).json()) == 1
        and "invite_url" not in json.dumps(c.get(inv, headers=H["owner_a"]).json()),
    )
    check(
        "список приглашений закрыт от MANAGER-а и чужих",
        c.get(inv, headers=H["outsider"]).status_code == 404 and c.get(inv).status_code == 401,
    )
    r2 = c.post(inv, headers=H["owner_a"], json={"email": "manager1@example.com", "role": "OWNER"})
    link2 = r2.json()["invite_url"]
    check(
        "повторное приглашение аннулирует прежнюю ссылку",
        len(c.get(inv, headers=H["owner_a"]).json()) == 1 and link1 != link2,
    )
    acc = "/invitations/accept"
    check(
        "старая ссылка недействительна (404)",
        c.post(acc, headers=H["manager1"], json={"token": token1}).status_code == 404,
    )
    r = c.post(acc, headers=H["manager1"], json={"token": "выдуманный-токен"})
    check(
        "несуществующий токен → 404 без эха токена",
        r.status_code == 404 and "выдуманный" not in r.text,
    )
    check(
        "пустой токен → 422 без эха",
        c.post(acc, headers=H["manager1"], json={"token": ""}).status_code in (404, 422),
    )
    check(
        "без входа принять нельзя → 401",
        c.post(acc, json={"token": link2.rsplit("/", 1)[1]}).status_code == 401,
    )
    r = c.post(acc, headers=H["outsider"], json={"token": link2.rsplit("/", 1)[1]})
    check("приглашение на чужой адрес → 403", r.status_code == 403, r.text[:80])
    check(
        "отказ не дал доступа посторонним",
        c.get(f"/businesses/{biz_a}", headers=H["outsider"]).status_code == 404,
    )
    r = c.post(acc, headers=H["manager1"], json={"token": link2.rsplit("/", 1)[1]})
    check(
        "верный адрес: приглашение принято с нужной ролью",
        r.status_code == 200 and r.json()["role"] == "OWNER" and r.json()["business_id"] == biz_a,
        r.text[:100],
    )
    check(
        "после принятия доступ появился",
        c.get(f"/businesses/{biz_a}", headers=H["manager1"]).status_code == 200,
    )
    check(
        "повторное использование ссылки → 404",
        c.post(acc, headers=H["manager1"], json={"token": link2.rsplit("/", 1)[1]}).status_code
        == 404,
    )
    check("действующих приглашений не осталось", c.get(inv, headers=H["owner_a"]).json() == [])
    check(
        "аудит: создание, принятие",
        len(events("INVITATION_CREATED")) == 2
        and events("INVITATION_ACCEPTED")[-1]["role"] == "OWNER",
    )
    c.patch(f"{mem}/{uid['manager1']}", headers=H["owner_a"], json={"role": "MANAGER"})

    r = c.post(inv, headers=H["owner_a"], json={"email": "outsider@example.com", "role": "MANAGER"})
    inv_id, tok3 = r.json()["id"], r.json()["invite_url"].rsplit("/", 1)[1]
    check(
        "отзыв приглашения → 204",
        c.delete(f"{inv}/{inv_id}", headers=H["owner_a"]).status_code == 204,
    )
    check(
        "отозванная ссылка не работает",
        c.post(acc, headers=H["outsider"], json={"token": tok3}).status_code == 404,
    )
    check(
        "повторный отзыв → 404",
        c.delete(f"{inv}/{inv_id}", headers=H["owner_a"]).status_code == 404,
    )
    check(
        "чужой владелец не отзывает чужое приглашение",
        c.delete(f"{inv}/{inv_id}", headers=H["owner_b"]).status_code == 404,
    )
    r = c.post(inv, headers=H["owner_a"], json={"email": "outsider@example.com", "role": "MANAGER"})
    tok4 = r.json()["invite_url"].rsplit("/", 1)[1]
    db_exec(
        "UPDATE invitations SET expires_at = '2000-01-01 00:00:00.000000' WHERE email = 'outsider@example.com'"
    )
    check(
        "просроченная ссылка не работает",
        c.post(acc, headers=H["outsider"], json={"token": tok4}).status_code == 404,
    )
    check("просроченные не показываются в списке", c.get(inv, headers=H["owner_a"]).json() == [])

    # ----------------------------------------------------------------------- #
    print("\n=== 4. Аналитика (раздел 13) ===")
    an = f"/businesses/{biz_a}/analytics"
    r = c.get(an, headers=H["owner_a"])
    a = r.json()
    check(
        "аналитика за 30 суток (по умолчанию)", r.status_code == 200 and len(a["daily"]) in (30, 31)
    )
    inc = db_rows(
        "SELECT COUNT(*) AS n FROM messages WHERE business_id = ? AND sender_type = 'CUSTOMER'",
        biz_a,
    )[0]["n"]
    sent = db_rows(
        "SELECT COUNT(*) AS n FROM ai_responses WHERE business_id = ? AND decision = 'SEND'", biz_a
    )[0]["n"]
    esc = db_rows(
        "SELECT COUNT(*) AS n FROM ai_responses WHERE business_id = ? AND decision = 'ESCALATE'",
        biz_a,
    )[0]["n"]
    check(
        "сообщения клиентов = данные БД",
        a["messages_incoming"] == inc,
        f"{a['messages_incoming']} vs {inc}",
    )
    check(
        "ответов AI и передач = данные БД",
        a["ai_answered"] == sent and a["ai_escalated"] == esc,
        f"{a['ai_answered']}/{a['ai_escalated']} vs {sent}/{esc}",
    )
    check("доля самостоятельных ответов", a["ai_share_percent"] == round(sent * 100 / (sent + esc)))
    leads_db = db_rows(
        "SELECT priority, COUNT(*) AS n FROM leads WHERE business_id = ? GROUP BY priority", biz_a
    )
    check(
        "лиды по приоритету = данные БД",
        a["leads_by_priority"]
        == {
            "HOT": next((x["n"] for x in leads_db if x["priority"] == "HOT"), 0),
            "WARM": next((x["n"] for x in leads_db if x["priority"] == "WARM"), 0),
            "COLD": next((x["n"] for x in leads_db if x["priority"] == "COLD"), 0),
        },
    )
    check("сумма лидов по статусам = всего", sum(a["leads_by_status"].values()) == a["leads_total"])
    check(
        "намерения клиентов посчитаны",
        a["intents"].get("PRICE", 0) >= 2 and a["intents"].get("BOOKING", 0) == 1,
    )
    check("серия по дням: сегодня есть входящие", a["daily"][-1]["incoming"] == inc)
    check(
        "данные компании B не смешиваются",
        a["messages_incoming"]
        != db_rows("SELECT COUNT(*) AS n FROM messages WHERE sender_type='CUSTOMER'")[0]["n"],
    )
    check("время ответа модели считается", a["ai_avg_latency_ms"] is not None)
    r = c.get(
        an,
        headers=H["owner_a"],
        params={"date_from": "2020-01-01T00:00:00", "date_to": "2020-01-31T00:00:00"},
    )
    check(
        "прошлый период: нули",
        r.status_code == 200
        and r.json()["messages_incoming"] == 0
        and len(r.json()["daily"]) == 31,
    )
    check(
        "начало позже конца → 422",
        c.get(
            an, headers=H["owner_a"], params={"date_from": "2026-02-01", "date_to": "2026-01-01"}
        ).status_code
        == 422,
    )
    check(
        "период длиннее года → 422",
        c.get(
            an, headers=H["owner_a"], params={"date_from": "2020-01-01", "date_to": "2026-01-01"}
        ).status_code
        == 422,
    )
    check("MANAGER не видит аналитику → 403", c.get(an, headers=H["manager1"]).status_code == 403)
    check(
        "чужой владелец → 404, аноним → 401",
        c.get(an, headers=H["owner_b"]).status_code == 404 and c.get(an).status_code == 401,
    )
    check("ADMIN видит аналитику любой компании", c.get(an, headers=hadmin).status_code == 200)
    st = c.get(f"/businesses/{biz_a}/inbox/state", headers=H["manager1"]).json()
    check(
        "состояние входящих: последнее сообщение и очередь",
        st["last_message_id"]
        == db_rows("SELECT MAX(id) AS m FROM messages WHERE business_id = ?", biz_a)[0]["m"]
        and st["attention"] >= 3,
    )
    check(
        "состояние входящих закрыто от чужих",
        c.get(f"/businesses/{biz_a}/inbox/state", headers=H["owner_b"]).status_code == 404,
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 5. CSRF: запросы из браузера с чужого сайта (раздел 16) ===")
    cookie_client = TestClient(app)
    cookie_client.post("/auth/login", json={"email": "owner_a@example.com", "password": PWD})
    check("cookie-сессия выдана", settings.auth_cookie_name in cookie_client.cookies)
    payload = {"phone": "+7 111 111-11-11"}
    put = f"/businesses/{biz_a}"
    r = cookie_client.put(put, json=payload, headers={"Origin": "https://evil.example"})
    check("cookie + Origin чужого сайта → 403", r.status_code == 403, r.text[:80])
    check(
        "данные не изменились",
        c.get(put, headers=H["owner_a"]).json()["phone"] == "+7 999 000-00-00",
    )
    check(
        "cookie + Referer чужого сайта → 403",
        cookie_client.put(
            put, json=payload, headers={"Referer": "https://evil.example/page"}
        ).status_code
        == 403,
    )
    check(
        "cookie + Origin: null (sandbox-форма) → 403",
        cookie_client.put(put, json=payload, headers={"Origin": "null"}).status_code == 403,
    )
    check(
        "cookie + Origin нашего сайта → 200",
        cookie_client.put(put, json=payload, headers={"Origin": "http://testserver"}).status_code
        == 200,
    )
    check(
        "cookie + Origin PUBLIC_BASE_URL → 200",
        cookie_client.put(
            put, json={"phone": "+7 222"}, headers={"Origin": "https://leadpilot.test"}
        ).status_code
        == 200,
    )
    check(
        "cookie без Origin (не браузер) → 200",
        cookie_client.put(put, json={"phone": "+7 999 000-00-00"}).status_code == 200,
    )
    check(
        "Bearer + чужой Origin → 200 (токен не уходит сам)",
        c.put(
            put, json=payload, headers={**H["owner_a"], "Origin": "https://evil.example"}
        ).status_code
        == 200,
    )
    c.put(put, json={"phone": "+7 999 000-00-00"}, headers=H["owner_a"])
    check(
        "GET с чужим Origin не блокируется",
        cookie_client.get(put, headers={"Origin": "https://evil.example"}).status_code == 200,
    )
    check(
        "вход с чужого сайта → 403 (login CSRF)",
        TestClient(app)
        .post(
            "/auth/login",
            json={"email": "owner_a@example.com", "password": PWD},
            headers={"Origin": "https://evil.example"},
        )
        .status_code
        == 403,
    )
    check(
        "регистрация с чужого сайта → 403",
        TestClient(app)
        .post(
            "/auth/register",
            json={"email": "evil@example.com", "password": PWD},
            headers={"Origin": "https://evil.example"},
        )
        .status_code
        == 403,
    )
    check(
        "ручной ответ с чужого сайта → 403",
        cookie_client.post(
            f"/conversations/{conv_id(3002)}/reply",
            json={"text": "x"},
            headers={"Origin": "https://evil.example"},
        ).status_code
        == 403,
    )
    check(
        "отказы CSRF не отправили сообщений клиенту",
        not any(b["text"] == "x" for b in fake.sent(TOKEN_A)),
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 6. Страницы: вход, редиректы, открытое перенаправление ===")
    anon = TestClient(app)
    r = anon.get("/", follow_redirects=False)
    check("/ → /cabinet", r.status_code == 303 and r.headers["location"] == "/cabinet")
    r = anon.get("/cabinet", follow_redirects=False)
    check(
        "аноним на /cabinet → /login",
        r.status_code == 303 and r.headers["location"].startswith("/login"),
    )
    r = anon.get(f"/cabinet/{biz_a}/messages?c=1", follow_redirects=False)
    check(
        "аноним на страницу компании → /login с возвратом",
        r.status_code == 303
        and r.headers["location"].startswith("/login?next=/cabinet/")
        and "messages" in r.headers["location"],
        r.headers["location"],
    )
    for evil in (
        "//evil.example",
        "https://evil.example",
        "/\\evil.example",
        "javascript:alert(1)",
        "/administrator",  # похожий префикс: /admin с этапа 6 — допустимая цель, это — нет
    ):
        page = anon.get("/login", params={"next": evil})
        m = re.search(r'data-redirect="([^"]*)"', page.text)
        check(
            f"login?next={evil[:22]} не даёт уйти на чужой адрес",
            m is not None and m.group(1) == "/cabinet",
            str(m and m.group(1)),
        )
    page = anon.get("/login", params={"next": "/cabinet/1/leads?priority=HOT"})
    check(
        "внутренний next сохраняется",
        'data-redirect="/cabinet/1/leads?priority=HOT"' in page.text.replace("&amp;", "&"),
    )
    check(
        "страницы входа и регистрации открыты",
        anon.get("/login").status_code == 200 and anon.get("/register").status_code == 200,
    )
    check(
        "вход и регистрация показывают форму без секретов",
        "password" in anon.get("/login").text and "jwt" not in anon.get("/login").text.lower(),
    )
    r = cookie_client.get("/login", follow_redirects=False)
    check(
        "вошедшего с /login → в кабинет",
        r.status_code == 303 and r.headers["location"] == "/cabinet",
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 7. Страницы кабинета и права ролей (разделы 5, 13, 14) ===")
    owner = TestClient(app)
    owner.post("/auth/login", json={"email": "owner_a@example.com", "password": PWD})
    manager = TestClient(app)
    manager.post("/auth/login", json={"email": "manager1@example.com", "password": PWD})
    other = TestClient(app)
    other.post("/auth/login", json={"email": "owner_b@example.com", "password": PWD})
    admin = TestClient(app)
    admin.post("/auth/login", json={"email": "admin@example.com", "password": "Adm1n-Pass-123!"})

    pages_all = ["", "/messages", "/leads", "/customers", "/services"]
    pages_owner = ["/ai", "/team", "/settings", "/analytics"]
    home = f"/cabinet/{biz_a}"
    check(
        "владелец: все страницы 200",
        all(owner.get(home + p).status_code == 200 for p in pages_all + pages_owner),
    )
    check(
        "менеджер: рабочие страницы 200",
        all(manager.get(home + p).status_code == 200 for p in pages_all),
    )
    codes = {p: manager.get(home + p).status_code for p in pages_owner}
    check(
        "менеджер: настройки, AI, сотрудники, аналитика → 403",
        set(codes.values()) == {403},
        str(codes),
    )
    check(
        "страница 403 — понятный HTML, а не JSON",
        "Недостаточно прав" in manager.get(home + "/settings").text
        and manager.get(home + "/settings").headers["content-type"].startswith("text/html"),
    )
    check(
        "чужая компания: все страницы 404",
        all(other.get(home + p).status_code == 404 for p in pages_all + pages_owner),
    )
    check(
        "несуществующая компания → 404 HTML",
        "не найдена" in owner.get("/cabinet/99999").text
        and owner.get("/cabinet/99999").status_code == 404,
    )
    check(
        "ADMIN открывает страницы любой компании",
        all(admin.get(home + p).status_code == 200 for p in pages_all + pages_owner),
    )
    nav_owner, nav_mgr = owner.get(home).text, manager.get(home).text
    check(
        "меню владельца содержит настройки и аналитику",
        "/settings" in nav_owner and "/analytics" in nav_owner and "/team" in nav_owner,
    )
    check(
        "меню менеджера без настроек, AI, сотрудников и аналитики",
        all(
            x not in nav_mgr
            for x in (f"{home}/settings", f"{home}/ai", f"{home}/team", f"{home}/analytics")
        ),
    )
    r = manager.get(home + "/services")
    check(
        "менеджер: услуги только для просмотра",
        "Добавить услугу" not in r.text and "data-service-edit" not in r.text,
    )
    r = owner.get(home + "/services")
    check(
        "владелец: кнопки услуг на месте",
        "data-service-new" in r.text and "data-service-edit" in r.text,
    )
    check(
        "нет утечки компании B в чужих страницах",
        "Лилия" not in owner.get(home + "/messages").text
        and "маникюр" not in owner.get(home + "/messages").text.lower(),
    )
    conv_b = db_rows("SELECT id FROM conversations WHERE business_id = ?", biz_b)[0]["id"]
    r = owner.get(f"{home}/messages?c={conv_b}")
    check(
        "c=диалог ЧУЖОЙ компании игнорируется без утечки",
        r.status_code == 200 and "маникюр" not in r.text.lower(),
    )
    cust_b = db_rows("SELECT id FROM customers WHERE business_id = ?", biz_b)[0]["id"]
    check("клиент чужой компании → 404", owner.get(f"{home}/customers/{cust_b}").status_code == 404)

    print("\n=== 8. Содержимое страниц ===")
    r = owner.get(home)
    check(
        "обзор: показатели и очередь",
        "Ждут вашего внимания" in r.text and "Очередь: нужен человек" in r.text,
    )
    attention_n = db_rows(
        "SELECT COUNT(*) AS n FROM conversations WHERE business_id = ? AND status = ?",
        biz_a,
        "NEEDS_ATTENTION",
    )[0]["n"]
    check(
        "обзор: счётчик в меню = число диалогов «требует внимания»",
        f'title="Требуют внимания">{attention_n}<' in r.text,
    )
    # Этап 8: чек-лист из 4 шагов; выполненные (услуги, бот, первое сообщение) — без кнопок.
    check(
        "обзор: выполненные шаги настройки без кнопок, когда услуги, бот и диалоги есть",
        ">К услугам<" not in r.text and ">К сообщениям<" not in r.text,
    )
    r = owner.get(home + "/messages")
    check(
        "сообщения: список диалогов и выбранный диалог",
        r.text.count('class="row-link"') >= 4 and 'id="log"' in r.text,
    )
    check(
        "сообщения: форма ответа отправляет на API",
        'data-url="/conversations/' in r.text and "/reply" in r.text,
    )
    r = owner.get(home + "/messages", params={"status": "NEEDS_ATTENTION", "priority": "HOT"})
    inbox_rows = r.text.split('class="inbox-scroll"')[1].split('class="thread"')[0]
    check(
        "сообщения: фильтр «требуют внимания» + «горячие»",
        inbox_rows.count('class="row-link"') >= 2
        and "Тёплый" not in inbox_rows
        and "Холодный" not in inbox_rows
        and "Решён" not in inbox_rows,
    )
    check(
        "мусор в фильтрах игнорируется (200)",
        owner.get(
            home + "/messages", params={"status": "???", "priority": "x", "c": "abc"}
        ).status_code
        == 200,
    )
    check(
        "некорректный offset → 422, не 500",
        owner.get(home + "/messages", params={"offset": "abc"}).status_code == 422,
    )
    r = owner.get(home + "/leads", params={"priority": "HOT"})
    check(
        "лиды: фильтр HOT показывает только горячие",
        "Горячий" in r.text and "Тёплый" not in r.text.split("<tbody>")[1],
    )
    check(
        "лиды: статус и ответственный меняются через API",
        'data-method="PATCH"' in r.text and 'data-field="assigned_to"' in r.text,
    )
    check("лиды: причина без служебного префикса «Правила:»", "Правила:" not in r.text)
    check(
        "лиды: фильтр по несуществующему периоду → пусто",
        "Лидов по этим условиям нет"
        in owner.get(home + "/leads", params={"date_to": "2000-01-01"}).text,
    )
    check(
        "клиенты: список и поиск",
        "Клиенты" in owner.get(home + "/customers").text
        and owner.get(home + "/customers", params={"q": "иван"}).text.count("/customers/") >= 3,
    )
    cid = db_rows("SELECT id FROM customers WHERE business_id = ? ORDER BY id LIMIT 1", biz_a)[0][
        "id"
    ]
    check(
        "клиент: история обращений",
        "История обращений" in owner.get(f"{home}/customers/{cid}").text,
    )
    check(
        "AI: форма настроек и проверка ответа",
        'name="ai_tone"' in owner.get(home + "/ai").text
        and "data-ai-preview" in owner.get(home + "/ai").text,
    )
    check(
        "AI: офлайн-режим объяснён владельцу", "упрощённом режиме" in owner.get(home + "/ai").text
    )
    check(
        "команда: роли и приглашение",
        "data-invite-new" in owner.get(home + "/team").text
        and "manager1@example.com" in owner.get(home + "/team").text,
    )
    check(
        "настройки: Telegram подключён, токена на странице нет",
        "принимает сообщения клиентов" in owner.get(home + "/settings").text
        and TOKEN_A not in owner.get(home + "/settings").text
        and fake.secret(TOKEN_A) not in owner.get(home + "/settings").text,
    )
    r = owner.get(home + "/analytics")
    check(
        "аналитика: график и показатели", "<svg" in r.text and "Ассистент справился сам" in r.text
    )
    check(
        "аналитика: некорректный период → последние 30 суток (200)",
        owner.get(
            home + "/analytics", params={"date_from": "2026-05-01", "date_to": "2026-01-01"}
        ).status_code
        == 200,
    )
    check(
        "аналитика: готовые периоды 7/30/90",
        all(
            owner.get(home + "/analytics", params={"days": d}).status_code == 200
            for d in (7, 30, 90)
        ),
    )

    print("\n=== 9. Кабинет: приглашение, онбординг, выбор компании ===")
    fresh = TestClient(app)
    fresh.post("/auth/register", json={"email": "fresh@example.com", "password": PWD})
    fresh.post("/auth/login", json={"email": "fresh@example.com", "password": PWD})
    r = fresh.get("/cabinet")
    check(
        "новый пользователь: форма создания компании",
        r.status_code == 200 and 'data-url="/businesses"' in r.text,
    )
    tok_page = (
        c.post(inv, headers=H["owner_a"], json={"email": "fresh@example.com", "role": "MANAGER"})
        .json()["invite_url"]
        .rsplit("/", 1)[1]
    )
    r = anon.get(f"/invite/{tok_page}")
    check(
        "страница приглашения: компания, роль, адрес",
        r.status_code == 200
        and "Бритва" in r.text
        and "Менеджер" in r.text
        and "fresh@example.com" in r.text,
    )
    check(
        "страница приглашения для гостя: регистрация с подставленной почтой",
        "email=fresh%40example.com" in r.text,
    )
    check(
        "страница приглашения: вошёл нужный адрес → кнопка принять",
        "data-accept-invite" in fresh.get(f"/invite/{tok_page}").text,
    )
    check(
        "страница приглашения: вошёл другой адрес → предупреждение",
        "другой адрес" in other.get(f"/invite/{tok_page}").text
        and "data-accept-invite" not in other.get(f"/invite/{tok_page}").text,
    )
    r = anon.get("/invite/несуществующий-токен")
    check(
        "невалидная ссылка → 404 с понятным текстом",
        r.status_code == 404 and "недействительно" in r.text,
    )
    check(
        "после принятия у пользователя одна компания → сразу в неё",
        (fresh.post("/invitations/accept", json={"token": tok_page}).status_code == 200)
        and fresh.get("/cabinet", follow_redirects=False).headers["location"] == home,
    )
    check(
        "менеджер по приглашению видит рабочие страницы, но не настройки",
        fresh.get(home + "/leads").status_code == 200
        and fresh.get(home + "/settings").status_code == 403,
    )
    two = TestClient(app)
    two.post("/auth/login", json={"email": "owner_b@example.com", "password": PWD})
    c.post(
        f"/businesses/{biz_b}/members",
        headers=H["owner_b"],
        json={"email": "owner_a@example.com", "role": "MANAGER"},
    )
    r = owner.get("/cabinet")
    check(
        "две компании: страница выбора",
        r.status_code == 200 and "Выберите компанию" in r.text and "Салон «Лилия»" in r.text,
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 10. XSS: чужой текст выводится как данные (раздел 16) ===")
    say(3050, "<script>window.__x=1</script><img src=x onerror=alert(1)> Сколько стоит?")
    db_exec(
        "UPDATE customers SET name = ? WHERE external_id = '3050'", '"><svg onload=alert(2)>Хакер'
    )
    c.post(
        f"/businesses/{biz_a}/services",
        headers=H["owner_a"],
        json={"name": 'x" onmouseover="alert(3)', "price": "1", "description": "<b>жирный</b>"},
    )
    c.put(
        f"/businesses/{biz_a}",
        headers=H["owner_a"],
        json={
            "ai_rules": "</textarea><script>alert(4)</script>",
            "description": "</textarea><script>alert(5)</script>",
        },
    )
    conv50 = conv_id(3050)
    pages = {
        "messages": owner.get(f"{home}/messages?c={conv50}").text,
        "dashboard": owner.get(home).text,
        "leads": owner.get(home + "/leads").text,
        "customers": owner.get(home + "/customers").text,
        "services": owner.get(home + "/services").text,
        "ai": owner.get(home + "/ai").text,
        "settings": owner.get(home + "/settings").text,
    }
    check(
        "текст сообщения экранирован",
        "&lt;script&gt;window.__x=1&lt;/script&gt;" in pages["messages"]
        and "<script>window.__x" not in pages["messages"],
    )
    check("тег в тексте сообщения не стал элементом", "<img src=x" not in pages["messages"])
    check(
        "имя клиента с разметкой экранировано везде",
        all("<svg onload" not in html for html in pages.values())
        and "&lt;svg onload=alert(2)&gt;" in pages["customers"],
    )
    check(
        "кавычка в названии услуги не создаёт атрибут (data-атрибуты)",
        'onmouseover="alert(3)' not in pages["services"]
        and "&#34; onmouseover=&#34;alert(3)" in pages["services"],
    )
    check(
        "HTML в описании услуги не исполняется",
        "<b>жирный</b>" not in pages["services"]
        and "&lt;b&gt;жирный&lt;/b&gt;" in pages["services"],
    )
    check(
        "правила AI не выходят из textarea",
        "<script>alert(4)</script>" not in pages["ai"]
        and "&lt;/textarea&gt;&lt;script&gt;" in pages["ai"],
    )
    check(
        "описание компании не выходит из textarea",
        "<script>alert(5)</script>" not in pages["settings"],
    )
    everything = (
        "\n".join(pages.values())
        + manager.get(home).text
        + owner.get(home + "/team").text
        + owner.get(home + "/analytics").text
    )
    check("нет ни одного inline-скрипта", not re.search(r"<script(?![^>]*\bsrc=)", everything))
    check(
        "нет inline-обработчиков событий (onclick, onerror…)",
        not re.search(r"<[^>]+\son[a-z]+\s*=\s*[\"']", everything),
        "",
    )
    check("нет inline-стилей (style=)", "style=" not in everything.replace("&#34; onmouseover", ""))
    check("нет |safe: динамика не содержит сырых <", "javascript:" not in everything)

    # ----------------------------------------------------------------------- #
    print("\n=== 11. Заголовки безопасности и статика (раздел 16) ===")
    for path, session in (("/login", anon), (home, owner), (home + "/messages", owner)):
        r = session.get(path)
        csp = r.headers.get("content-security-policy", "")
        ok = (
            "script-src 'self'" in csp
            and "style-src 'self'" in csp
            and "frame-ancestors 'none'" in csp
            and "object-src 'none'" in csp
            and "base-uri 'none'" in csp
            and "unsafe-inline" not in csp
            and "unsafe-eval" not in csp
        )
        check(f"{path[:22]}: строгий CSP без unsafe-*", ok, csp[:60])
        check(
            f"{path[:22]}: запрет фреймов, nosniff, no-store",
            r.headers.get("x-frame-options") == "DENY"
            and r.headers.get("x-content-type-options") == "nosniff"
            and r.headers.get("cache-control") == "no-store",
        )
    r = c.get("/health")
    check(
        "API: nosniff и запрет фреймов на всех ответах",
        r.headers.get("x-content-type-options") == "nosniff"
        and r.headers.get("x-frame-options") == "DENY"
        and "content-security-policy" not in r.headers,
    )
    check(
        "страницы ошибок тоже под CSP",
        "content-security-policy" in owner.get("/cabinet/99999").headers,
    )
    for path, ctype in (
        ("/static/css/styles.css", "text/css"),
        ("/static/js/app.js", "javascript"),
        ("/static/js/pages.js", "javascript"),
        ("/static/fonts/golos-cyrillic.woff2", "font/woff2"),
    ):
        r = c.get(path)
        check(
            f"статика {path.rsplit('/', 1)[1]} отдаётся",
            r.status_code == 200 and ctype in r.headers["content-type"] and len(r.content) > 500,
        )
    check(
        "шрифты собственные: внешних адресов в CSS нет",
        "http://" not in c.get("/static/css/styles.css").text
        and "https://" not in c.get("/static/css/styles.css").text,
    )
    js = c.get("/static/js/app.js").text
    check(
        "JS: нет innerHTML/eval/document.write (вывод только через textContent)",
        not re.search(
            r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function", js
        ),
    )
    check(
        "страницы не подключают внешних ресурсов",
        not re.search(r'(src|href)="https?://', everything),
    )

    print("\n=== 12. Выход и сессия ===")
    r = owner.post("/auth/logout")
    check("выход очищает cookie", r.status_code == 204)
    check("после выхода кабинет закрыт", owner.get(home, follow_redirects=False).status_code == 303)

print("\n" + "=" * 70)
print(f"ИТОГО: пройдено {len(PASSED)}, провалено {len(FAILED)}")
if FAILED:
    print("Провалены:")
    for name in FAILED:
        print("  -", name)
    sys.exit(1)
