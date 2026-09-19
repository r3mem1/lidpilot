"""
Проверочный скрипт этапа 1 (НЕ часть приложения, можно удалить).

Прогоняет API через FastAPI TestClient на отдельной SQLite-базе:
регистрация/вход, роли OWNER/MANAGER/ADMIN, изоляция компаний,
ограничение частоты, аудит в system_logs.

Требует дополнительно: pip install httpx
Запуск:  python smoke_test.py
"""

from __future__ import annotations

import os
import pathlib
import sqlite3
import sys

BASE = pathlib.Path(__file__).resolve().parent
DB = BASE / "test_smoke.db"
if DB.exists():
    DB.unlink()

os.environ.update(
    DATABASE_URL=f"sqlite:///{DB}",
    AUTO_CREATE_TABLES="true",
    JWT_SECRET="smoke-test-secret-key-at-least-32-characters-long",
    AUTH_COOKIE_SECURE="false",
    ENVIRONMENT="development",
    RATE_LIMIT_ENABLED="true",
    AUTH_RATE_LIMIT_ATTEMPTS="200",
    BOOTSTRAP_ADMIN_EMAIL="admin@example.com",
    BOOTSTRAP_ADMIN_PASSWORD="Adm1n-Bootstrap-Pass",
)
sys.path.insert(0, str(BASE))

from fastapi.testclient import TestClient  # noqa: E402

from config import settings  # noqa: E402
from main import app  # noqa: E402
from services import rate_limit_service  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, extra: str = "") -> None:
    (PASSED if condition else FAILED).append(f"{name} {extra}".strip())
    print(("  OK  " if condition else "FAIL  ") + name + (f"  [{extra}]" if extra else ""))


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def register(c: TestClient, email: str, password: str = "Str0ng-Pass-1") -> None:
    r = c.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201, r.text


def login(c: TestClient, email: str, password: str = "Str0ng-Pass-1") -> str:
    r = c.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


with TestClient(app) as c:
    print("\n=== 1. Служебные эндпоинты ===")
    check("GET /health = 200", c.get("/health").status_code == 200)

    print("\n=== 2. Регистрация (раздел 6.1) ===")
    r = c.post("/auth/register", json={"email": "Owner.A@Example.COM", "password": "Str0ng-Pass-1"})
    check("регистрация = 201", r.status_code == 201, str(r.status_code))
    check(
        "email нормализован в нижний регистр",
        r.json()["email"] == "owner.a@example.com",
        r.json()["email"],
    )
    check("роль по умолчанию OWNER", r.json()["role"] == "OWNER")
    check("password_hash не возвращается", "password_hash" not in r.json())
    r = c.post("/auth/register", json={"email": "owner.a@example.com", "password": "Str0ng-Pass-1"})
    check("повторный email = 409", r.status_code == 409, str(r.status_code))
    r = c.post("/auth/register", json={"email": "short@example.com", "password": "123"})
    check("короткий пароль = 422", r.status_code == 422, str(r.status_code))
    r = c.post("/auth/register", json={"email": "not-an-email", "password": "Str0ng-Pass-1"})
    check("невалидный email = 422", r.status_code == 422, str(r.status_code))

    print("\n=== 3. Вход/выход ===")
    r = c.post("/auth/login", json={"email": "owner.a@example.com", "password": "Str0ng-Pass-1"})
    check("вход = 200", r.status_code == 200, str(r.status_code))
    token_a = r.json()["access_token"]
    check("выдан токен", bool(token_a))
    cookie_set = settings.auth_cookie_name in r.cookies or settings.auth_cookie_name in c.cookies
    check("установлена HttpOnly cookie", cookie_set)
    check("cookie HttpOnly", "httponly" in r.headers.get("set-cookie", "").lower())
    r = c.post("/auth/login", json={"email": "owner.a@example.com", "password": "wrong-password"})
    check("неверный пароль = 401", r.status_code == 401, str(r.status_code))
    r = c.post("/auth/login", json={"email": "nobody@example.com", "password": "whatever-pass"})
    check("несуществующий email = 401 (без утечки)", r.status_code == 401, str(r.status_code))

    print("\n=== 4. GET /me и проверка токена ===")
    c.cookies.clear()
    check("/me без токена = 401", c.get("/me").status_code == 401)
    r = c.get("/me", headers=bearer(token_a))
    check("/me с Bearer = 200", r.status_code == 200, str(r.status_code))
    check("/me пока без компаний", r.json()["memberships"] == [])
    check(
        "/me с испорченным токеном = 401",
        c.get("/me", headers=bearer(token_a + "x")).status_code == 401,
    )
    check(
        "/me с чужой подписью = 401",
        c.get("/me", headers=bearer("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.zzz")).status_code == 401,
    )
    # cookie-авторизация (кабинет на Jinja2)
    c.cookies.set(settings.auth_cookie_name, token_a)
    check("/me по cookie = 200", c.get("/me").status_code == 200)
    c.cookies.clear()

    print("\n=== 5. Компания и услуги (разделы 6.2, 6.3) ===")
    r = c.post(
        "/businesses",
        headers=bearer(token_a),
        json={
            "name": "Барбершоп «Бритва»",
            "category": "barbershop",
            "phone": "+7 999 000-00-00",
            "working_hours": "пн-сб 10:00-21:00",
            "ai_rules": "Отвечать вежливо, не обещать запись без подтверждения.",
        },
    )
    check("создание компании = 201", r.status_code == 201, str(r.status_code))
    biz_a = r.json()
    check("статус новой компании TRIAL", biz_a["status"] == "TRIAL", biz_a["status"])
    check(
        "создатель — владелец",
        biz_a["owner_id"] == c.get("/me", headers=bearer(token_a)).json()["user"]["id"],
    )
    me_a = c.get("/me", headers=bearer(token_a)).json()
    check("в /me появилось членство OWNER", me_a["memberships"][0]["role"] == "OWNER")

    r = c.post(
        f"/businesses/{biz_a['id']}/services",
        headers=bearer(token_a),
        json={"name": "Стрижка + борода", "price": "2500.50", "duration": 60},
    )
    check("создание услуги = 201", r.status_code == 201, str(r.status_code))
    svc_a = r.json()
    check("цена как Decimal без потерь", str(svc_a["price"]) == "2500.50", str(svc_a["price"]))
    r = c.get(f"/businesses/{biz_a['id']}/services", headers=bearer(token_a))
    check("список услуг = 1", r.status_code == 200 and len(r.json()) == 1)
    r = c.put(f"/services/{svc_a['id']}", headers=bearer(token_a), json={"price": "2700.00"})
    check("изменение услуги = 200", r.status_code == 200 and str(r.json()["price"]) == "2700.00")
    r = c.put(
        f"/businesses/{biz_a['id']}", headers=bearer(token_a), json={"address": "ул. Ленина, 1"}
    )
    check(
        "изменение компании владельцем = 200",
        r.status_code == 200 and r.json()["address"] == "ул. Ленина, 1",
    )
    r = c.post(
        f"/businesses/{biz_a['id']}/services",
        headers=bearer(token_a),
        json={"name": "X", "price": "-5"},
    )
    check("отрицательная цена = 422", r.status_code == 422, str(r.status_code))

    print("\n=== 6. Изоляция тенантов (разделы 16, 21) ===")
    register(c, "owner.b@example.com")
    token_b = login(c, "owner.b@example.com")
    r = c.post("/businesses", headers=bearer(token_b), json={"name": "Салон «Вторая»"})
    biz_b = r.json()
    check("вторая компания создана", r.status_code == 201)

    checks_cross = [
        ("чужая компания GET", c.get(f"/businesses/{biz_a['id']}", headers=bearer(token_b))),
        (
            "чужая компания PUT",
            c.put(f"/businesses/{biz_a['id']}", headers=bearer(token_b), json={"name": "Взлом"}),
        ),
        ("чужие услуги GET", c.get(f"/businesses/{biz_a['id']}/services", headers=bearer(token_b))),
        (
            "чужие услуги POST",
            c.post(
                f"/businesses/{biz_a['id']}/services",
                headers=bearer(token_b),
                json={"name": "Хак", "price": "1"},
            ),
        ),
        (
            "чужая услуга PUT",
            c.put(f"/services/{svc_a['id']}", headers=bearer(token_b), json={"price": "1"}),
        ),
        ("чужая услуга DELETE", c.delete(f"/services/{svc_a['id']}", headers=bearer(token_b))),
        (
            "чужие сотрудники GET",
            c.get(f"/businesses/{biz_a['id']}/members", headers=bearer(token_b)),
        ),
        (
            "чужие сотрудники POST",
            c.post(
                f"/businesses/{biz_a['id']}/members",
                headers=bearer(token_b),
                json={"email": "owner.b@example.com"},
            ),
        ),
    ]
    for name, resp in checks_cross:
        check(f"{name} = 404", resp.status_code == 404, str(resp.status_code))
    r = c.get(f"/businesses/{biz_b['id']}/services", headers=bearer(token_b))
    check("свои услуги видны (пусто)", r.status_code == 200 and r.json() == [])
    r = c.put(f"/services/{svc_a['id']}", headers=bearer(token_a), json={"active": False})
    check(
        "услуга A не изменена чужими запросами",
        r.status_code == 200 and str(r.json()["price"]) == "2700.00",
    )

    print("\n=== 7. Роли внутри компании (раздел 5) ===")
    register(c, "manager@example.com")
    token_m = login(c, "manager@example.com")
    r = c.post(
        f"/businesses/{biz_a['id']}/members",
        headers=bearer(token_a),
        json={"email": "manager@example.com", "role": "MANAGER"},
    )
    check("владелец добавил менеджера = 201", r.status_code == 201, str(r.status_code))
    r = c.post(
        f"/businesses/{biz_a['id']}/members",
        headers=bearer(token_a),
        json={"email": "manager@example.com"},
    )
    check("повторное добавление = 409", r.status_code == 409, str(r.status_code))
    r = c.post(
        f"/businesses/{biz_a['id']}/members",
        headers=bearer(token_a),
        json={"email": "ghost@example.com"},
    )
    check("незарегистрированный сотрудник = 404", r.status_code == 404, str(r.status_code))

    check(
        "менеджер видит компанию",
        c.get(f"/businesses/{biz_a['id']}", headers=bearer(token_m)).status_code == 200,
    )
    check(
        "менеджер видит прайс",
        c.get(f"/businesses/{biz_a['id']}/services", headers=bearer(token_m)).status_code == 200,
    )
    r = c.put(f"/businesses/{biz_a['id']}", headers=bearer(token_m), json={"name": "Переименовал"})
    check("менеджер не меняет настройки = 403", r.status_code == 403, str(r.status_code))
    r = c.post(
        f"/businesses/{biz_a['id']}/services",
        headers=bearer(token_m),
        json={"name": "Y", "price": "1"},
    )
    check("менеджер не создаёт услуги = 403", r.status_code == 403, str(r.status_code))
    r = c.put(f"/services/{svc_a['id']}", headers=bearer(token_m), json={"price": "1"})
    check("менеджер не меняет прайс = 403", r.status_code == 403, str(r.status_code))
    r = c.delete(f"/services/{svc_a['id']}", headers=bearer(token_m))
    check("менеджер не удаляет услуги = 403", r.status_code == 403, str(r.status_code))
    r = c.get(f"/businesses/{biz_a['id']}/members", headers=bearer(token_m))
    check("менеджер не видит список сотрудников = 403", r.status_code == 403, str(r.status_code))
    me_m = c.get("/me", headers=bearer(token_m)).json()
    check(
        "у менеджера 1 компания с ролью MANAGER",
        me_m["memberships"]
        == [{"business_id": biz_a["id"], "business_name": "Барбершоп «Бритва»", "role": "MANAGER"}],
        str(me_m["memberships"]),
    )

    print("\n=== 8. ADMIN платформы (раздел 5, 15) ===")
    token_admin = login(c, "admin@example.com", "Adm1n-Bootstrap-Pass")
    check("ADMIN создан из .env и вошёл", bool(token_admin))
    check(
        "ADMIN видит компанию A",
        c.get(f"/businesses/{biz_a['id']}", headers=bearer(token_admin)).status_code == 200,
    )
    check(
        "ADMIN видит компанию B",
        c.get(f"/businesses/{biz_b['id']}", headers=bearer(token_admin)).status_code == 200,
    )
    check(
        "ADMIN: несуществующая компания = 404",
        c.get("/businesses/9999", headers=bearer(token_admin)).status_code == 404,
    )
    check(
        "роль ADMIN в /me",
        c.get("/me", headers=bearer(token_admin)).json()["user"]["role"] == "ADMIN",
    )
    r = c.post(
        "/auth/register",
        json={"email": "fake-admin@example.com", "password": "Str0ng-Pass-1", "role": "ADMIN"},
    )
    check(
        "через регистрацию нельзя стать ADMIN",
        r.status_code == 201 and r.json()["role"] == "OWNER",
        r.json().get("role"),
    )

    print("\n=== 9. Блокировка пользователя и выход ===")
    r = c.post("/auth/logout", headers=bearer(token_a))
    check("выход = 204", r.status_code == 204, str(r.status_code))
    check(
        "токен после выхода остаётся валидным до истечения (stateless JWT)",
        c.get("/me", headers=bearer(token_a)).status_code == 200,
    )

    conn = sqlite3.connect(DB)
    conn.execute("update users set status='SUSPENDED' where email='owner.b@example.com'")
    conn.commit()
    check(
        "заблокированный пользователь = 403",
        c.get("/me", headers=bearer(token_b)).status_code == 403,
    )
    r = c.post("/auth/login", json={"email": "owner.b@example.com", "password": "Str0ng-Pass-1"})
    check("заблокированный не может войти = 401", r.status_code == 401, str(r.status_code))
    conn.execute("update users set status='ACTIVE' where email='owner.b@example.com'")
    conn.commit()

    print("\n=== 10. Удаление услуги и каскады ===")
    r = c.delete(f"/services/{svc_a['id']}", headers=bearer(token_a))
    check("удаление услуги владельцем = 204", r.status_code == 204, str(r.status_code))
    check(
        "повторное удаление = 404",
        c.delete(f"/services/{svc_a['id']}", headers=bearer(token_a)).status_code == 404,
    )

    print("\n=== 11. Ограничение частоты (раздел 16) ===")
    rate_limit_service.reset()
    original = settings.auth_rate_limit_attempts
    settings.auth_rate_limit_attempts = 3
    codes = [
        c.post(
            "/auth/login", json={"email": "owner.a@example.com", "password": "bad-password-x"}
        ).status_code
        for _ in range(5)
    ]
    check("после лимита приходит 429", codes.count(429) >= 2, str(codes))
    settings.auth_rate_limit_attempts = original
    rate_limit_service.reset()

    print("\n=== 12. Истёкший токен ===")
    original_ttl = settings.access_token_ttl_minutes
    settings.access_token_ttl_minutes = -1
    expired = login(c, "owner.a@example.com")
    settings.access_token_ttl_minutes = original_ttl
    check("истёкший токен = 401", c.get("/me", headers=bearer(expired)).status_code == 401)

print("\n=== 13. Аудит в system_logs (разделы 16, 17) ===")
conn = sqlite3.connect(DB)
rows = dict(
    conn.execute("select event_type, count(*) from system_logs group by event_type").fetchall()
)
for event in [
    "AUTH_REGISTER",
    "AUTH_LOGIN_SUCCESS",
    "AUTH_LOGIN_FAILED",
    "AUTH_LOGOUT",
    "BUSINESS_CREATED",
    "BUSINESS_UPDATED",
    "BUSINESS_MEMBER_ADDED",
    "SERVICE_CREATED",
    "SERVICE_UPDATED",
    "SERVICE_DELETED",
    "ACCESS_DENIED",
    "ADMIN_BOOTSTRAPPED",
]:
    check(f"событие {event} записано", rows.get(event, 0) > 0, str(rows.get(event, 0)))
denied = conn.execute(
    "select business_id, metadata from system_logs where event_type='ACCESS_DENIED' limit 1"
).fetchone()
check(
    "ACCESS_DENIED содержит business_id и актора",
    denied and denied[0] and "actor_user_id" in denied[1],
    str(denied),
)
check(
    "неудачный вход не пишет пароль",
    all(
        "Str0ng" not in (m or "") and "bad-password" not in (m or "")
        for (m,) in conn.execute(
            "select metadata from system_logs where event_type='AUTH_LOGIN_FAILED'"
        )
    ),
)

print("\n" + "=" * 70)
print(f"ИТОГО: пройдено {len(PASSED)}, провалено {len(FAILED)}")
if FAILED:
    print("ПРОВАЛЕНО:")
    for f in FAILED:
        print("  -", f)
sys.exit(1 if FAILED else 0)
