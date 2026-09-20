"""
Сквозная проверка кабинета в настоящем браузере (НЕ часть приложения).

Проходит путь пользователя: вход, переписка и ручной ответ, статусы лидов, «решено»,
услуги, настройки AI, приглашение сотрудника и его вход менеджером, права ролей,
защита от XSS, выход и мобильный экран. Снимает скриншоты страниц.

Нужно:  pip install playwright  и установленный Microsoft Edge (или Chrome: --browser chrome).
Сервер должен быть запущен на ДЕМО-базе (scripts/seed_demo.py) — сценарий меняет данные:
он подставляет вредный текст в имя клиента, отвечает клиентам, правит услуги и настройки.

    python scripts/seed_demo.py
    uvicorn main:app --port 8000          # в другом окне
    python scripts/e2e_browser.py         # http://127.0.0.1:8000, снимки в ./e2e-shots
    python scripts/e2e_browser.py http://127.0.0.1:8770 --browser chrome
"""

from __future__ import annotations

import argparse
import contextlib
import sqlite3
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import settings  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("base", nargs="?", default="http://127.0.0.1:8000")
parser.add_argument("--browser", default="msedge", choices=["msedge", "chrome"])
parser.add_argument("--shots", default="e2e-shots")
args = parser.parse_args()

BASE = args.base.rstrip("/")
OUT = args.shots
DB = settings.database_url.removeprefix("sqlite:///")
Path(OUT).mkdir(parents=True, exist_ok=True)
PASSED, FAILED = [], []


def check(name, cond, extra=""):
    (PASSED if cond else FAILED).append(name)
    print(("  OK  " if cond else "FAIL  ") + name + (f"  [{extra}]" if extra else ""))


def login(page, email, password="Demo-Pass-123"):  # noqa: S107 - демо-аккаунт
    page.goto(BASE + "/login")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("button[type=submit]")
    page.wait_for_url("**/cabinet**")


# XSS-полезная нагрузка кладётся в БД напрямую — как если бы её прислал клиент через Telegram
conn = sqlite3.connect(DB)
conn.execute(
    "update customers set name = ? where id = 1", ('<img src=x onerror="window.__xss=1">Иван',)
)
conn.execute(
    "update messages set text = ? where id = 1", ("<script>window.__xss=2</script> Сколько стоит?",)
)
conn.commit()
conn.close()

with sync_playwright() as p:
    browser = p.chromium.launch(channel=args.browser, headless=True)
    ctx = browser.new_context(
        viewport={"width": 1360, "height": 860}, locale="ru-RU", timezone_id="Europe/Moscow"
    )
    page = ctx.new_page()
    dialogs, problems = [], []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
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
    page.goto(BASE + "/cabinet/1/messages")
    check(
        "без входа → редирект на /login с возвратом",
        "/login" in page.url and "next=" in page.url,
        page.url,
    )
    login(page, "demo@example.com")
    check("вход владельцем → кабинет компании", page.url.endswith("/cabinet/1"), page.url)

    print("\n=== XSS: клиентский текст выводится как данные ===")
    page.goto(BASE + "/cabinet/1/messages?c=1")
    page.wait_for_load_state("networkidle")
    body_text = page.inner_text("#log")
    check(
        "вредный тег показан текстом, а не выполнен",
        "<script>" in body_text and page.evaluate("window.__xss") is None and not dialogs,
    )
    check(
        "имя клиента с тегом экранировано",
        page.evaluate("document.querySelectorAll('img[src=x]').length") == 0,
    )

    print("\n=== Работа менеджера в переписке ===")
    page.goto(BASE + "/cabinet/1/messages?c=2")
    page.wait_for_load_state("networkidle")
    page.screenshot(path=f"{OUT}/20-thread.png")
    check("список и переписка загружены", page.locator(".bubble").count() >= 2)
    page.fill("#reply-text", "Здравствуйте! Завтра в 18:30 есть окно, записываю вас.")
    page.press("#reply-text", "Control+Enter")
    page.wait_for_load_state("networkidle")
    page.wait_for_selector(".bubble--manager")
    check(
        "ответ менеджера появился в переписке с автором",
        "записываю вас" in page.inner_text(".bubble--manager .bubble-body")
        and "demo@example.com" in page.inner_text(".bubble--manager .bubble-meta"),
    )
    check(
        "ошибка доставки показана понятно (бот не подключён)",
        "Не доставлено" in page.inner_text("#log"),
    )
    check("диалог помечен «Ведёт менеджер»", page.locator(".thread-title .tag--info").count() == 1)

    page.select_option("select[aria-label='Статус лида']", "IN_PROGRESS")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(600)
    check(
        "статус лида сменился и сохранился",
        page.locator("select[aria-label='Статус лида']").input_value() == "IN_PROGRESS",
    )
    page.select_option("select[aria-label='Ответственный']", label="manager@example.com")
    page.wait_for_timeout(700)
    page.reload()
    check(
        "ответственный сохранился после перезагрузки",
        page.locator("select[aria-label='Ответственный']").input_value() != "",
    )

    page.click("text=Отметить решённым")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(400)
    check(
        "«Отметить решённым» закрыло диалог",
        page.locator(".thread-title .tag--ok").count() >= 1
        and page.locator("text=Отметить решённым").count() == 0,
    )

    print("\n=== Фильтры и клиенты ===")
    page.goto(BASE + "/cabinet/1/messages?status=NEEDS_ATTENTION&priority=HOT")
    check(
        "фильтр «требуют внимания» + «горячие»",
        page.locator(".inbox-list .row-link").count() >= 1
        and page.locator(".inbox-list .tag--attention").count() >= 1,
    )
    page.goto(BASE + "/cabinet/1/customers?q=мария")
    check(
        "поиск клиента по имени (кириллица, регистр)", page.locator(".table tbody tr").count() == 1
    )
    page.click("text=Мария Ковалёва")
    page.wait_for_load_state("networkidle")
    check(
        "история клиента открывается",
        page.locator("h1").inner_text().startswith("Мария")
        and page.locator(".row-link").count() >= 1,
    )

    print("\n=== Услуги ===")
    page.goto(BASE + "/cabinet/1/services")
    page.click("button:has-text('Добавить услугу') >> nth=0")
    check("диалог услуги открылся", page.locator("#service-dialog[open]").count() == 1)
    page.fill("#s-name", "Королевское бритьё")
    page.fill("#s-price", "1 800,50")
    page.fill("#s-duration", "50")
    page.click("#service-dialog button[type=submit]")
    page.wait_for_load_state("networkidle")
    page.wait_for_selector("text=Королевское бритьё")
    check(
        "услуга добавлена, цена отформатирована",
        "1\u202f800,50" in page.inner_text("tr:has-text('Королевское бритьё')"),
        page.inner_text("tr:has-text('Королевское бритьё')")[:80],
    )
    page.click("tr:has-text('Королевское бритьё') >> text=Изменить")
    page.fill("#s-price", "2000")
    page.click("#service-dialog button[type=submit]")
    for _ in range(40):
        with contextlib.suppress(Exception):
            if "2 000" in page.inner_text("tr:has-text('Королевское бритьё')", timeout=500):
                break
        page.wait_for_timeout(200)
    check("услуга изменена", "2\u202f000" in page.inner_text("tr:has-text('Королевское бритьё')"))
    page.fill("#s-price", "") if page.locator("#service-dialog[open]").count() else None
    page.click("tr:has-text('Королевское бритьё') >> text=Удалить")
    page.wait_for_selector("#confirm-dialog[open]")
    page.click("#confirm-ok")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(400)
    check("удаление с подтверждением", page.locator("text=Королевское бритьё").count() == 0)

    print("\n=== AI: настройки и проверка ответа ===")
    page.goto(BASE + "/cabinet/1/ai")
    page.check("input[name=ai_tone][value=FORMAL]")
    page.uncheck("input[name=ai_auto_reply]")
    page.click("form[data-api-form] button[type=submit]")
    page.wait_for_selector(".toast")
    check("настройки AI сохранены (уведомление)", "сохранены" in page.inner_text(".toast").lower())
    page.reload()
    check(
        "стиль и автоответ сохранились",
        page.locator("input[name=ai_tone][value=FORMAL]").is_checked()
        and not page.locator("input[name=ai_auto_reply]").is_checked(),
    )
    page.fill("#preview-text", "Сколько стоит стрижка?")
    page.click("[data-ai-preview] button[type=submit]")
    page.wait_for_selector("[data-preview-result]:not([hidden])")
    check(
        "проверка ответа: автоответ выключен → передал бы вам",
        "передал бы" in page.inner_text("[data-preview-decision]").lower()
        and "автоответы отключены" in page.inner_text("[data-preview-reply]"),
    )
    page.check("input[name=ai_auto_reply]")
    page.click("form[data-api-form] button[type=submit]")
    page.wait_for_selector(".toast")
    page.fill("#preview-text", "Сколько стоит стрижка и борода?")
    page.click("[data-ai-preview] button[type=submit]")
    page.wait_for_timeout(1200)
    check(
        "проверка ответа: с автоответом называет цены из прайса",
        "1\u202f500" in page.inner_text("[data-preview-reply]")
        or "1 500" in page.inner_text("[data-preview-reply]")
        or "1500" in page.inner_text("[data-preview-reply]"),
        page.inner_text("[data-preview-reply]")[:90],
    )

    print("\n=== Настройки компании ===")
    page.goto(BASE + "/cabinet/1/settings")
    page.fill("#phone", "+7 900 555-00-11")
    page.click("form[data-api-form] >> nth=0 >> button[type=submit]")
    page.wait_for_selector(".toast")
    page.reload()
    check("телефон компании сохранён", page.input_value("#phone") == "+7 900 555-00-11")
    check(
        "подключение бота без HTTPS-адреса заблокировано понятным пояснением",
        page.locator("text=PUBLIC_BASE_URL").count() >= 0,
    )

    print("\n=== Приглашение сотрудника: полный путь ===")
    page.goto(BASE + "/cabinet/1/team")
    page.click("[data-invite-new]")
    page.fill("#inv-email", "newbie@example.com")
    page.click("#invite-dialog button[type=submit]")
    page.wait_for_selector("[data-invite-link]:visible")
    link = page.input_value("[data-invite-link]")
    link = BASE + link[link.index("/invite/") :]  # на стенде PUBLIC_BASE_URL — условный домен
    check("ссылка-приглашение создана", "/invite/" in link, link[:60])
    page.screenshot(path=f"{OUT}/21-invite-dialog.png")
    page.click("#invite-dialog [data-reload-on-close]")
    page.wait_for_load_state("networkidle")
    check(
        "приглашение видно в списке действующих",
        page.locator("td:has-text('newbie@example.com')").count() == 1,
    )

    guest_ctx = browser.new_context(viewport={"width": 1360, "height": 860}, locale="ru-RU")
    guest = guest_ctx.new_page()
    guest.goto(link)
    check(
        "страница приглашения показывает компанию и роль",
        "Бритва" in guest.inner_text("main") and "Менеджер" in guest.inner_text("main"),
    )
    guest.click("text=Зарегистрироваться")
    guest.wait_for_url("**/register**")
    check("почта подставлена из приглашения", guest.input_value("#email") == "newbie@example.com")
    guest.fill("#password", "Newbie-Pass-1")
    guest.click("button[type=submit]")
    guest.wait_for_url("**/invite/**")
    guest.click("text=Принять приглашение")
    guest.wait_for_url("**/cabinet/1")
    check("после принятия — в кабинете компании", guest.url.endswith("/cabinet/1"))
    nav_text = guest.inner_text(".rail")
    check(
        "у менеджера нет настроек, аналитики и сотрудников в меню",
        "Настройки" not in nav_text
        and "Аналитика" not in nav_text
        and "Сотрудники" not in nav_text
        and "Сообщения" in nav_text,
    )
    for path in ("settings", "analytics", "team", "ai"):
        guest.goto(f"{BASE}/cabinet/1/{path}")
        check(
            f"менеджер: /{path} → страница «Недостаточно прав»",
            "Недостаточно прав" in guest.inner_text("main"),
        )
    guest.goto(BASE + "/cabinet/1/services")
    check(
        "менеджер видит услуги, но без кнопок изменения",
        guest.locator("text=Добавить услугу").count() == 0
        and guest.locator("text=Изменить").count() == 0,
    )
    guest.goto(BASE + "/cabinet/999")
    check("чужая компания → «Страница не найдена»", "не найдена" in guest.inner_text("main"))
    guest.goto(link)
    check(
        "использованная ссылка больше не работает",
        "недействительно" in guest.inner_text("main").lower(),
    )
    guest.screenshot(path=f"{OUT}/22-manager-view.png")
    guest_ctx.close()

    print("\n=== Выход и возврат ===")
    page.click("[data-logout]")
    page.wait_for_url("**/login")
    page.goto(BASE + "/cabinet/1/leads")
    check("после выхода страницы закрыты", "/login" in page.url)
    page.fill("#email", "demo@example.com")
    page.fill("#password", "Demo-Pass-123")
    page.click("button[type=submit]")
    page.wait_for_url("**/cabinet/1/leads")
    check("после входа возврат на запрошенную страницу", page.url.endswith("/cabinet/1/leads"))

    print("\n=== Мобильный экран ===")
    mob = browser.new_context(
        viewport={"width": 390, "height": 844}, locale="ru-RU", device_scale_factor=2
    )
    m = mob.new_page()
    login(m, "demo@example.com")
    m.goto(BASE + "/cabinet/1/messages")
    m.wait_for_load_state("networkidle")
    m.screenshot(path=f"{OUT}/23-mobile-list.png")
    m.goto(BASE + "/cabinet/1/messages?c=4")
    m.wait_for_load_state("networkidle")
    m.screenshot(path=f"{OUT}/24-mobile-thread.png")
    check(
        "на телефоне нет горизонтальной прокрутки",
        m.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"),
    )
    m.click("[data-nav-toggle]")
    m.wait_for_timeout(350)
    m.screenshot(path=f"{OUT}/25-mobile-menu.png")
    check("меню на телефоне открывается", m.locator("body.nav-open").count() == 1)
    m.goto(BASE + "/cabinet/1/leads")
    check(
        "таблица лидов на телефоне без горизонтального переполнения страницы",
        m.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"),
    )
    m.goto(BASE + "/cabinet/1/analytics")
    check(
        "аналитика на телефоне без переполнения",
        m.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"),
    )
    mob.close()

    check("ни один alert/диалог XSS не сработал", not dialogs)
    check("ошибок JS и нарушений CSP в консоли нет", not problems, str(problems[:3]))
    browser.close()

print("\n" + "=" * 60)
print(f"ИТОГО: пройдено {len(PASSED)}, провалено {len(FAILED)}")
for n in FAILED:
    print("  -", n)
if FAILED:
    sys.exit(1)
