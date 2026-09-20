# LeadPilot — контекст проекта для Claude Code

Микро-SaaS «LeadPilot»: AI-обработка входящих заявок малого бизнеса (MVP-вертикаль — барбершопы/салоны).
Источник истины — `C:\workflow\ТЗ_LeadPilot_MicroSaaS.docx` (v1.0). Разделы ТЗ ниже указаны как «§N».
При конфликте кода и ТЗ — ТЗ выигрывает; отклонение от ТЗ согласуется с пользователем.

## Стек (§9)
Python 3.11+ (локально 3.13), FastAPI, SQLAlchemy 2.0, Alembic, Pydantic v2, SQLite (dev) / PostgreSQL-Supabase (prod),
Jinja2 + HTML/CSS/JS (без SPA), httpx → OpenAI-совместимый LLM API, Telegram Bot API, Argon2id + JWT (HttpOnly cookie / Bearer).

## Слои (§18, Приложение A) — нарушать нельзя
```
routes/        тонкий HTTP-слой: валидация (schemas.py), зависимости доступа, вызов services
services/      бизнес-логика, транзакции, аудит (audit_service.log_event)
models.py      ORM;  schemas.py — Pydantic;  migrations/ — Alembic (схема в prod меняется ТОЛЬКО миграциями)
integrations/  внешние каналы за Protocol ChannelClient (Telegram сейчас, WhatsApp потом)
ai/            pipeline без знаний о БД и Telegram: normalize → classify → context → respond → validate
templates/ static/   кабинет (Jinja2)
```

## Жёсткие инварианты
1. **Мультитенантность (§8, §16):** маршрут никогда не берёт `business_id` из запроса «как есть» — только через `BusinessContext`
   (`services/access_service.py`). Нет доступа → **404, не 403**. Любой запрос к бизнес-данным фильтруется по `business_id`.
2. **AI не выдумывает** цены, адреса, сроки, скидки, свободные слоты (§6.6, §12.3). Ответ уходит клиенту только после `ResponseValidator`;
   иначе `ESCALATE` менеджеру (§6.7). Данные из БД приоритетнее слов клиента.
3. **Секреты** только в `.env`/секрет-хранилище; не в коде, не в логах, не в ответах API (§16). `integrations.credentials_ref` — ссылка, не сам секрет.
   Токен бота компании хранится только зашифрованным (`enc:…`, `services/secret_store.py`), секрет webhook — только SHA-256;
   URL Bot API содержит токен → логгеры `httpx`/`httpcore` держать на WARNING, ошибки клиента канала — без URL.
4. **Сообщение не теряется (§18, §21):** сначала сохранить входящее, потом вызывать внешние API; webhook идемпотентен
   (по `external_message_id`/`update_id`), повторная обработка безопасна.
5. **Аудит и логи (§16–17):** каждое значимое событие → `system_logs` через `audit_service`; по логам должно быть видно,
   что случилось с конкретным сообщением и почему отправлен именно этот ответ.
6. После ручного ответа менеджера AI молчит в диалоге до «решено» (`conversations.handled_by_manager`); приоритет лида внутри открытого диалога не понижается.
7. Кабинет (`routes/cabinet.py`, `templates/`, `static/`): весь чужой текст только через автоэкранирование Jinja (без `|safe`), в JS — `textContent`; без inline-скриптов/стилей (строгий CSP в `main.py`); данные кабинет меняет только через JSON API; cookie-сессия защищена проверкой Origin (`enforce_same_origin`).
8. Роль проверяется на **каждом** защищённом endpoint; `/admin/*` — только ADMIN (`require_platform_admin`); ADMIN не создаётся публичной регистрацией.

## Статус этапов (§20)
| Этап | Состояние |
|---|---|
| 1 Локальное ядро (auth, RBAC, компании, услуги) | ✅ реализован, `smoke_test.py` |
| 2 AI pipeline | ✅ реализован, `smoke_test_ai.py` |
| 3 Telegram (webhook, отправка) | ✅ реализован, `smoke_test_stage3.py` |
| 4 CRM-ядро (лиды, статусы, ручной ответ, фильтры) | ✅ реализован, `smoke_test_stage4.py` |
| 5 Кабинет бизнеса (dashboard, сообщения, лиды, клиенты, услуги, AI, команда, настройки, аналитика) | ✅ реализован, `smoke_test_stage5.py`, `scripts/e2e_browser.py` |
| 6 Admin-панель | ⏳ `routes/admin.py` пуст |
| 7–9 Пилот, SaaS-автоматизация, масштабирование | — |

Не выходить за границы текущего этапа: не добавлять функции из «Не входит в MVP» (§19) — календарь, биллинг, несколько каналов, мобильное приложение.

## Команды
Windows: перед запуском тестов и сканеров `export PYTHONUTF8=1` (PowerShell: `$env:PYTHONUTF8=1`) — иначе кириллица/стрелки в выводе роняют скрипты (`charmap`).
```bash
pip install -r requirements.txt -r requirements-dev.txt
alembic upgrade head                   # миграции; новая: alembic revision --autogenerate -m "..."
uvicorn main:app --reload              # http://127.0.0.1:8000/docs
python smoke_test.py                   # 80 проверок этапа 1
python smoke_test_ai.py                # 141 проверка этапа 2
python smoke_test_stage3.py            # 139 проверок этапа 3
python smoke_test_stage4.py            # 121 проверка этапа 4
python smoke_test_stage5.py            # 181 проверка этапа 5
python scripts/seed_demo.py            # демо-данные для кабинета (только на dev-БД)
ruff check . && ruff format --check .  # стиль
pyright                                # типы (LSP-плагин pyright-lsp)
bandit -r . -x ./migrations,./smoke_test.py,./smoke_test_ai.py   # SAST
pip-audit -r requirements.txt          # уязвимости зависимостей
```
Тесты — самодостаточные скрипты `smoke_test*.py` (httpx + временная SQLite). Новый этап = новый `smoke_test_stageN.py` в том же стиле.
**Запускать тесты через `python scripts/run_checks.py [stage4 …]`** — выводит только «ИТОГО» и упавшие проверки; полный вывод в `.test_logs/*.log`
(открывать лог только при падении и нужен контекст). Голые `python smoke_test*.py` — только если нужен полный вывод.
**Линтеры одной командой: `python scripts/run_checks.py lint [ruff|pyright|bandit|pip-audit]`** — те же ruff/format/pyright/bandit/pip-audit,
но bandit выводится строкой на находку (не 250 строк), остальные — итоговой строкой. Находки не фильтруются; полные логи в `.test_logs/lint_*.log`.
Известный шум: bandit даёт 19 LOW только в `smoke_test_stage3-5.py` и `scripts/` (тестовые пароли, assert) — в коде приложения замечаний нет.
Hook `scripts/hooks/ruff_after_edit.py` (PostToolUse) после правки `.py` молча проверяет файл ruff'ом и при замечаниях возвращает их сразу.

## Экономия токенов (без потери качества проверок)
- Не читать целиком `README.md`, `docs/manual-check.md`, `smoke_test_stage*.py`, `models.py`, `services/message_service.py`: сначала Grep, затем Read с `offset/limit`.
- Широкий поиск «где что реализовано» — субагенту `Explore`; в основной контекст возвращать выводы, а не дампы файлов.
- Не перечитывать файл после Edit; независимые вызовы инструментов — параллельно, одним сообщением.
- Не читать `.env`, `*.db`, `static/fonts/`, кэши (закрыто в `permissions.deny`); БД смотреть через MCP `sqlite` с точечным SELECT.
- Проверки (ruff/pyright/bandit) запускать на изменённых файлах, полный прогон — перед коммитом и закрытием этапа.

## Соглашения кода
- Комментарии и docstring — на русском, с указанием раздела ТЗ; имена — английские; `from __future__ import annotations`.
- Enum-значения в БД — строки (`UserRole`, `BusinessStatus`, …); новые типы событий — в `audit_service.EventType`.
- Пароли — Argon2id; токены — JWT в HttpOnly cookie (кабинет) или Bearer (API).
- Не добавлять endpoint'ы вне §11 без явного пометки «вне ТЗ» (как `ai/preview`, выключен флагом).

## Инструменты, настроенные для проекта
- **Агенты** (`.claude/agents/`): `leadpilot-architect`, `backend-developer`, `ai-pipeline-engineer`, `telegram-integration-engineer`,
  `cabinet-frontend-developer`, `security-reviewer`, `qa-tester`, `spec-reviewer`.
- **Скиллы** (`.claude/skills/`): `tenant-isolation-check`, `ai-guardrails-check`, `telegram-webhook`, `add-endpoint`, `acceptance-check`, `stage-runbook`.
- **Плагины** (project scope, включены): pyright-lsp, code-review, security-guidance, commit-commands, supabase (плагин playwright отключён — вместо него MCP `playwright` из `.mcp.json`).
  Глобально: context7 (актуальные доки библиотек), frontend-design. Отключены в `.claude/settings.json` ради токенов (вернуть `true` по необходимости):
  vercel, pr-review-toolkit, feature-dev, code-simplifier, hookify, claude-md-management, semgrep.
- **MCP** (`.mcp.json`): `sqlite` (dev-БД), `fetch` (доки Telegram Bot API), `git`, `playwright` (E2E кабинета, через Яндекс.Браузер — Chrome не установлен); из плагинов — `supabase` (HTTP, нужна OAuth-авторизация).
