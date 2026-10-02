"""
Проверочный скрипт этапа 6 — административная панель (НЕ часть приложения, можно удалить).

Покрывает разделы 5, 11, 15, 16, 17 ТЗ и критерии приёмки 12, 13: доступ только для ADMIN
(API и страницы), список компаний с поиском и фильтрами, смена статуса (suspended реально
останавливает AI, но не теряет сообщение), тариф и пробный период, метрики SaaS (MRR),
системные логи с маскированием секретов, аудит критических действий, защиту от XSS и CSRF.
Telegram подменяется httpx.MockTransport, AI работает офлайн (AI_PROVIDER=stub).

Запуск:  python smoke_test_stage6.py
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sqlite3
import sys
from datetime import UTC, datetime

BASE = pathlib.Path(__file__).resolve().parent
DB = BASE / "test_smoke_stage6.db"
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
    TRIAL_DAYS="14",
    PLAN_PRICE_START_RUB="1990",
    PLAN_PRICE_PRO_RUB="4990",
)
sys.path.insert(0, str(BASE))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ai.pipeline import REPLY_STAFF_WILL_ANSWER  # noqa: E402
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
    rows = db_rows(
        "SELECT business_id, level, message, metadata FROM system_logs "
        "WHERE event_type = ? ORDER BY id",
        event_type,
    )
    return [
        {
            "business_id": r["business_id"],
            "level": r["level"],
            "message": r["message"],
            **json.loads(r["metadata"] or "{}"),
        }
        for r in rows
    ]


def iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


PWD = "Str0ng-Pass-1"
ADMIN_PWD = "Adm1n-Pass-123!"
_uid = {"n": 0}
EVIL_NAME = "<script>alert(1)</script>Салон"
EVIL_MESSAGE = '<img src=x onerror="alert(2)">'


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


everything: list[str] = []  # все ответы админки: по ним ищем утечки секретов и разметки


html_pages: list[str] = []  # только HTML: разметку проверяем по нему (в JSON чужой текст — данные)


def keep(response: httpx.Response) -> httpx.Response:
    everything.append(response.text)
    if "text/html" in response.headers.get("content-type", ""):
        html_pages.append(response.text)
    return response


with TestClient(app) as c:
    # ----------------------------------------------------------------------- #
    print("\n=== 0. Подготовка ===")
    people = ["owner_a", "manager1", "owner_b", "owner_c", "outsider"]
    for name in people:
        c.post("/auth/register", json={"email": f"{name}@example.com", "password": PWD})

    def bearer(email: str, password: str | None = None) -> dict:
        token = c.post("/auth/login", json={"email": email, "password": password or PWD}).json()[
            "access_token"
        ]
        c.cookies.clear()
        return {"Authorization": f"Bearer {token}"}

    H = {name: bearer(f"{name}@example.com") for name in people}
    HA = bearer("admin@example.com", ADMIN_PWD)
    biz_a = c.post("/businesses", headers=H["owner_a"], json={"name": "Барбершоп «Бритва»"}).json()[
        "id"
    ]
    biz_b = c.post("/businesses", headers=H["owner_b"], json={"name": "Салон «Лилия»"}).json()["id"]
    biz_c = c.post("/businesses", headers=H["owner_c"], json={"name": EVIL_NAME}).json()["id"]
    for biz, owner, price in ((biz_a, "owner_a", "1500.00"), (biz_b, "owner_b", "2000.00")):
        c.post(
            f"/businesses/{biz}/services",
            headers=H[owner],
            json={"name": "Стрижка", "price": price},
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

    def say(chat: int, text: str, hook: dict) -> None:
        r = c.post("/webhooks/telegram", json=upd(chat, text), headers=hook)
        assert r.status_code == 200, r.text

    say(4001, "Сколько стоит стрижка?", HOOK_A)
    say(4002, "Хочу записаться на завтра", HOOK_A)
    say(4003, "Сколько стоит стрижка?", HOOK_B)
    ai_sent_a = len(fake.sent(TOKEN_A))
    check(
        "компании, бот и диалоги подготовлены",
        len(db_rows("SELECT id FROM businesses")) == 3
        and len(db_rows("SELECT id FROM messages")) >= 6
        and ai_sent_a >= 1,
    )
    check(
        "каждая новая компания получила пробную подписку (14 суток)",
        len(db_rows("SELECT id FROM subscriptions WHERE plan = 'TRIAL' AND status = 'ACTIVE'")) == 3
        and all(
            abs(
                (iso(r["expires_at"]) - iso(r["started_at"])).total_seconds()
                - settings.trial_days * 86400
            )
            < 5
            for r in db_rows("SELECT started_at, expires_at FROM subscriptions")
        ),
    )
    reg = c.post(
        "/auth/register",
        json={"email": "sneaky@example.com", "password": PWD, "role": "ADMIN"},
    )
    sneaky = c.get("/me", headers=bearer("sneaky@example.com")).json()
    check(
        "публичная регистрация не создаёт ADMIN (§5)",
        reg.status_code in (200, 201) and sneaky["user"]["role"] == "OWNER",
        str(sneaky["user"]["role"]),
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 1. Доступ только для ADMIN: JSON API (раздел 15, критерий 12) ===")
    api_calls = [
        ("GET", "/admin/businesses", None),
        ("GET", "/admin/logs", None),
        ("GET", "/admin/metrics", None),
        ("PUT", f"/admin/businesses/{biz_a}/status", {"status": "SUSPENDED"}),
        ("PUT", f"/admin/businesses/{biz_a}/plan", {"plan": "PRO"}),
    ]
    denied_before = len(events("ACCESS_DENIED"))
    for method, url, body in api_calls:
        label = f"{method} {url.split('/')[2] if url.count('/') > 1 else url}"
        anon = c.request(method, url, json=body)
        check(f"{label}: без токена → 401", anon.status_code == 401, str(anon.status_code))
        for who in ("owner_a", "manager1", "outsider"):
            r = c.request(method, url, json=body, headers=H[who])
            check(f"{label}: {who} → 403", r.status_code == 403, str(r.status_code))
    check(
        "статус компании и подписка не изменились от чужих запросов",
        db_rows("SELECT status FROM businesses WHERE id = ?", biz_a)[0]["status"] == "TRIAL"
        and db_rows("SELECT plan FROM subscriptions WHERE business_id = ?", biz_a)[0]["plan"]
        == "TRIAL",
    )
    check(
        "каждая отклонённая попытка записана как ACCESS_DENIED (§17)",
        len(events("ACCESS_DENIED")) - denied_before == len(api_calls) * 3,
        str(len(events("ACCESS_DENIED")) - denied_before),
    )
    check(
        "ответ 403 — JSON без данных панели",
        c.get("/admin/businesses", headers=H["owner_a"])
        .headers["content-type"]
        .startswith("application/json")
        and "Барбершоп" not in c.get("/admin/businesses", headers=H["owner_a"]).text,
    )
    for method, url, body in api_calls:
        r = keep(c.request(method, url, json=body, headers=HA))
        check(f"ADMIN: {method} {url} → 200", r.status_code == 200, str(r.status_code))
    # эти вызовы поменяли состояние: возвращаем исходное
    c.put(f"/admin/businesses/{biz_a}/status", headers=HA, json={"status": "TRIAL"})
    c.put(f"/admin/businesses/{biz_a}/plan", headers=HA, json={"plan": "TRIAL"})

    # ----------------------------------------------------------------------- #
    print("\n=== 2. Доступ только для ADMIN: страницы ===")
    pages = ["/admin", "/admin/companies", f"/admin/companies/{biz_a}", "/admin/events"]
    for url in pages:
        anon = c.get(url, follow_redirects=False)
        check(
            f"{url}: аноним → редирект на вход",
            anon.status_code == 303 and anon.headers["location"].startswith("/login?next=/admin"),
            anon.headers.get("location", ""),
        )
    owner = TestClient(app)
    owner.post("/auth/login", json={"email": "owner_a@example.com", "password": PWD})
    for url in pages:
        r = owner.get(url, follow_redirects=False)
        check(
            f"{url}: владелец компании → 403 страница ошибки",
            r.status_code == 403
            and "text/html" in r.headers["content-type"]
            and "Барбершоп" not in r.text,
            str(r.status_code),
        )
    admin = TestClient(app)
    admin.post("/auth/login", json={"email": "admin@example.com", "password": ADMIN_PWD})
    check("cookie-сессия ADMIN выдана", settings.auth_cookie_name in admin.cookies)
    for url in pages:
        r = keep(admin.get(url, follow_redirects=False))
        check(f"{url}: ADMIN → 200", r.status_code == 200, str(r.status_code))
    check(
        "несуществующая компания в панели → 404",
        admin.get("/admin/companies/999999").status_code == 404
        and c.put(
            "/admin/businesses/999999/status", headers=HA, json={"status": "ACTIVE"}
        ).status_code
        == 404,
    )
    r = admin.get("/cabinet", follow_redirects=False)
    check(
        "ADMIN без своей компании из /cabinet попадает в /admin",
        r.status_code == 303 and r.headers["location"] == "/admin",
        r.headers.get("location", ""),
    )
    r = TestClient(app).get("/login", params={"next": "/admin/companies"})
    check(
        "форма входа принимает возврат в /admin/companies",
        'data-redirect="/admin/companies"' in r.text,
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 3. Список компаний: показатели, поиск, фильтры (раздел 15) ===")
    r = keep(c.get("/admin/businesses", headers=HA))
    page = r.json()
    items = {i["id"]: i for i in page["items"]}
    check("ADMIN видит все компании", page["total"] == 3 and set(items) == {biz_a, biz_b, biz_c})
    a = items[biz_a]
    check(
        "показатели компании: пользователи, сообщения, активность, дата регистрации",
        a["users_count"] == 2
        and a["messages_count"] >= 2
        and a["last_activity_at"] is not None
        and a["created_at"]
        and items[biz_c]["messages_count"] == 0
        and items[biz_c]["last_activity_at"] is None,
        f"users={a['users_count']} messages={a['messages_count']}",
    )
    check(
        "статус, тариф и trial в списке",
        a["status"] == "TRIAL" and a["plan"] == "TRIAL" and a["trial_expired"] is False,
    )
    check(
        "интеграции в списке: канал и статус",
        a["integrations"]
        and a["integrations"][0]["channel"] == "TELEGRAM"
        and a["integrations"][0]["status"] == "ACTIVE"
        and items[biz_c]["integrations"] == [],
    )
    secrets = (TOKEN_A, TOKEN_B, fake.secret(TOKEN_A), fake.secret(TOKEN_B), "enc:")
    check(
        "в списке нет токенов ботов, секретов webhook и credentials_ref",
        not any(s in r.text for s in secrets)
        and "credentials_ref" not in r.text
        and "webhook_secret_hash" not in r.text,
    )
    q = c.get("/admin/businesses", headers=HA, params={"q": "SCRIPT"}).json()
    check(
        "поиск по названию без учёта регистра (латиница; кириллицу регистронезависимо ищет PostgreSQL)",
        q["total"] == 1 and q["items"][0]["id"] == biz_c,
    )
    check(
        "спецсимволы поиска не работают как шаблон (%)",
        c.get("/admin/businesses", headers=HA, params={"q": "%"}).json()["total"] == 0,
    )
    check(
        "фильтр по статусу и тарифу",
        c.get("/admin/businesses", headers=HA, params={"status": "ACTIVE"}).json()["total"] == 0
        and c.get("/admin/businesses", headers=HA, params={"plan": "TRIAL"}).json()["total"] == 3
        and c.get("/admin/businesses", headers=HA, params={"plan": "PRO"}).json()["total"] == 0,
    )
    p1 = c.get("/admin/businesses", headers=HA, params={"limit": 2, "offset": 0}).json()
    p2 = c.get("/admin/businesses", headers=HA, params={"limit": 2, "offset": 2}).json()
    check(
        "пагинация: всего 3, страницы 2+1 без повторов",
        p1["total"] == 3
        and len(p1["items"]) == 2
        and len(p2["items"]) == 1
        and not {i["id"] for i in p1["items"]} & {i["id"] for i in p2["items"]},
    )
    check(
        "некорректные параметры → 422",
        c.get("/admin/businesses", headers=HA, params={"limit": 0}).status_code == 422
        and c.get("/admin/businesses", headers=HA, params={"limit": 101}).status_code == 422
        and c.get("/admin/businesses", headers=HA, params={"status": "BAD"}).status_code == 422
        and c.get("/admin/businesses", headers=HA, params={"offset": -1}).status_code == 422,
    )
    db_exec("DELETE FROM subscriptions WHERE business_id = ?", biz_c)
    check(
        "компания без подписки получает пробную и остаётся в списке",
        c.get("/admin/businesses", headers=HA).json()["total"] == 3
        and len(db_rows("SELECT id FROM subscriptions WHERE business_id = ?", biz_c)) == 1,
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 4. Статус компании: suspended останавливает AI, но не теряет сообщение ===")
    n_msgs = len(
        db_rows("SELECT id FROM messages WHERE business_id = ? AND sender_type = 'CUSTOMER'", biz_a)
    )
    sent_before = len(fake.sent(TOKEN_A))
    n_status_ev = len(events("ADMIN_BUSINESS_STATUS_CHANGED"))
    r = keep(c.put(f"/admin/businesses/{biz_a}/status", headers=HA, json={"status": "SUSPENDED"}))
    check("PUT status SUSPENDED → 200", r.status_code == 200 and r.json()["status"] == "SUSPENDED")
    ev = events("ADMIN_BUSINESS_STATUS_CHANGED")
    admin_id = db_rows("SELECT id FROM users WHERE email = 'admin@example.com'")[0]["id"]
    check(
        "аудит: кто, у какой компании, что было и что стало",
        len(ev) == n_status_ev + 1
        and ev[-1]["business_id"] == biz_a
        and ev[-1]["actor_user_id"] == admin_id
        and ev[-1]["old_status"] == "TRIAL"
        and ev[-1]["new_status"] == "SUSPENDED"
        and ev[-1]["level"] == "WARNING",
        str(ev[-1:]),
    )
    check(
        "повтор того же статуса не пишет лишнего события",
        c.put(
            f"/admin/businesses/{biz_a}/status", headers=HA, json={"status": "SUSPENDED"}
        ).status_code
        == 200
        and len(events("ADMIN_BUSINESS_STATUS_CHANGED")) == n_status_ev + 1,
    )
    say(4004, "Сколько стоит стрижка?", HOOK_A)
    conv = db_rows(
        "SELECT c.status, c.attention_reason FROM conversations c JOIN customers u "
        "ON u.id = c.customer_id WHERE u.external_id = '4004'"
    )[0]
    check(
        "приостановленная компания: сообщение сохранено (не теряется, §18)",
        len(
            db_rows(
                "SELECT id FROM messages WHERE business_id = ? AND sender_type = 'CUSTOMER'",
                biz_a,
            )
        )
        == n_msgs + 1,
    )
    check(
        # Проверка сайта 2026-10-02: ответить сотрудники не могут — ответа не обещаем.
        "…AI не запускается, клиенту — «не можем ответить в чате», без обещания ответа",
        len(fake.sent(TOKEN_A)) == sent_before + 1
        and "не можем ответить в этом чате" in fake.sent(TOKEN_A)[-1]["text"]
        and "сотрудник ответит" not in fake.sent(TOKEN_A)[-1]["text"].lower()
        and conv["status"] == "NEEDS_ATTENTION"
        and conv["attention_reason"] == "BUSINESS_SUSPENDED",
        str(dict(conv)),
    )
    check(
        "…событие MESSAGE_SKIPPED в логе",
        any(e["business_id"] == biz_a for e in events("MESSAGE_SKIPPED")),
    )
    conv_a = db_rows("SELECT id FROM conversations WHERE business_id = ? LIMIT 1", biz_a)[0]["id"]
    check(
        "менеджер приостановленной компании не может отправить ответ",
        c.post(
            f"/conversations/{conv_a}/reply", headers=H["manager1"], json={"text": "Здравствуйте"}
        ).status_code
        == 403,
    )
    cabinet = TestClient(app)
    cabinet.post("/auth/login", json={"email": "owner_a@example.com", "password": PWD})
    check(
        "кабинет владельца показывает, что компания приостановлена",
        "Компания приостановлена" in cabinet.get(f"/cabinet/{biz_a}").text,
    )
    detail = keep(admin.get(f"/admin/companies/{biz_a}"))
    check(
        "карточка компании: предупреждение и управление",
        "Компания приостановлена" in detail.text and 'data-url="/admin/businesses/' in detail.text,
    )
    c.put(f"/admin/businesses/{biz_a}/status", headers=HA, json={"status": "ACTIVE"})
    say(4005, "Сколько стоит стрижка?", HOOK_A)
    check(
        "после возврата в ACTIVE AI отвечает снова",
        len(fake.sent(TOKEN_A)) == sent_before + 2
        and fake.sent(TOKEN_A)[-1]["text"] != REPLY_STAFF_WILL_ANSWER
        and db_rows("SELECT status FROM businesses WHERE id = ?", biz_a)[0]["status"] == "ACTIVE",
    )
    check(
        "неверный статус → 422",
        c.put(
            f"/admin/businesses/{biz_a}/status", headers=HA, json={"status": "DELETED"}
        ).status_code
        == 422
        and c.put(f"/admin/businesses/{biz_a}/status", headers=HA, json={}).status_code == 422,
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 5. Тариф и пробный период (раздел 15) ===")
    r = keep(c.get("/admin/businesses", headers=HA, params={"q": "Лилия"}))
    b0 = r.json()["items"][0]
    exp0 = iso(b0["expires_at"])
    r = c.put(f"/admin/businesses/{biz_b}/plan", headers=HA, json={"extend_days": 7})
    check(
        "продление пробного периода на 7 дней",
        r.status_code == 200
        and abs((iso(r.json()["expires_at"]) - exp0).total_seconds() - 7 * 86400) < 5
        and r.json()["plan"] == "TRIAL",
        r.text[:120],
    )
    r = c.put(f"/admin/businesses/{biz_b}/plan", headers=HA, json={"expires_on": "2031-05-04"})
    check(
        "срок задаётся датой (до конца дня)",
        r.status_code == 200 and r.json()["expires_at"].startswith("2031-05-04T23:59:59"),
        r.text[:120],
    )
    r = c.put(f"/admin/businesses/{biz_b}/plan", headers=HA, json={"plan": "START"})
    check(
        "смена тарифа TRIAL → START: подписка активна, срока нет",
        r.status_code == 200
        and r.json()["plan"] == "START"
        and r.json()["expires_at"] is None
        and r.json()["subscription_status"] == "ACTIVE",
        r.text[:160],
    )
    ev = events("ADMIN_SUBSCRIPTION_CHANGED")
    check(
        "аудит смены тарифа: старое и новое значение, актор",
        ev[-1]["old"]["plan"] == "TRIAL"
        and ev[-1]["new"]["plan"] == "START"
        and ev[-1]["actor_user_id"] == admin_id
        and ev[-1]["business_id"] == biz_b,
        str(ev[-1:]),
    )
    r = c.put(f"/admin/businesses/{biz_b}/plan", headers=HA, json={"extend_days": 30})
    check(
        "продление платного тарифа без срока считается от сегодня",
        abs((iso(r.json()["expires_at"]) - datetime.now(UTC)).total_seconds() - 30 * 86400) < 60,
    )
    n_ev = len(events("ADMIN_SUBSCRIPTION_CHANGED"))
    c.put(f"/admin/businesses/{biz_b}/plan", headers=HA, json={"plan": "START"})
    check(
        "повтор без изменений не пишет события",
        len(events("ADMIN_SUBSCRIPTION_CHANGED")) == n_ev,
    )
    for body, name in (
        ({}, "пустое тело"),
        ({"plan": "GOLD"}, "неизвестный тариф"),
        ({"extend_days": 0}, "0 дней"),
        ({"extend_days": 366}, "больше года"),
        ({"expires_on": "2030-01-01", "extend_days": 5}, "и дата, и дни"),
        ({"expires_on": "не дата"}, "нечитаемая дата"),
    ):
        check(
            f"тариф: {name} → 422",
            c.put(f"/admin/businesses/{biz_b}/plan", headers=HA, json=body).status_code == 422,
        )
    r = c.put(f"/admin/businesses/{biz_b}/plan", headers=HA, json={"status": "CANCELED"})
    check(
        "подписку можно отменить",
        r.status_code == 200 and r.json()["subscription_status"] == "CANCELED",
    )
    c.put(f"/admin/businesses/{biz_b}/plan", headers=HA, json={"status": "ACTIVE"})
    db_exec(
        "UPDATE subscriptions SET expires_at = ? WHERE business_id = ?",
        "2020-01-01 00:00:00.000000",
        biz_c,
    )
    item_c = next(
        i for i in c.get("/admin/businesses", headers=HA).json()["items"] if i["id"] == biz_c
    )
    check("истёкший пробный период помечен в списке", item_c["trial_expired"] is True)
    r = c.put(f"/admin/businesses/{biz_c}/plan", headers=HA, json={"extend_days": 10})
    check(
        "продление истёкшего trial считается от сегодня",
        abs((iso(r.json()["expires_at"]) - datetime.now(UTC)).total_seconds() - 10 * 86400) < 60
        and r.json()["trial_expired"] is False,
    )
    db_exec(
        "UPDATE subscriptions SET expires_at = ? WHERE business_id = ?",
        "2020-01-01 00:00:00.000000",
        biz_c,
    )
    # возврат платной компании на пробный тариф — с новым пробным сроком
    r = c.put(f"/admin/businesses/{biz_b}/plan", headers=HA, json={"plan": "TRIAL"})
    check(
        "возврат на TRIAL даёт новый пробный срок",
        r.json()["plan"] == "TRIAL"
        and abs((iso(r.json()["expires_at"]) - datetime.now(UTC)).total_seconds() - 14 * 86400)
        < 60,
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 6. Метрики SaaS: компании, подписки, MRR (раздел 15) ===")
    m = keep(c.get("/admin/metrics", headers=HA)).json()
    check(
        "метрики: 3 компании, 1 активная, 2 на пробном, trial истёк у 1",
        m["companies_total"] == 3
        and m["companies_active"] == 1
        and m["companies_trial"] == 2
        and m["companies_suspended"] == 0
        and m["trials_expired"] == 1,
        str(m),
    )
    check(
        "пробные и без оплаты: MRR = 0, подписок 0",
        m["mrr_rub"] == 0 and m["paid_subscriptions"] == 0,
    )
    start, pro = settings.plan_price_start_rub, settings.plan_price_pro_rub
    c.put(f"/admin/businesses/{biz_b}/status", headers=HA, json={"status": "ACTIVE"})
    c.put(f"/admin/businesses/{biz_b}/plan", headers=HA, json={"plan": "PRO"})
    c.put(f"/admin/businesses/{biz_a}/plan", headers=HA, json={"plan": "START"})
    m = c.get("/admin/metrics", headers=HA).json()
    check(
        "MRR = START + PRO у активных компаний с платным тарифом",
        m["mrr_rub"] == start + pro and m["paid_subscriptions"] == 2 and m["companies_active"] == 2,
        str(m),
    )
    c.put(f"/admin/businesses/{biz_b}/status", headers=HA, json={"status": "SUSPENDED"})
    m = c.get("/admin/metrics", headers=HA).json()
    check(
        "приостановленная компания выпадает из MRR",
        m["mrr_rub"] == start and m["paid_subscriptions"] == 1 and m["companies_suspended"] == 1,
        str(m),
    )
    c.put(f"/admin/businesses/{biz_b}/status", headers=HA, json={"status": "ACTIVE"})
    c.put(f"/admin/businesses/{biz_a}/plan", headers=HA, json={"status": "CANCELED"})
    m = c.get("/admin/metrics", headers=HA).json()
    check(
        "отменённая подписка не входит в MRR",
        m["mrr_rub"] == pro and m["paid_subscriptions"] == 1,
        str(m),
    )
    c.put(f"/admin/businesses/{biz_a}/plan", headers=HA, json={"status": "ACTIVE"})
    check(
        "активность: сообщения и ошибки за сутки считаются",
        m["messages_24h"] == len(db_rows("SELECT id FROM messages"))
        and m["errors_24h"] >= 0
        and m["integrations_with_errors"] == 0,
    )
    db_exec(
        "UPDATE integrations SET status = 'ERROR', last_error = ? WHERE business_id = ?",
        f"Сбой: https://api.telegram.org/bot{TOKEN_B}/sendMessage",
        biz_b,
    )
    m = c.get("/admin/metrics", headers=HA).json()
    check("интеграция в статусе ERROR попала в метрики", m["integrations_with_errors"] == 1)
    listed = c.get("/admin/businesses", headers=HA, params={"q": "Лилия"})
    check(
        "текст ошибки интеграции виден, но токен бота в нём замаскирован",
        "Сбой" in listed.text and TOKEN_B not in listed.text and "***" in listed.text,
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 7. Системные логи: фильтры, маскирование секретов (разделы 16, 17) ===")
    db_exec(
        "INSERT INTO system_logs (business_id, level, event_type, message, metadata, created_at) "
        "VALUES (?, 'ERROR', 'AI_ERROR', ?, ?, ?)",
        biz_a,
        f"Сбой AI при обработке {EVIL_MESSAGE} токен {TOKEN_A}",
        json.dumps(
            {
                "bot_token": TOKEN_A,
                "Authorization": "Bearer abc.def.ghi",
                "nested": {"password": "hunter2", "list": [{"api_key": "sk-secret"}]},
                "note": "видно",
                "url": f"https://api.telegram.org/bot{TOKEN_A}/sendMessage",
                "attempt": 2,
            }
        ),
        "2026-09-10 12:00:00.000000",
    )
    db_exec(
        "INSERT INTO system_logs (business_id, level, event_type, message, metadata, created_at) "
        "VALUES (NULL, 'CRITICAL', 'UNHANDLED_ERROR', 'Платформенный сбой', NULL, ?)",
        "2026-09-11 12:00:00.000000",
    )
    r = keep(c.get("/admin/logs", headers=HA, params={"level": "ERROR"}))
    logs = r.json()
    err = logs["items"][0] if logs["items"] else {}
    check(
        "фильтр по уровню ERROR",
        logs["total"] >= 1 and all(i["level"] == "ERROR" for i in logs["items"]),
        str(logs["total"]),
    )
    check(
        "в логах нет токена бота, пароля, Authorization и api_key",
        not any(s in r.text for s in (TOKEN_A, "hunter2", "abc.def.ghi", "sk-secret")),
    )
    payload = err.get("payload") or {}
    check(
        "секретные поля payload заменены на ***, обычные видны",
        payload.get("bot_token") == "***"
        and payload.get("Authorization") == "***"
        and payload["nested"]["password"] == "***"
        and payload["nested"]["list"][0]["api_key"] == "***"
        and payload.get("note") == "видно"
        and payload.get("attempt") == 2
        and TOKEN_A not in payload.get("url", ""),
        str(payload),
    )
    check(
        "в логах указано название компании; платформенные события — без компании",
        err.get("business_name") == "Барбершоп «Бритва»"
        and c.get("/admin/logs", headers=HA, params={"level": "CRITICAL"}).json()["items"][0][
            "business_name"
        ]
        is None,
    )
    both = c.get("/admin/logs", headers=HA, params=[("level", "ERROR"), ("level", "CRITICAL")])
    check("несколько уровней сразу", both.json()["total"] == logs["total"] + 1)
    check(
        "фильтр по типу события и по компании",
        c.get("/admin/logs", headers=HA, params={"event_type": "AI_ERROR"}).json()["total"] == 1
        and all(
            i["business_id"] == biz_b
            for i in c.get("/admin/logs", headers=HA, params={"business_id": biz_b}).json()["items"]
        )
        and c.get("/admin/logs", headers=HA, params={"business_id": biz_b}).json()["total"] >= 1,
    )
    check(
        "фильтр по периоду",
        c.get(
            "/admin/logs",
            headers=HA,
            params={"date_from": "2026-09-10T00:00:00Z", "date_to": "2026-09-10T23:59:59Z"},
        ).json()["total"]
        == 1
        and c.get("/admin/logs", headers=HA, params={"date_from": "2026-09-12T00:00:00Z"}).json()[
            "total"
        ]
        >= 1,
    )
    check(
        "поиск по тексту; % не работает как шаблон",
        c.get("/admin/logs", headers=HA, params={"q": "Платформенный"}).json()["total"] == 1
        and c.get("/admin/logs", headers=HA, params={"q": "%"}).json()["total"] == 0,
    )
    all_logs = c.get("/admin/logs", headers=HA, params={"limit": 200}).json()
    p1 = c.get("/admin/logs", headers=HA, params={"limit": 3}).json()
    p2 = c.get("/admin/logs", headers=HA, params={"limit": 3, "offset": 3}).json()
    ids = [i["id"] for i in all_logs["items"]]
    keys = [(iso(i["created_at"]), i["id"]) for i in all_logs["items"]]
    check(
        "порядок: новые сверху; пагинация без повторов",
        keys == sorted(keys, reverse=True)
        and p1["total"] == all_logs["total"]
        and [i["id"] for i in p1["items"] + p2["items"]] == ids[:6],
    )
    check(
        "некорректные параметры логов → 422",
        c.get("/admin/logs", headers=HA, params={"limit": 201}).status_code == 422
        and c.get("/admin/logs", headers=HA, params={"level": "FATAL"}).status_code == 422
        and c.get("/admin/logs", headers=HA, params={"business_id": 0}).status_code == 422,
    )
    check(
        "цепочка сообщения видна в логах: получено → обработано/пропущено (§17)",
        {"WEBHOOK_RECEIVED", "MESSAGE_PROCESSED", "MESSAGE_SKIPPED"}
        <= {i["event_type"] for i in all_logs["items"]},
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 8. Страницы панели: содержимое, XSS, заголовки ===")
    ov = keep(admin.get("/admin"))
    check(
        "обзор: метрики, MRR и блоки со сбоями",
        "MRR" in ov.text
        and f"{start + pro:,}".replace(",", " ") in re.sub(r"\s", " ", ov.text)
        and "Свежие ошибки" in ov.text
        and "Интеграции со сбоем" in ov.text
        and "Пробный период истёк" in ov.text,
    )
    check(
        "BILLING_CONTACT не задан → на обзоре предупреждение админу",
        "Не задан контакт для оплаты" in ov.text,
    )
    check(
        "обзор: интеграция в сбое и компания с истёкшим trial видны",
        "Салон «Лилия»" in ov.text and "Сбой" in ov.text,
    )
    lst = keep(admin.get("/admin/companies"))
    check(
        "список компаний: названия, статусы, тарифы, фильтры",
        "Барбершоп «Бритва»" in lst.text
        and "Активна" in lst.text
        and "Старт" in lst.text
        and 'name="status"' in lst.text
        and 'name="plan"' in lst.text,
    )
    check(
        "фильтр на странице работает",
        "Барбершоп" in admin.get("/admin/companies", params={"q": "Бритв"}).text
        and "Лилия" not in admin.get("/admin/companies", params={"q": "Бритв"}).text,
    )
    det = keep(admin.get(f"/admin/companies/{biz_a}"))
    check(
        "карточка компании: сотрудники, интеграции, тариф",
        "manager1@example.com" in det.text
        and "owner_a@example.com" in det.text
        and "Telegram" in det.text
        and 'name="expires_on"' in det.text
        and "Продлить на 7 дн." in det.text,
    )
    ev_page = keep(admin.get("/admin/events", params=[("level", "ERROR"), ("level", "CRITICAL")]))
    check(
        "страница событий: ошибки, фильтры, данные события",
        "AI_ERROR" in ev_page.text
        and "Платформенный сбой" in ev_page.text
        and "Данные события" in ev_page.text
        and "видно" in ev_page.text,
    )
    check(
        "страница событий: секреты замаскированы",
        not any(s in ev_page.text for s in (TOKEN_A, "hunter2", "abc.def.ghi", "sk-secret")),
    )
    junk = [
        "/admin/companies?status=BAD&plan=X&offset=-5&q=" + "я" * 100,
        "/admin/events?level=BAD&business_id=abc&date_from=zzz&date_to=2026-13-40&offset=-9",
        "/admin/events?date_from=2026-09-20&date_to=2026-09-01",
    ]
    for url in junk:
        check(
            f"мусор в фильтрах не роняет страницу: {url[:38]}…", admin.get(url).status_code == 200
        )
    check(
        "пагинация страниц: ссылки «Дальше»/«Назад»",
        "offset=" in admin.get("/admin/events", params={"offset": 1}).text
        and admin.get("/admin/events").status_code == 200,
    )

    # XSS: чужой текст только через автоэкранирование
    evil_page = admin.get("/admin/companies").text + admin.get(f"/admin/companies/{biz_c}").text
    check(
        "название компании с <script> выводится экранированным",
        "&lt;script&gt;alert(1)&lt;/script&gt;" in evil_page
        and "<script>alert(1)" not in evil_page,
    )
    ev_text = admin.get("/admin/events", params={"level": "ERROR"}).text
    check(
        "текст события с <img onerror> выводится экранированным",
        "&lt;img" in ev_text and "<img src=x" not in ev_text,
    )
    bad_pages = "\n".join(html_pages)
    check(
        "нет inline-скриптов, inline-стилей и обработчиков on*=",
        not re.search(r"<script(?![^>]*\bsrc=)", bad_pages)
        and not re.search(r"\sstyle=", bad_pages)
        and not re.search(r"\son[a-z]+\s*=\s*[\"']", re.sub(r"&lt;.*?&gt;", "", bad_pages)),
    )
    check(
        "страницы не подключают внешних ресурсов",
        not re.search(r'(src|href)="https?://', "\n".join(html_pages)),
    )
    for url in ("/admin", "/admin/companies", "/admin/events"):
        h = admin.get(url).headers
        check(
            f"{url}: строгий CSP и запрет кэширования",
            "script-src 'self'" in h.get("content-security-policy", "")
            and h.get("cache-control") == "no-store",
        )
    check(
        "JSON API админки тоже не кэшируется",
        c.get("/admin/metrics", headers=HA).headers.get("cache-control") == "no-store",
    )
    check(
        "ни один ответ панели не содержит токены ботов и секреты webhook",
        not any(
            s in "\n".join(everything)
            for s in (TOKEN_A, fake.secret(TOKEN_A), fake.secret(TOKEN_B))
        )
        and TOKEN_B not in "\n".join(everything),
    )
    check(
        "кабинет: у ADMIN в компании есть ссылка на панель",
        "Панель администратора" in admin.get(f"/cabinet/{biz_a}").text,
    )
    check(
        "кабинет: у владельца ссылки на панель нет",
        "Панель администратора" not in cabinet.get(f"/cabinet/{biz_a}").text,
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 9. CSRF для cookie-сессии ADMIN (раздел 16) ===")
    put = f"/admin/businesses/{biz_c}/status"
    before = db_rows("SELECT status FROM businesses WHERE id = ?", biz_c)[0]["status"]
    r = admin.put(put, json={"status": "SUSPENDED"}, headers={"Origin": "https://evil.example"})
    check(
        "cookie + Origin чужого сайта → 403, статус не изменился",
        r.status_code == 403
        and db_rows("SELECT status FROM businesses WHERE id = ?", biz_c)[0]["status"] == before,
    )
    r = admin.put(put, json={"status": "ACTIVE"}, headers={"Origin": "http://testserver"})
    check("cookie + Origin нашего сайта → 200", r.status_code == 200)
    r = admin.put(
        f"/admin/businesses/{biz_c}/plan",
        json={"extend_days": 1},
        headers={"Origin": "null"},
    )
    check("cookie + Origin: null → 403", r.status_code == 403)
    r = c.put(put, json={"status": "TRIAL"}, headers={**HA, "Origin": "https://evil.example"})
    check("Bearer + чужой Origin → 200 (токен не уходит сам)", r.status_code == 200)

    # ----------------------------------------------------------------------- #
    print("\n=== 10. Изоляция компаний не нарушена (критерий 13) ===")
    check(
        "владелец A по-прежнему не видит компанию B",
        c.get(f"/businesses/{biz_b}", headers=H["owner_a"]).status_code == 404
        and c.get(f"/cabinet/{biz_b}", follow_redirects=False).status_code in (303, 401, 404)
        and cabinet.get(f"/cabinet/{biz_b}").status_code == 404,
    )
    check(
        "владелец не видит подписку и тариф через свои API",
        "subscription" not in c.get(f"/businesses/{biz_a}", headers=H["owner_a"]).text.lower()
        and "plan" not in c.get(f"/businesses/{biz_a}", headers=H["owner_a"]).json(),
    )
    check(
        "владелец не может сменить статус компании через /businesses",
        c.put(f"/businesses/{biz_a}", headers=H["owner_a"], json={"status": "ACTIVE"}).status_code
        in (200, 400, 422)
        and db_rows("SELECT status FROM businesses WHERE id = ?", biz_a)[0]["status"] == "ACTIVE",
    )
    admin_events = [
        e
        for t in ("ADMIN_BUSINESS_STATUS_CHANGED", "ADMIN_SUBSCRIPTION_CHANGED")
        for e in events(t)
    ]
    check(
        "все действия ADMIN записаны в аудит с актором",
        len(admin_events) >= 10 and all(e["actor_user_id"] == admin_id for e in admin_events),
        str(len(admin_events)),
    )

print("\n" + "=" * 70)
print(f"ИТОГО: пройдено {len(PASSED)}, провалено {len(FAILED)}")
if FAILED:
    print("Провалены:")
    for name in FAILED:
        print("  -", name)
    sys.exit(1)
