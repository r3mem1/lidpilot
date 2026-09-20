"""
Сквозная проверка панели администратора в настоящем браузере (НЕ часть приложения).

Проходит путь ADMIN: вход, обзор, список компаний с фильтром, карточка компании, смена
статуса и тарифа, продление пробного периода, журнал событий с замаскированными секретами,
права обычного владельца, защита от XSS, мобильный экран. Снимает скриншоты страниц.
Меняет данные — запускать только на тестовой/демо-базе.

Подготовка: чистая база — сервер сам создаёт таблицы и первого ADMIN, а скрипт наполняет её
через API (владельцы, компании) и добавляет в журнал событие с секретами. Переменные сервера:
    DATABASE_URL=sqlite:///./e2e_admin.db  AUTO_CREATE_TABLES=true  JWT_SECRET=<32+ символов>
    AUTH_COOKIE_SECURE=false  REPROCESS_INTERVAL_SECONDS=0  AI_PROVIDER=stub
    BOOTSTRAP_ADMIN_EMAIL=admin@example.com  BOOTSTRAP_ADMIN_PASSWORD=Adm1n-Pass-123!
    uvicorn main:app --port 8771
    python scripts/e2e_admin.py http://127.0.0.1:8771
Браузер: по умолчанию Edge; другой Chromium (например Яндекс) — `--executable <путь к exe>`.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument("base", nargs="?", default="http://127.0.0.1:8771")
parser.add_argument("--browser", default="msedge", choices=["msedge", "chrome"])
parser.add_argument("--executable", default=None, help="путь к exe любого Chromium-браузера")
parser.add_argument("--shots", default="e2e-shots")
parser.add_argument("--db", default="e2e_admin.db", help="файл БД сервера (событие в журнал)")
args = parser.parse_args()

BASE = args.base.rstrip("/")
OUT = args.shots
Path(OUT).mkdir(parents=True, exist_ok=True)
PASSED, FAILED = [], []
ADMIN = ("admin@example.com", "Adm1n-Pass-123!")  # noqa: S105 - тестовый аккаунт
OWNER = ("owner_a@example.com", "Str0ng-Pass-1")  # noqa: S105
COMPANIES = [
    ("owner_a@example.com", "Барбершоп «Бритва»"),
    ("owner_b@example.com", "Салон «Лилия»"),
    ("owner_c@example.com", "<script>alert(1)</script>Салон"),
]


def seed():
    """Владельцы и компании через публичный API; в журнал — событие с секретами."""
    with httpx.Client(base_url=BASE) as api:
        for email, name in COMPANIES:
            registered = api.post("/auth/register", json={"email": email, "password": OWNER[1]})
            if not registered.is_success:
                return  # база уже наполнена прошлым запуском
            token = api.post("/auth/login", json={"email": email, "password": OWNER[1]}).json()
            api.cookies.clear()
            api.post(
                "/businesses",
                json={"name": name},
                headers={"Authorization": f"Bearer {token['access_token']}"},
            )
    payload = {"bot_token": "111111:" + "A" * 35, "password": "hunter2", "note": "видно"}
    conn = sqlite3.connect(args.db)
    conn.execute(
        "insert into system_logs (business_id, level, event_type, message, metadata, created_at) "
        "values (1, 'ERROR', 'AI_ERROR', ?, ?, datetime('now'))",
        ("Сбой AI: превышено время ожидания", json.dumps(payload)),
    )
    conn.commit()
    conn.close()


seed()


def check(name, cond, extra=""):
    (PASSED if cond else FAILED).append(name)
    print(("  OK  " if cond else "FAIL  ") + name + (f"  [{extra}]" if extra else ""))


def login(page, creds):
    if "/login" not in page.url:  # уже на форме входа — параметр next не теряем
        page.goto(BASE + "/login")
    page.fill("#email", creds[0])
    page.fill("#password", creds[1])
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")


with sync_playwright() as p:
    if args.executable:
        browser = p.chromium.launch(executable_path=args.executable, headless=True)
    else:
        browser = p.chromium.launch(channel=args.browser, headless=True)
    ctx = browser.new_context(
        viewport={"width": 1360, "height": 860}, locale="ru-RU", timezone_id="Europe/Moscow"
    )
    page = ctx.new_page()
    dialogs, problems = [], []

    def on_dialog(dialog):
        dialogs.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", on_dialog)
    page.on("pageerror", lambda e: problems.append(str(e)))
    page.on(
        "console",
        lambda m: (
            problems.append(m.text)
            if m.type in ("error", "warning") and "Failed to load" not in m.text
            else None
        ),
    )

    print("\n=== Доступ ===")
    page.goto(BASE + "/admin/companies")
    check("без входа → /login с возвратом", "/login" in page.url and "next=" in page.url, page.url)
    login(page, ADMIN)
    check("после входа ADMIN возвращён в панель", page.url.endswith("/admin/companies"), page.url)

    print("\n=== Обзор и список компаний ===")
    page.goto(BASE + "/admin")
    page.wait_for_load_state("networkidle")
    check(
        "обзор: метрики и MRR",
        "MRR" in page.inner_text("main") and page.locator(".board").count() == 2,
    )
    page.screenshot(path=f"{OUT}/30-admin-overview.png")
    page.goto(BASE + "/admin/companies")
    rows_all = page.locator("tbody tr").count()
    check("в списке видны все компании", rows_all >= 3, str(rows_all))
    page.select_option("#f-status", "ACTIVE")
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    check(
        "фильтр по статусу применяется",
        "status=ACTIVE" in page.url and page.locator("tbody tr").count() <= rows_all,
        page.url,
    )
    page.goto(BASE + "/admin/companies")
    check(
        "XSS: название с <script> показано текстом",
        "<script>alert(1)</script>" in page.inner_text("main")
        and page.evaluate("document.querySelectorAll('main script').length") == 0
        and not dialogs,
    )
    page.screenshot(path=f"{OUT}/31-admin-companies.png")

    print("\n=== Карточка компании: статус и тариф через JSON API ===")
    page.click("text=Салон «Лилия»")
    page.wait_for_load_state("networkidle")
    check("карточка открылась", "Тариф и пробный период" in page.inner_text("main"), page.url)
    page.select_option("#business-status", "SUSPENDED")
    page.wait_for_load_state("networkidle")
    page.wait_for_selector(".notice--error")
    check(
        "смена статуса → предупреждение о приостановке после перезагрузки",
        "Компания приостановлена" in page.inner_text("main")
        and page.input_value("#business-status") == "SUSPENDED",
    )
    page.screenshot(path=f"{OUT}/32-admin-company.png")
    page.select_option("#business-status", "ACTIVE")
    page.wait_for_load_state("networkidle")
    page.wait_for_function("!document.querySelector('.notice--error')")
    check("возврат в «Активна»", page.input_value("#business-status") == "ACTIVE")
    page.select_option("#plan", "PRO")
    page.click("form[data-api-form] button[type=submit]")
    page.wait_for_load_state("networkidle")
    page.wait_for_function("document.querySelector('main').innerText.includes('Сейчас: Про')")
    check("смена тарифа на «Про» сохранена", "Сейчас: Про" in page.inner_text("main"))
    page.click("text=Продлить на 7 дн.")
    page.wait_for_load_state("networkidle")
    check("продление срока не ломает страницу", "Тариф и пробный период" in page.inner_text("main"))

    print("\n=== События и ошибки ===")
    page.goto(BASE + "/admin/events")
    page.check("input[name=level][value=ERROR]")
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")
    check("фильтр по уровню в адресе", "level=ERROR" in page.url, page.url)
    page.locator("summary").first.click()
    payload = page.locator(".payload pre").first.inner_text()
    check(
        "данные события раскрываются, секреты замаскированы",
        "***" in payload and "AAAAAAAA" not in payload and "hunter2" not in payload,
        payload[:80],
    )
    page.screenshot(path=f"{OUT}/33-admin-events.png")

    print("\n=== Права обычного пользователя ===")
    owner_ctx = browser.new_context(viewport={"width": 1360, "height": 860}, locale="ru-RU")
    owner = owner_ctx.new_page()
    login(owner, OWNER)
    owner.goto(BASE + "/admin")
    check(
        "владелец компании → «Недостаточно прав»", "Недостаточно прав" in owner.inner_text("main")
    )
    check(
        "владелец: панель недоступна и по API",
        owner.evaluate("fetch('/admin/metrics').then(r => r.status)") is not None
        and owner.evaluate("fetch('/admin/metrics').then(r => r.status)") == 403,
    )
    owner_ctx.close()

    print("\n=== Мобильный экран ===")
    mob = browser.new_context(
        viewport={"width": 390, "height": 844}, locale="ru-RU", device_scale_factor=2
    )
    m = mob.new_page()
    login(m, ADMIN)
    for path in ("/admin", "/admin/companies", "/admin/events"):
        m.goto(BASE + path)
        m.wait_for_load_state("networkidle")
        check(
            f"{path} на телефоне без горизонтального переполнения страницы",
            m.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"),
        )
    m.click("[data-nav-toggle]")
    m.wait_for_timeout(350)
    m.screenshot(path=f"{OUT}/34-admin-mobile-menu.png")
    check("меню на телефоне открывается", m.locator("body.nav-open").count() == 1)
    mob.close()

    print("\n=== Выход ===")
    page.click("[data-logout]")
    page.wait_for_url("**/login")
    page.goto(BASE + "/admin")
    check("после выхода панель закрыта", "/login" in page.url)

    check("ни один alert/диалог XSS не сработал", not dialogs)
    check("ошибок JS и нарушений CSP в консоли нет", not problems, str(problems[:3]))
    browser.close()

print("\n" + "=" * 70)
print(f"ИТОГО: пройдено {len(PASSED)}, провалено {len(FAILED)}")
if FAILED:
    for name in FAILED:
        print("  -", name)
    raise SystemExit(1)
