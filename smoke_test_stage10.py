"""
Проверочный скрипт: мастера, расписание, записи и запись клиентов через AI
(вне ТЗ, §22 «автоматическая запись»; НЕ часть приложения, можно удалить).

Часть 1 — роль MASTER и доступ, смены по датам, свободное время (длительность услуги,
шаг сетки, услуги мастера, часовой пояс, прошлое), атомарная бронь (гонка двух броней),
решения по записи (подтвердить / отклонить / отменить), изоляция компаний.
Telegram подменяется httpx.MockTransport, AI офлайн (stub).

Запуск:  python smoke_test_stage10.py
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import re
import sqlite3
import sys
import threading
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

BASE = pathlib.Path(__file__).resolve().parent
DB = BASE / "test_smoke_stage10.db"
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
    BOOTSTRAP_ADMIN_EMAIL="admin@example.com",
    BOOTSTRAP_ADMIN_PASSWORD="Adm1n-Pass-123!",
)
sys.path.insert(0, str(BASE))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from database import SessionLocal  # noqa: E402
from integrations.telegram import TelegramClient  # noqa: E402
from main import app  # noqa: E402
from models import BookingSource, Business, Master, Service  # noqa: E402
from services import booking_service, integration_service  # noqa: E402

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
# Фейковый Telegram (для сообщений клиенту о решении по брони)
# --------------------------------------------------------------------------- #
TOKEN_A = "111111:" + "A" * 35


class FakeTelegram:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._id = 100

    def handler(self, request: httpx.Request) -> httpx.Response:
        match = re.match(r"^/bot([^/]+)/(\w+)$", request.url.path)
        assert match
        token, method = match.group(1), match.group(2)
        body = json.loads(request.content) if request.content else {}
        self.calls.append((token, method, body))
        if method == "getMe":
            return httpx.Response(
                200, json={"ok": True, "result": {"id": 111111, "username": "shopbot"}}
            )
        if method == "sendMessage":
            self._id += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self._id}})
        return httpx.Response(200, json={"ok": True, "result": True})

    def sent(self, chat: int | None = None) -> list[dict]:
        return [
            b
            for _t, m, b in self.calls
            if m == "sendMessage" and (chat is None or str(b.get("chat_id")) == str(chat))
        ]

    def secret(self) -> str:
        return [b["secret_token"] for _t, m, b in self.calls if m == "setWebhook"][-1]


fake = FakeTelegram()
integration_service.build_telegram_client = lambda token: TelegramClient(
    token,
    base_url="https://api.telegram.test",
    transport=httpx.MockTransport(fake.handler),
    sleep=lambda _s: None,
)

MSK = ZoneInfo("Europe/Moscow")
TODAY = datetime.now(MSK).date()
D1 = TODAY + timedelta(days=2)  # «послезавтра» — всегда в будущем
D2 = TODAY + timedelta(days=3)
PWD = "Str0ng-Pass-1"


def iso(d: date) -> str:
    return d.isoformat()


with TestClient(app) as c:
    # ----------------------------------------------------------------------- #
    print("\n=== 0. Подготовка: компания, мастера, услуги ===")
    people = ["owner_a", "manager_a", "master_ivan", "master_petr", "owner_b"]
    for name in people:
        c.post("/auth/register", json={"email": f"{name}@example.com", "password": PWD})

    def bearer(email: str, password: str = PWD) -> dict:
        tok = c.post("/auth/login", json={"email": email, "password": password}).json()[
            "access_token"
        ]
        c.cookies.clear()
        return {"Authorization": f"Bearer {tok}"}

    H = {n: bearer(f"{n}@example.com") for n in people}
    HA = bearer("admin@example.com", "Adm1n-Pass-123!")
    biz_a = c.post("/businesses", headers=H["owner_a"], json={"name": "Бритва"}).json()["id"]
    biz_b = c.post("/businesses", headers=H["owner_b"], json={"name": "Лилия"}).json()["id"]
    cut = c.post(
        f"/businesses/{biz_a}/services",
        headers=H["owner_a"],
        json={"name": "Стрижка", "price": "1500", "duration": 60},
    ).json()["id"]
    beard = c.post(
        f"/businesses/{biz_a}/services",
        headers=H["owner_a"],
        json={"name": "Борода", "price": "800", "duration": 30},
    ).json()["id"]
    svc_b = c.post(
        f"/businesses/{biz_b}/services",
        headers=H["owner_b"],
        json={"name": "Маникюр", "price": "1200"},
    ).json()["id"]
    c.post(
        f"/businesses/{biz_a}/members",
        headers=H["owner_a"],
        json={"email": "manager_a@example.com", "role": "MANAGER"},
    )
    r = c.post(
        f"/businesses/{biz_a}/members",
        headers=H["owner_a"],
        json={"email": "master_ivan@example.com", "role": "MASTER"},
    )
    check("участник с ролью MASTER добавлен", r.status_code in (200, 201), str(r.status_code))
    # второй мастер — через приглашение
    inv = c.post(
        f"/businesses/{biz_a}/invitations",
        headers=H["owner_a"],
        json={"email": "master_petr@example.com", "role": "MASTER"},
    ).json()
    token = inv["invite_url"].rsplit("/", 1)[-1]
    r = c.post("/invitations/accept", headers=H["master_petr"], json={"token": token})
    check("приглашение мастера принято", r.status_code in (200, 201), str(r.status_code))

    masters = c.get(f"/businesses/{biz_a}/masters", headers=H["owner_a"]).json()
    check("профили мастеров созданы автоматически (2)", len(masters) == 2, str(len(masters)))
    ivan = next(m for m in masters if m["display_name"] == "master_ivan")["id"]
    petr = next(m for m in masters if m["display_name"] == "master_petr")["id"]
    r = c.put(f"/masters/{ivan}", headers=H["owner_a"], json={"display_name": "Иван"})
    check(
        "владелец переименовал мастера", r.status_code == 200 and r.json()["display_name"] == "Иван"
    )
    c.put(f"/masters/{petr}", headers=H["owner_a"], json={"display_name": "Пётр"})
    r = c.put(f"/masters/{petr}/services", headers=H["owner_a"], json={"service_ids": [beard]})
    check(
        "услуги мастера: Пётр делает только бороду",
        r.status_code == 200 and r.json()["service_ids"] == [beard],
    )
    r = c.put(f"/masters/{petr}/services", headers=H["owner_a"], json={"service_ids": [svc_b]})
    check("чужая услуга мастеру не назначается (422)", r.status_code == 422)
    solo = c.post(
        f"/businesses/{biz_a}/masters",
        headers=H["owner_a"],
        json={"display_name": "Анна (без аккаунта)"},
    )
    check("мастер без аккаунта создан", solo.status_code == 201 and solo.json()["user_id"] is None)
    anna = solo.json()["id"]

    # ----------------------------------------------------------------------- #
    print("\n=== 1. Доступ по ролям ===")
    check(
        "менеджер видит всех мастеров",
        len(c.get(f"/businesses/{biz_a}/masters", headers=H["manager_a"]).json()) == 3,
    )
    own = c.get(f"/businesses/{biz_a}/masters", headers=H["master_ivan"]).json()
    check("мастер видит только себя", [m["id"] for m in own] == [ivan])
    check(
        "мастеру закрыты диалоги (403)",
        c.get(f"/businesses/{biz_a}/conversations", headers=H["master_ivan"]).status_code == 403,
    )
    check(
        "мастеру закрыты лиды (403)",
        c.get(f"/businesses/{biz_a}/leads", headers=H["master_ivan"]).status_code == 403,
    )
    check(
        "мастеру закрыты клиенты (403)",
        c.get(f"/businesses/{biz_a}/customers", headers=H["master_ivan"]).status_code == 403,
    )
    check(
        "мастеру закрыта карточка компании (403)",
        c.get(f"/businesses/{biz_a}", headers=H["master_ivan"]).status_code == 403,
    )
    check(
        "мастер не создаёт мастеров (403)",
        c.post(
            f"/businesses/{biz_a}/masters", headers=H["master_ivan"], json={"display_name": "x"}
        ).status_code
        == 403,
    )
    check(
        "менеджер не создаёт мастеров (403)",
        c.post(
            f"/businesses/{biz_a}/masters", headers=H["manager_a"], json={"display_name": "x"}
        ).status_code
        == 403,
    )
    check(
        "чужой владелец: мастера компании A — 404",
        c.get(f"/businesses/{biz_a}/masters", headers=H["owner_b"]).status_code == 404,
    )
    check(
        "чужой владелец: PUT /masters/{id} — 404",
        c.put(f"/masters/{ivan}", headers=H["owner_b"], json={"display_name": "x"}).status_code
        == 404,
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 2. Смены по датам ===")
    r = c.post(
        f"/masters/{ivan}/shifts",
        headers=H["master_ivan"],
        json={"day": iso(D1), "start_time": "10:00", "end_time": "14:00"},
    )
    check("мастер ставит себе смену", r.status_code == 201, str(r.status_code))
    shift_ivan = r.json()["id"]
    r = c.post(
        f"/masters/{ivan}/shifts",
        headers=H["master_ivan"],
        json={"day": iso(D1), "start_time": "13:00", "end_time": "15:00"},
    )
    check("пересекающаяся смена → 409", r.status_code == 409)
    r = c.post(
        f"/masters/{ivan}/shifts",
        headers=H["master_ivan"],
        json={"day": iso(D1), "start_time": "15:00", "end_time": "18:00"},
    )
    check("вторая смена после перерыва", r.status_code == 201)
    r = c.post(
        f"/masters/{ivan}/shifts",
        headers=H["master_ivan"],
        json={"day": iso(D1), "start_time": "19:00", "end_time": "18:00"},
    )
    check("конец раньше начала → 422", r.status_code == 422)
    r = c.post(
        f"/masters/{petr}/shifts",
        headers=H["master_ivan"],
        json={"day": iso(D1), "start_time": "10:00", "end_time": "12:00"},
    )
    check("мастер не правит чужое расписание (403)", r.status_code == 403)
    r = c.post(
        f"/masters/{petr}/shifts",
        headers=H["manager_a"],
        json={"day": iso(D1), "start_time": "10:00", "end_time": "12:00"},
    )
    check("менеджер не правит расписание (403)", r.status_code == 403)
    r = c.post(
        f"/masters/{petr}/shifts",
        headers=H["owner_a"],
        json={"day": iso(D1), "start_time": "10:00", "end_time": "12:00"},
    )
    check("владелец ставит смену любому мастеру", r.status_code == 201)
    c.post(
        f"/masters/{anna}/shifts",
        headers=H["owner_a"],
        json={"day": iso(D2), "start_time": "09:00", "end_time": "11:00"},
    )
    params = {"date_from": iso(D1), "date_to": iso(D2)}
    check(
        "менеджер видит смены всех (4)",
        len(c.get(f"/businesses/{biz_a}/shifts", headers=H["manager_a"], params=params).json())
        == 4,
    )
    own_shifts = c.get(
        f"/businesses/{biz_a}/shifts", headers=H["master_ivan"], params=params
    ).json()
    check(
        "мастер видит только свои смены",
        {s["master_id"] for s in own_shifts} == {ivan} and len(own_shifts) == 2,
    )
    r = c.put(
        f"/shifts/{shift_ivan}",
        headers=H["master_petr"],
        json={"start_time": "09:00", "end_time": "14:00"},
    )
    check("чужую смену мастер не меняет (403)", r.status_code == 403)
    r = c.put(
        f"/shifts/{shift_ivan}",
        headers=H["owner_b"],
        json={"start_time": "09:00", "end_time": "14:00"},
    )
    check("смена чужой компании — 404", r.status_code == 404)
    check(
        "слишком длинный период → 422",
        c.get(
            f"/businesses/{biz_a}/shifts",
            headers=H["owner_a"],
            params={"date_from": iso(D1), "date_to": iso(D1 + timedelta(days=90))},
        ).status_code
        == 422,
    )

    # ----------------------------------------------------------------------- #
    print("\n=== 3. Свободное время ===")
    slots = c.get(
        f"/businesses/{biz_a}/availability",
        headers=H["manager_a"],
        params={"service_id": cut, "date_from": iso(D1), "date_to": iso(D1)},
    ).json()
    ivan_starts = [s["local_start"][11:16] for s in slots if s["master_id"] == ivan]
    check(
        "стрижка 60 мин, шаг 30: окна Ивана 10:00…13:00 и 15:00…17:00",
        ivan_starts
        == [
            "10:00",
            "10:30",
            "11:00",
            "11:30",
            "12:00",
            "12:30",
            "13:00",
            "15:00",
            "15:30",
            "16:00",
            "16:30",
            "17:00",
        ],
        str(ivan_starts),
    )
    check("Пётр не делает стрижку — его окон нет", all(s["master_id"] != petr for s in slots))
    beard_slots = c.get(
        f"/businesses/{biz_a}/availability",
        headers=H["manager_a"],
        params={"service_id": beard, "date_from": iso(D1), "date_to": iso(D1)},
    ).json()
    check("бороду делают оба мастера", {s["master_id"] for s in beard_slots} == {ivan, petr})
    first = slots[0]
    utc_start = datetime.fromisoformat(first["starts_at"])
    check(
        "время в UTC соответствует 10:00 МСК",
        utc_start.astimezone(MSK).strftime("%H:%M") == "10:00",
    )
    check(
        "мастеру свободное время закрыто (403)",
        c.get(
            f"/businesses/{biz_a}/availability",
            headers=H["master_ivan"],
            params={"service_id": cut},
        ).status_code
        == 403,
    )
    check(
        "услуга чужой компании — 404",
        c.get(
            f"/businesses/{biz_a}/availability", headers=H["owner_a"], params={"service_id": svc_b}
        ).status_code
        == 404,
    )

    with SessionLocal() as db:
        business = db.get_one(Business, biz_a)
        service = db.get_one(Service, cut)
        late_now = datetime.combine(D1, time(12, 10), tzinfo=MSK).astimezone(UTC)
        later = booking_service.free_slots(
            db, business, service, day_from=D1, day_to=D1, master_id=ivan, now=late_now
        )
        check(
            # «сейчас» 12:10 → раньше 13:10 нельзя; 13:30 не влезает в смену до 14:00.
            "прошлое и ближайший час не предлагаются",
            bool(later) and later[0].local_start.strftime("%H:%M") == "15:00",
            later[0].local_start.strftime("%H:%M") if later else "нет",
        )

    # ----------------------------------------------------------------------- #
    print("\n=== 4. Записи сотрудниками и бронь ===")
    r = c.post(
        f"/businesses/{biz_a}/bookings",
        headers=H["manager_a"],
        json={
            "master_id": ivan,
            "service_id": cut,
            "day": iso(D1),
            "start_time": "10:00",
            "client_name": "Сергей",
        },
    )
    check(
        "менеджер записал клиента (CONFIRMED)",
        r.status_code == 201 and r.json()["status"] == "CONFIRMED",
        str(r.status_code),
    )
    booking_1 = r.json()["id"]
    r = c.post(
        f"/businesses/{biz_a}/bookings",
        headers=H["owner_a"],
        json={
            "master_id": ivan,
            "service_id": cut,
            "day": iso(D1),
            "start_time": "10:30",
            "client_name": "Олег",
        },
    )
    check("пересечение с записью → 409", r.status_code == 409)
    r = c.post(
        f"/businesses/{biz_a}/bookings",
        headers=H["owner_a"],
        json={
            "master_id": ivan,
            "service_id": cut,
            "day": iso(D1),
            "start_time": "14:00",
            "client_name": "Олег",
        },
    )
    check("вне смены (перерыв) → 409", r.status_code == 409)
    r = c.post(
        f"/businesses/{biz_a}/bookings",
        headers=H["owner_a"],
        json={
            "master_id": petr,
            "service_id": cut,
            "day": iso(D1),
            "start_time": "10:00",
            "client_name": "Олег",
        },
    )
    check("мастер не делает услугу → 409", r.status_code == 409)
    r = c.post(
        f"/businesses/{biz_a}/bookings",
        headers=H["master_ivan"],
        json={
            "master_id": ivan,
            "service_id": cut,
            "day": iso(D1),
            "start_time": "11:00",
            "client_name": "x",
        },
    )
    check("мастер не создаёт записи (403)", r.status_code == 403)
    r = c.post(
        f"/businesses/{biz_b}/bookings",
        headers=H["owner_b"],
        json={
            "master_id": ivan,
            "service_id": svc_b,
            "day": iso(D1),
            "start_time": "11:00",
            "client_name": "x",
        },
    )
    check("мастер чужой компании в записи — 404", r.status_code == 404)
    slots = c.get(
        f"/businesses/{biz_a}/availability",
        headers=H["manager_a"],
        params={"service_id": cut, "date_from": iso(D1), "date_to": iso(D1), "master_id": ivan},
    ).json()
    check(
        "занятое время исчезло из свободного",
        [s["local_start"][11:16] for s in slots][:2] == ["11:00", "11:30"],
    )

    # Гонка: две брони AI на один и тот же слот одновременно — выживает одна.
    results: list[str] = []

    def race(name: str) -> None:
        with SessionLocal() as db:
            business = db.get_one(Business, biz_a)
            try:
                booking_service.create_booking(
                    db,
                    business,
                    master=db.get_one(Master, ivan),
                    service=db.get_one(Service, cut),
                    starts_at=datetime.combine(D1, time(12, 0), tzinfo=MSK),
                    client_name=name,
                    source=BookingSource.AI,
                )
                results.append("ok")
            except booking_service.BookingConflict:
                results.append("conflict")

    threads = [threading.Thread(target=race, args=(f"Гость {i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(
        "гонка двух броней на один слот: одна успешна, одна отклонена",
        sorted(results) == ["conflict", "ok"],
        str(results),
    )
    held = db_rows(
        "SELECT id, status, source FROM bookings WHERE master_id=? AND client_name LIKE 'Гость%'",
        ivan,
    )
    check(
        "бронь AI — в статусе PENDING",
        len(held) == 1 and held[0]["status"] == "PENDING" and held[0]["source"] == "AI",
    )
    hold_id = held[0]["id"]

    # ----------------------------------------------------------------------- #
    print("\n=== 5. Решения по записи ===")
    lst = c.get(
        f"/businesses/{biz_a}/bookings",
        headers=H["master_ivan"],
        params={"date_from": iso(D1), "date_to": iso(D1)},
    ).json()
    check("мастер видит свои записи", {b["id"] for b in lst} == {booking_1, hold_id})
    check(
        "Пётр не видит записи Ивана",
        c.get(
            f"/businesses/{biz_a}/bookings",
            headers=H["master_petr"],
            params={"date_from": iso(D1), "date_to": iso(D1)},
        ).json()
        == [],
    )
    check(
        "Пётр не подтверждает чужую бронь (404)",
        c.post(f"/bookings/{hold_id}/confirm", headers=H["master_petr"]).status_code == 404,
    )
    r = c.post(f"/bookings/{hold_id}/confirm", headers=H["master_ivan"])
    check(
        "мастер подтвердил свою бронь", r.status_code == 200 and r.json()["status"] == "CONFIRMED"
    )
    check(
        "повторное подтверждение → 409",
        c.post(f"/bookings/{hold_id}/confirm", headers=H["manager_a"]).status_code == 409,
    )
    check(
        "мастер не отменяет запись (403)",
        c.post(f"/bookings/{hold_id}/cancel", headers=H["master_ivan"]).status_code == 403,
    )
    r = c.post(f"/bookings/{hold_id}/cancel", headers=H["manager_a"])
    check("менеджер отменил запись", r.status_code == 200 and r.json()["status"] == "CANCELLED")
    slots = c.get(
        f"/businesses/{biz_a}/availability",
        headers=H["manager_a"],
        params={"service_id": cut, "date_from": iso(D1), "date_to": iso(D1), "master_id": ivan},
    ).json()
    check("после отмены время снова свободно", "12:00" in [s["local_start"][11:16] for s in slots])
    check(
        "чужая компания: запись — 404",
        c.post(f"/bookings/{booking_1}/cancel", headers=H["owner_b"]).status_code == 404,
    )
    r = c.delete(f"/shifts/{shift_ivan}", headers=H["owner_a"])
    check("смену с активной записью удалить нельзя (409)", r.status_code == 409)
    r = c.put(
        f"/shifts/{shift_ivan}",
        headers=H["owner_a"],
        json={"start_time": "11:00", "end_time": "14:00"},
    )
    check("сузить смену, оставив запись вне времени, нельзя (409)", r.status_code == 409)
    r = c.delete(f"/masters/{ivan}", headers=H["owner_a"])
    check("мастера с предстоящими записями не удалить (409)", r.status_code == 409)
    events = {
        r["event_type"]
        for r in db_rows("SELECT event_type FROM system_logs WHERE business_id=?", biz_a)
    }
    check(
        "события аудита записаны",
        {
            "MASTER_CREATED",
            "SHIFT_CREATED",
            "BOOKING_CREATED",
            "BOOKING_HELD",
            "BOOKING_CONFIRMED",
            "BOOKING_CANCELLED",
        }
        <= events,
        str(sorted(e for e in events if e.startswith(("MASTER", "SHIFT", "BOOKING")))),
    )
    r = c.put(f"/businesses/{biz_a}", headers=H["owner_a"], json={"timezone": "Mars/Olympus"})
    check("неизвестный часовой пояс → 422", r.status_code == 422)
    r = c.put(
        f"/businesses/{biz_a}",
        headers=H["owner_a"],
        json={"timezone": "Asia/Yekaterinburg", "booking_enabled": True},
    )
    check(
        "часовой пояс и AI-запись сохраняются",
        r.status_code == 200
        and r.json()["timezone"] == "Asia/Yekaterinburg"
        and r.json()["booking_enabled"] is True,
    )
    slots_ekb = c.get(
        f"/businesses/{biz_a}/availability",
        headers=H["manager_a"],
        params={"service_id": beard, "date_from": iso(D2), "date_to": iso(D2), "master_id": anna},
    ).json()
    check(
        "смены читаются в поясе компании",
        bool(slots_ekb)
        and slots_ekb[0]["local_start"][11:16] == "09:00"
        and datetime.fromisoformat(slots_ekb[0]["starts_at"]).astimezone(UTC).hour == 4,
    )
    c.put(f"/businesses/{biz_a}", headers=H["owner_a"], json={"timezone": "Europe/Moscow"})

with contextlib.suppress(PermissionError):
    DB.unlink(missing_ok=True)

print(f"\nИТОГО: пройдено {len(PASSED)}, провалено {len(FAILED)}")
if FAILED:
    print("Провалены:")
    for name in FAILED:
        print("  -", name)
    sys.exit(1)
