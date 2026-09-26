"""
Проверочный скрипт этапа 9 — второй канал VK и общий rate limit (НЕ часть приложения, можно удалить).

Покрывает разделы 1, 11, 16, 18, 22 ТЗ: канал VK через общий контракт integrations/base.py
(ядро без правок под канал), разбор событий Callback API, клиент VK API (ключ не в URL и логах,
повторы, «клиент запретил сообщения»), подключение сообщества (сначала сохранить секрет и строку
подтверждения, потом зарегистрировать сервер), webhook /webhooks/vk (подтверждение, secret, дубли,
беседы, message_deny/allow, сбой отправки без потери сообщения), изоляцию компаний и каналов,
кабинет (блок VK, onboarding, подпись канала) и rate limit на общей БД.
VK подменяется httpx.MockTransport, AI офлайн (stub).

Запуск:  python smoke_test_stage9.py
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import sys
from urllib.parse import parse_qs

BASE = pathlib.Path(__file__).resolve().parent
DB = BASE / "test_smoke_stage9.db"
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
    SYSTEM_LOGS_PURGE_INTERVAL_HOURS="0",
    AUTH_RATE_LIMIT_ATTEMPTS="1000",
    WEBHOOK_RATE_LIMIT_ATTEMPTS="1000",
)
sys.path.insert(0, str(BASE))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from config import settings  # noqa: E402
from integrations import base as channel_base  # noqa: E402
from integrations import vk  # noqa: E402
from integrations.base import ChannelSendError  # noqa: E402
from integrations.telegram import TelegramClient  # noqa: E402
from integrations.vk import VkClient  # noqa: E402
from main import app  # noqa: E402
from services import integration_service, rate_limit_service  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, extra: str = "") -> None:
    (PASSED if condition else FAILED).append(f"{name} {extra}".strip())
    print(("  OK  " if condition else "FAIL  ") + name + (f"  [{extra}]" if extra else ""))


def db_rows(sql: str, *params) -> list[sqlite3.Row]:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Фейковый VK API
# --------------------------------------------------------------------------- #
TOKEN_A = "vk1.a." + "A" * 60
TOKEN_B = "vk1.a." + "B" * 60
TOKEN_BAD = "vk1.a." + "X" * 60
GROUPS = {TOKEN_A: (1001, "Барбершоп «Бритва»"), TOKEN_B: (2002, "Салон «Лилия»")}


class FakeVk:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str], str]] = []  # token, method, params, url
        self.fail_send: list[int] = []  # коды ошибок для следующих messages.send
        self.at_add_server: dict | None = None
        self._msg = 500
        self._server = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        match = re.match(r"^/method/([\w.]+)$", request.url.path)
        assert match, request.url.path
        method = match.group(1)
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ")
        params = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append((token, method, params, str(request.url)))
        if token not in GROUPS:
            return httpx.Response(
                200, json={"error": {"error_code": 5, "error_msg": "User authorization failed"}}
            )
        group_id, name = GROUPS[token]
        if method == "groups.getById":
            return httpx.Response(
                200, json={"response": {"groups": [{"id": group_id, "name": name}], "profiles": []}}
            )
        if method == "groups.getCallbackConfirmationCode":
            return httpx.Response(200, json={"response": {"code": f"conf{group_id}"}})
        if method == "groups.addCallbackServer":
            self.at_add_server = {
                "rows": [
                    dict(r)
                    for r in db_rows(
                        "SELECT channel_settings, webhook_secret_hash FROM integrations "
                        "WHERE channel = 'VK' AND external_account_id = ?",
                        str(group_id),
                    )
                ],
                "secret": params.get("secret_key"),
            }
            self._server += 1
            return httpx.Response(200, json={"response": {"server_id": self._server}})
        if method in ("groups.setCallbackSettings", "groups.deleteCallbackServer"):
            return httpx.Response(200, json={"response": 1})
        if method == "messages.send":
            if self.fail_send:
                code = self.fail_send.pop(0)
                return httpx.Response(
                    200, json={"error": {"error_code": code, "error_msg": f"err {code}"}}
                )
            self._msg += 1
            return httpx.Response(200, json={"response": self._msg})
        return httpx.Response(200, json={"error": {"error_code": 3, "error_msg": "Unknown method"}})

    def sent(self, token: str, peer: int | None = None) -> list[dict[str, str]]:
        return [
            p
            for t, m, p, _ in self.calls
            if m == "messages.send"
            and t == token
            and (peer is None or p.get("peer_id") == str(peer))
        ]

    def methods(self, token: str) -> list[str]:
        return [m for t, m, _, _ in self.calls if t == token]


fake = FakeVk()
integration_service.build_vk_client = lambda token: VkClient(
    token,
    base_url="https://api.vk.test",
    transport=httpx.MockTransport(fake.handler),
    sleep=lambda _s: None,
)
# Telegram — только для проверки «два канала у одной компании».
TG_TOKEN = "111111:" + "T" * 35
tg_sent: list[dict] = []


def tg_handler(request: httpx.Request) -> httpx.Response:
    method = request.url.path.rsplit("/", 1)[-1]
    body = json.loads(request.content) if request.content else {}
    if method == "getMe":
        return httpx.Response(200, json={"ok": True, "result": {"id": 111111, "username": "tgbot"}})
    if method == "sendMessage":
        tg_sent.append(body)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(tg_sent)}})
    if method == "setWebhook":
        tg_handler.secret = body["secret_token"]  # type: ignore[attr-defined]
    return httpx.Response(200, json={"ok": True, "result": True})


integration_service.build_telegram_client = lambda token: TelegramClient(
    token,
    base_url="https://api.telegram.test",
    transport=httpx.MockTransport(tg_handler),
    sleep=lambda _s: None,
)

_n = {"event": 0, "msg": 0}


def msg_event(
    group_id: int,
    secret: str | None,
    peer: int,
    text: str,
    *,
    msg_id: int | None = None,
    from_id: int | None = None,
) -> dict:
    _n["event"] += 1
    if msg_id is None:
        _n["msg"] += 1
        msg_id = _n["msg"]
    event = {
        "type": "message_new",
        "event_id": f"ev{_n['event']}",
        "v": "5.199",
        "group_id": group_id,
        "object": {
            "message": {
                "id": msg_id,
                "peer_id": peer,
                "from_id": from_id if from_id is not None else peer,
                "text": text,
                "conversation_message_id": msg_id,
                "date": 1700000000,
            },
            "client_info": {},
        },
    }
    if secret is not None:
        event["secret"] = secret
    return event


# =========================================================================== #
print("\n=== 1. Ядро не зависит от канала ===")
core = (BASE / "services" / "message_service.py").read_text(encoding="utf-8")
check(
    "message_service импортирует только integrations.base",
    "integrations.telegram" not in core and "integrations.vk" not in core,
)
check(
    "telegram.py реэкспортирует общие типы",
    __import__("integrations.telegram", fromlist=["x"]).IncomingMessage
    is channel_base.IncomingMessage,
)
check(
    "split_text общий для каналов",
    channel_base.split_text("a " * 10, 6) == vk.split_text("a " * 10, 6),
)

# =========================================================================== #
print("\n=== 2. Разбор событий Callback API ===")
ev = vk.parse_event({"type": "confirmation", "group_id": 1001})
check("confirmation распознан", ev.kind == "confirmation" and ev.group_id == "1001")
ev = vk.parse_event(msg_event(1001, "s", 42, "Сколько стоит стрижка?", msg_id=77))
check(
    "message_new → нейтральное IncomingMessage",
    ev.kind == "message"
    and ev.message is not None
    and ev.message.channel == "VK"
    and ev.message.external_chat_id == "42"
    and ev.message.external_message_id == "77"
    and ev.secret == "s",
)
att = msg_event(1001, "s", 42, "", msg_id=78)
att["object"]["message"]["attachments"] = [{"type": "photo"}]
ev = vk.parse_event(att)
check(
    "вложение без текста → attachment",
    ev.kind == "message" and ev.message is not None and ev.message.content_type == "attachment",
)
check(
    "беседа (peer ≥ 2e9) не обрабатывается",
    vk.parse_event(msg_event(1001, "s", 2_000_000_005, "всем привет", from_id=42)).reason
    == "not_private_chat",
)
check(
    "сообщение от сообщества не обрабатывается",
    vk.parse_event(msg_event(1001, "s", -5, "x", from_id=-5)).kind == "ignored",
)
ev = vk.parse_event(
    {"type": "message_deny", "group_id": 1001, "secret": "s", "object": {"user_id": 42}}
)
check("message_deny → deny с user_id", ev.kind == "deny" and ev.user_id == "42")
check(
    "message_allow → allow",
    vk.parse_event({"type": "message_allow", "group_id": 1, "object": {"user_id": 1}}).kind
    == "allow",
)
check(
    "прочие события → ignored",
    vk.parse_event({"type": "wall_post_new", "group_id": 1}).kind == "ignored",
)
check("мусор не роняет разбор", vk.parse_event({}).kind == "ignored")

# =========================================================================== #
print("\n=== 3. Клиент VK API ===")
client = integration_service.build_vk_client(TOKEN_A)
client.send_message("42", "Привет")
token, method, params, url = fake.calls[-1]
check("ключ в заголовке Authorization, не в URL", token == TOKEN_A and TOKEN_A not in url)
check(
    "версия API 5.199 и random_id ≠ 0",
    params.get("v") == "5.199" and int(params.get("random_id", "0")) > 0,
)
before = len(fake.sent(TOKEN_A))
client.send_message("42", ("слово " * 1000).strip())
check(
    "длинный текст режется на части ≤ 4000",
    len(fake.sent(TOKEN_A)) - before == 2
    and all(len(p["message"]) <= 4000 for p in fake.sent(TOKEN_A)[before:]),
)
fake.fail_send = [6, 10]
client.send_message("42", "после повторов")
check(
    "ошибки 6/10 повторяются, затем успех",
    fake.sent(TOKEN_A)[-1]["message"] == "после повторов" and not fake.fail_send,
)
fake.fail_send = [901]
try:
    client.send_message("42", "x")
    check("901 → клиент запретил сообщения", False)
except ChannelSendError as exc:
    check("901 → клиент запретил сообщения", exc.blocked_by_user and not exc.retryable)
fake.fail_send = []
try:
    integration_service.build_vk_client(TOKEN_BAD).get_group()
    check("неверный ключ → понятная ошибка без ключа", False)
except ChannelSendError as exc:
    check(
        "неверный ключ → понятная ошибка без ключа",
        "недействителен" in str(exc) and TOKEN_BAD not in str(exc),
    )


def boom(_req: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError(
        "connect failed to https://api.vk.test/method/x?access_token=" + TOKEN_A
    )


try:
    VkClient(
        TOKEN_A,
        base_url="https://api.vk.test",
        transport=httpx.MockTransport(boom),
        sleep=lambda _s: None,
        max_retries=1,
    ).send_message("1", "x")
except ChannelSendError as exc:
    check(
        "сетевая ошибка: текст без ключа и URL",
        TOKEN_A not in str(exc) and "api.vk" not in str(exc) and exc.retryable,
    )
check("repr клиента не раскрывает ключ", TOKEN_A not in repr(client))

# =========================================================================== #
PWD = "Str0ng-Pass-1"
with TestClient(app) as c:
    print("\n=== 4. Подключение сообщества ===")
    for name in ("owner_a", "manager_a", "owner_b"):
        c.post("/auth/register", json={"email": f"{name}@example.com", "password": PWD})

    def bearer(email: str) -> dict:
        tok = c.post("/auth/login", json={"email": email, "password": PWD}).json()["access_token"]
        c.cookies.clear()
        return {"Authorization": f"Bearer {tok}"}

    H = {n: bearer(f"{n}@example.com") for n in ("owner_a", "manager_a", "owner_b")}
    biz_a = c.post("/businesses", headers=H["owner_a"], json={"name": "Бритва"}).json()["id"]
    biz_b = c.post("/businesses", headers=H["owner_b"], json={"name": "Лилия"}).json()["id"]
    c.post(
        f"/businesses/{biz_a}/members",
        headers=H["owner_a"],
        json={"email": "manager_a@example.com", "role": "MANAGER"},
    )
    c.post(
        f"/businesses/{biz_a}/services",
        headers=H["owner_a"],
        json={"name": "Стрижка", "price": "1500"},
    )
    c.post(
        f"/businesses/{biz_b}/services",
        headers=H["owner_b"],
        json={"name": "Маникюр", "price": "1200"},
    )

    r = c.post(
        f"/businesses/{biz_a}/integrations/vk",
        headers=H["manager_a"],
        json={"access_token": TOKEN_A},
    )
    check("менеджер не может подключить канал", r.status_code in (403, 404), str(r.status_code))
    r = c.post(
        f"/businesses/{biz_a}/integrations/vk",
        headers=H["owner_a"],
        json={"access_token": "короткий ключ"},
    )
    check(
        "неверный формат ключа → 422 без эха ключа",
        r.status_code == 422 and "короткий" not in r.text,
    )
    r = c.post(
        f"/businesses/{biz_a}/integrations/vk",
        headers=H["owner_a"],
        json={"access_token": TOKEN_BAD},
    )
    check(
        "ключ отклонён VK → 400",
        r.status_code == 400 and TOKEN_BAD not in r.text,
        str(r.status_code),
    )

    r = c.post(
        f"/businesses/{biz_a}/integrations/vk", headers=H["owner_a"], json={"access_token": TOKEN_A}
    )
    body = r.json()
    check(
        "сообщество подключено (201)",
        r.status_code == 201 and body["channel"] == "VK" and body["status"] == "ACTIVE",
        str(r.status_code),
    )
    check("ответ без ключа и секрета", TOKEN_A not in r.text and "secret" not in r.text.lower())
    check(
        "webhook_url канала — /webhooks/vk",
        body["webhook_url"] == "https://leadpilot.test/webhooks/vk",
    )
    methods = fake.methods(TOKEN_A)
    check(
        "порядок вызовов: getById → код → addCallbackServer → setCallbackSettings",
        methods[-4:]
        == [
            "groups.getById",
            "groups.getCallbackConfirmationCode",
            "groups.addCallbackServer",
            "groups.setCallbackSettings",
        ],
        str(methods[-4:]),
    )
    add_params = next(
        p for t, m, p, _ in reversed(fake.calls) if m == "groups.addCallbackServer" and t == TOKEN_A
    )
    check(
        "сервер: наш URL, title ≤ 14, secret ≤ 50",
        add_params["url"] == "https://leadpilot.test/webhooks/vk"
        and len(add_params["title"]) <= 14
        and 0 < len(add_params["secret_key"]) <= 50,
    )
    snap = fake.at_add_server or {}
    stored_before = json.loads((snap.get("rows") or [{}])[0].get("channel_settings") or "{}")
    SECRET_A = snap.get("secret") or ""
    check(
        "к моменту addCallbackServer строка подтверждения и секрет уже в БД",
        stored_before.get("confirmation_code") == "conf1001"
        and (snap.get("rows") or [{}])[0].get("webhook_secret_hash")
        == hashlib.sha256(SECRET_A.encode()).hexdigest(),
    )
    settings_params = next(
        p for t, m, p, _ in reversed(fake.calls) if m == "groups.setCallbackSettings"
    )
    check(
        "подписка на message_new/allow/deny, API 5.199",
        settings_params.get("message_new") == "1"
        and settings_params.get("message_deny") == "1"
        and settings_params.get("api_version") == "5.199",
    )
    row = dict(
        db_rows(
            "SELECT credentials_ref, channel_settings, external_account_name FROM integrations WHERE channel='VK' AND business_id=?",
            biz_a,
        )[0]
    )
    check(
        "ключ хранится шифртекстом",
        row["credentials_ref"].startswith("enc:") and TOKEN_A not in row["credentials_ref"],
    )
    check("server_id сохранён", json.loads(row["channel_settings"]).get("server_id") == "1")
    r = c.post(
        f"/businesses/{biz_b}/integrations/vk", headers=H["owner_b"], json={"access_token": TOKEN_A}
    )
    check("то же сообщество к другой компании → 409", r.status_code == 409, str(r.status_code))
    c.post(
        f"/businesses/{biz_b}/integrations/vk", headers=H["owner_b"], json={"access_token": TOKEN_B}
    )
    SECRET_B = (fake.at_add_server or {}).get("secret") or ""

    # ------------------------------------------------------------------- #
    print("\n=== 5. Webhook /webhooks/vk ===")
    r = c.post("/webhooks/vk", json={"type": "confirmation", "group_id": 1001})
    check("confirmation → строка подтверждения", r.status_code == 200 and r.text == "conf1001")
    r = c.post("/webhooks/vk", json={"type": "confirmation", "group_id": 999})
    check("confirmation неизвестного сообщества → 404", r.status_code == 404)
    r = c.post("/webhooks/vk", json=msg_event(1001, "неверный", 42, "привет"))
    check("неверный secret → 401", r.status_code == 401)
    r = c.post("/webhooks/vk", json=msg_event(1001, None, 42, "привет"))
    check("без secret → 401", r.status_code == 401)
    r = c.post("/webhooks/vk", json=msg_event(1001, SECRET_B, 42, "привет"))
    check("secret чужого сообщества → 401", r.status_code == 401)
    rejected = db_rows("SELECT COUNT(*) AS n FROM system_logs WHERE event_type='WEBHOOK_REJECTED'")[
        0
    ]["n"]
    check("отказы записаны в журнал", rejected >= 3)

    before = len(fake.sent(TOKEN_A, 42))
    event = msg_event(1001, SECRET_A, 42, "Сколько стоит стрижка?")
    r = c.post("/webhooks/vk", json=event)
    check("сообщение принято: ответ «ok»", r.status_code == 200 and r.text == "ok")
    check("AI ответил клиенту через messages.send", len(fake.sent(TOKEN_A, 42)) == before + 1)
    msg = db_rows(
        "SELECT m.external_message_id, cu.channel, cu.external_id, cu.business_id FROM messages m "
        "JOIN conversations cv ON cv.id = m.conversation_id JOIN customers cu ON cu.id = cv.customer_id "
        "WHERE cu.channel='VK' AND m.sender_type='CUSTOMER' ORDER BY m.id"
    )
    check(
        "сообщение сохранено с каналом VK и peer_id",
        len(msg) == 1 and msg[0]["external_id"] == "42" and msg[0]["business_id"] == biz_a,
    )
    r = c.post("/webhooks/vk", json=event)
    check(
        "повтор того же события → «ok» без второго ответа",
        r.text == "ok" and len(fake.sent(TOKEN_A, 42)) == before + 1,
    )
    r = c.post(
        "/webhooks/vk", json=msg_event(1001, SECRET_A, 2_000_000_003, "в беседе", from_id=42)
    )
    ignored = db_rows("SELECT COUNT(*) AS n FROM system_logs WHERE event_type='WEBHOOK_IGNORED'")[
        0
    ]["n"]
    check("беседа → «ok» и запись «не обрабатывается»", r.text == "ok" and ignored >= 1)

    r = c.post(
        "/webhooks/vk",
        json={
            "type": "message_deny",
            "group_id": 1001,
            "secret": SECRET_A,
            "object": {"user_id": 42},
        },
    )
    blocked = db_rows(
        "SELECT channel_blocked FROM customers WHERE channel='VK' AND external_id='42'"
    )[0]["channel_blocked"]
    check("message_deny → клиент помечен «запретил сообщения»", r.text == "ok" and blocked == 1)
    r = c.post(
        "/webhooks/vk",
        json={
            "type": "message_allow",
            "group_id": 1001,
            "secret": SECRET_A,
            "object": {"user_id": 42},
        },
    )
    blocked = db_rows(
        "SELECT channel_blocked FROM customers WHERE channel='VK' AND external_id='42'"
    )[0]["channel_blocked"]
    check("message_allow → запрет снят", r.text == "ok" and blocked == 0)
    events = db_rows(
        "SELECT event_type FROM system_logs WHERE event_type LIKE 'CUSTOMER_CHANNEL_%'"
    )
    check(
        "запрет и разрешение записаны в журнал",
        {e["event_type"] for e in events}
        == {"CUSTOMER_CHANNEL_BLOCKED", "CUSTOMER_CHANNEL_UNBLOCKED"},
    )

    fake.fail_send = [10, 10, 10]
    r = c.post("/webhooks/vk", json=msg_event(1001, SECRET_A, 43, "Сколько стоит стрижка?"))
    fake.fail_send = []
    saved = db_rows(
        "SELECT m.id FROM messages m JOIN conversations cv ON cv.id=m.conversation_id "
        "JOIN customers cu ON cu.id=cv.customer_id WHERE cu.external_id='43' AND m.sender_type='CUSTOMER'"
    )
    failed = db_rows(
        "SELECT delivery_status FROM messages m JOIN conversations cv ON cv.id=m.conversation_id "
        "JOIN customers cu ON cu.id=cv.customer_id WHERE cu.external_id='43' AND m.sender_type='AI'"
    )
    check(
        "сбой VK при отправке: входящее сохранено, «ok» для VK", r.text == "ok" and len(saved) == 1
    )
    check(
        "ответ помечен как недоставленный (повтор позже)",
        bool(failed) and failed[0]["delivery_status"] in ("FAILED", "PENDING"),
    )

    # ------------------------------------------------------------------- #
    print("\n=== 6. Изоляция компаний и каналов ===")
    c.post("/webhooks/vk", json=msg_event(2002, SECRET_B, 42, "Сколько стоит маникюр?"))
    cust = db_rows(
        "SELECT business_id FROM customers WHERE channel='VK' AND external_id='42' ORDER BY business_id"
    )
    check(
        "один и тот же пользователь VK — разные клиенты у разных компаний",
        [r["business_id"] for r in cust] == sorted([biz_a, biz_b]),
    )
    check("ответ компании B ушёл от её сообщества", len(fake.sent(TOKEN_B, 42)) == 1)
    vk_conv_a = db_rows(
        "SELECT cv.id FROM conversations cv JOIN customers cu ON cu.id = cv.customer_id "
        "WHERE cv.business_id = ? AND cu.channel = 'VK' AND cu.external_id = '42'",
        biz_a,
    )[0]["id"]
    check(
        "владелец B не видит диалог VK компании A (404)",
        c.get(f"/conversations/{vk_conv_a}", headers=H["owner_b"]).status_code == 404,
    )
    r = c.post(
        f"/conversations/{vk_conv_a}/reply",
        headers=H["manager_a"],
        json={"text": "Ждём вас в 18:00!"},
    )
    check(
        "ручной ответ менеджера уходит в VK",
        r.status_code == 201 and fake.sent(TOKEN_A, 42)[-1]["message"] == "Ждём вас в 18:00!",
        str(r.status_code),
    )

    c.post(
        f"/businesses/{biz_a}/integrations/telegram",
        headers=H["owner_a"],
        json={"bot_token": TG_TOKEN},
    )
    tg_secret = getattr(tg_handler, "secret", "")
    c.post(
        "/webhooks/telegram",
        headers={"X-Telegram-Bot-Api-Secret-Token": tg_secret},
        json={
            "update_id": 1,
            "message": {
                "message_id": 1,
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "is_bot": False, "first_name": "Иван"},
                "text": "Сколько стоит стрижка?",
            },
        },
    )
    both = db_rows(
        "SELECT channel FROM customers WHERE business_id=? AND external_id='42' ORDER BY channel",
        biz_a,
    )
    check(
        "id 42 в Telegram и VK — разные клиенты одной компании",
        [r["channel"] for r in both] == ["TELEGRAM", "VK"],
    )
    check(
        "ответ Telegram-клиенту ушёл в Telegram, не в VK",
        len(tg_sent) == 1 and tg_sent[0]["chat_id"] in (42, "42"),
    )

    # ------------------------------------------------------------------- #
    print("\n=== 7. Кабинет ===")
    c.cookies.clear()
    c.post("/auth/login", json={"email": "owner_a@example.com", "password": PWD})
    html = c.get(f"/cabinet/{biz_a}/settings").text
    check(
        "в настройках блок VK: сообщество подключено",
        "<h2>VK</h2>" in html and "Бритва" in html and "Отключить сообщество" in html,
    )
    check("в HTML нет ключа и секрета", TOKEN_A not in html and SECRET_A not in html)
    html = c.get(f"/cabinet/{biz_a}/customers").text
    check(
        "в списке клиентов подпись канала VK",
        ">VK<" in html.replace("\n", "").replace(" ", "") or "VK" in html,
    )
    c.cookies.clear()
    c.post("/auth/login", json={"email": "owner_b@example.com", "password": PWD})
    html = c.get(f"/cabinet/{biz_b}").text
    check(
        "onboarding: шаг «канал» выполнен при одном VK",
        "Подключите Telegram-бота или сообщество VK" in html
        and "К настройкам"
        not in html.split("Подключите Telegram-бота или сообщество VK")[1].split("</li>")[0],
    )
    c.cookies.clear()

    # ------------------------------------------------------------------- #
    print("\n=== 8. Отключение ===")
    r = c.delete(f"/businesses/{biz_a}/integrations/vk", headers=H["owner_a"])
    check(
        "отключение → 204 и deleteCallbackServer",
        r.status_code == 204 and fake.methods(TOKEN_A)[-1] == "groups.deleteCallbackServer",
    )
    r = c.post("/webhooks/vk", json=msg_event(1001, SECRET_A, 42, "ещё вопрос"))
    check("после отключения события отклоняются", r.status_code == 401)
    r = c.post("/webhooks/vk", json={"type": "confirmation", "group_id": 1001})
    check("после отключения строка подтверждения не выдаётся", r.status_code == 404)
    r = c.post(
        f"/businesses/{biz_a}/integrations/vk", headers=H["owner_a"], json={"access_token": TOKEN_A}
    )
    check("повторное подключение работает", r.status_code == 201)

    logs = " ".join(
        r["message"] + " " + (r["metadata"] or "")
        for r in db_rows("SELECT message, metadata FROM system_logs")
    )
    check(
        "в system_logs нет ключей и секретов VK",
        TOKEN_A not in logs
        and TOKEN_B not in logs
        and SECRET_A not in logs
        and SECRET_B not in logs,
    )

    # ------------------------------------------------------------------- #
    print("\n=== 9. Rate limit в общей БД ===")
    rate_limit_service.reset()
    results = [
        rate_limit_service.hit("test", "1.2.3.4", limit=3, window_seconds=60) for _ in range(5)
    ]
    check(
        "лимит 3 в окне: 3 разрешены, дальше отказ",
        results == [True, True, True, False, False],
        str(results),
    )
    row = db_rows("SELECT count FROM rate_limit_counters WHERE key='test:1.2.3.4'")
    check("счётчик хранится в таблице rate_limit_counters", bool(row) and row[0]["count"] == 5)
    check(
        "другой ключ — свой счётчик",
        rate_limit_service.hit("test", "5.6.7.8", limit=3, window_seconds=60),
    )
    real_time = rate_limit_service.time.time
    rate_limit_service.time.time = lambda: real_time() + 120  # type: ignore[assignment]
    try:
        check(
            "новое окно начинает счёт заново",
            rate_limit_service.hit("test", "1.2.3.4", limit=3, window_seconds=60),
        )
    finally:
        rate_limit_service.time.time = real_time  # type: ignore[assignment]
    conn = sqlite3.connect(DB)
    conn.execute(
        "INSERT INTO rate_limit_counters(key, window_start, count) VALUES ('old:x', 1000, 9)"
    )
    conn.commit()
    conn.close()
    check(
        "очистка удаляет закрытые окна",
        rate_limit_service.purge_expired() >= 1
        and not db_rows("SELECT 1 FROM rate_limit_counters WHERE key='old:x'"),
    )

    real_increment = rate_limit_service._increment

    def broken(*_a, **_k):
        raise OperationalError("SELECT", {}, Exception("db down"))

    rate_limit_service._increment = broken  # type: ignore[assignment]
    try:
        check(
            "БД недоступна → запрос пропускается (fail-open)",
            rate_limit_service.hit("test", "9.9.9.9", limit=1, window_seconds=60),
        )
    finally:
        rate_limit_service._increment = real_increment  # type: ignore[assignment]

    rate_limit_service.reset()
    saved_attempts = settings.auth_rate_limit_attempts
    settings.auth_rate_limit_attempts = 3
    try:
        codes = [
            c.post("/auth/login", json={"email": "nobody@example.com", "password": "x"}).status_code
            for _ in range(5)
        ]
    finally:
        settings.auth_rate_limit_attempts = saved_attempts
    check(
        "вход: после 3 попыток — 429 (счётчик в БД)",
        codes[:3] == [401, 401, 401] and codes[3:] == [429, 429],
        str(codes),
    )

with contextlib.suppress(PermissionError):
    DB.unlink(missing_ok=True)

print(f"\nИТОГО: пройдено {len(PASSED)}, провалено {len(FAILED)}")
if FAILED:
    print("Провалены:")
    for name in FAILED:
        print("  -", name)
    sys.exit(1)
