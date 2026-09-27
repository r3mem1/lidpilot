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
integrations/  каналы за общим контрактом integrations/base.py (ChannelClient, IncomingMessage): Telegram, VK; ядро импортирует только base
ai/            pipeline без знаний о БД и Telegram: normalize → classify → context → respond → validate
templates/ static/   кабинет (Jinja2)
```

## Жёсткие инварианты
1. **Мультитенантность (§8, §16):** маршрут никогда не берёт `business_id` из запроса «как есть» — только через `BusinessContext`
   (`services/access_service.py`). Нет доступа → **404, не 403**. Любой запрос к бизнес-данным фильтруется по `business_id`.
2. **AI не выдумывает** цены, адреса, сроки, скидки, свободные слоты (§6.6, §12.3). Свободное время называет только движок записи `ai/booking.py` — из БД и шаблоном, не LLM. Ответ уходит клиенту только после `ResponseValidator`;
   иначе `ESCALATE` менеджеру (§6.7). Данные из БД приоритетнее слов клиента.
3. **Секреты** только в `.env`/секрет-хранилище; не в коде, не в логах, не в ответах API (§16). `integrations.credentials_ref` — ссылка, не сам секрет.
   Токен бота компании хранится только зашифрованным (`enc:…`, `services/secret_store.py`), секрет webhook — только SHA-256;
   URL Bot API содержит токен → логгеры `httpx`/`httpcore` держать на WARNING, ошибки клиента канала — без URL.
4. **Сообщение не теряется (§18, §21):** сначала сохранить входящее, потом вызывать внешние API; webhook идемпотентен
   (по `external_message_id`/`update_id`), повторная обработка безопасна.
5. **Аудит и логи (§16–17):** каждое значимое событие → `system_logs` через `audit_service`; по логам должно быть видно,
   что случилось с конкретным сообщением и почему отправлен именно этот ответ.
6. После ручного ответа менеджера AI молчит в диалоге до «решено» (`conversations.handled_by_manager`); приоритет лида внутри открытого диалога не понижается.
   **Ответ на каждое сообщение** (решение заказчика 2026-09-27, отступление от §6.7): клиент всегда получает ответ — AI или шаблон `REPLY_*` из `ai/pipeline.py`;
   молчание — только при `handled_by_manager` или выключенных автоответах (`ai_auto_reply`). «Передаю администратору» — только если AI не понял запрос
   (сначала один переспрос). Серия сообщений — один ответ (`reply_debounce_seconds`); `message_service.ensure_replies` шлёт шаблон, если ответа нет 2 мин.
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
| 6 Admin-панель (`/admin`: обзор, компании, статус/тариф/trial, события, метрики, MRR) | ✅ реализован, `smoke_test_stage6.py`, `scripts/e2e_admin.py` |
| 7 Первый пилот | 🔄 в работе: прод на Render + Supabase, бэкап и restore-drill, Sentry (`monitoring.py`), срок хранения `system_logs`, эксплуатация — `docs/operations.md`; осталось — webhook на проде, 1–3 компании, приёмка §21 на проде |
| 8 SaaS-автоматизация (без платежей: оплата по счёту, продление в `/admin`) | ✅ реализован: срок подписки выключает AI (`services/subscription_service.py`), баннеры, блок «Тариф», чек-лист onboarding (`services/onboarding_service.py`), `smoke_test_stage8.py` |
| Вне ТЗ (§22): мастера и запись | ✅ роль MASTER, смены по датам, записи, AI-бронь по расписанию (`ai/booking.py`, время только из БД), уведомления мастеру; `routes/bookings.py`, `smoke_test_stage10.py` |
| 9 Масштабирование | ✅ канал VK (`integrations/vk.py`, `/webhooks/vk`), общий контракт каналов `integrations/base.py`, rate limit в общей БД (`rate_limit_counters`), `smoke_test_stage9.py` |

Не выходить за границы текущего этапа: не добавлять функции из «Не входит в MVP» (§19) — календарь, биллинг, несколько каналов, мобильное приложение.

## Команды
Windows: перед запуском тестов и сканеров `export PYTHONUTF8=1` (PowerShell: `$env:PYTHONUTF8=1`) — иначе кириллица/стрелки в выводе роняют скрипты (`charmap`).
```bash
pip install -r requirements.txt -r requirements-dev.txt
alembic upgrade head                   # миграции; новая: alembic revision --autogenerate -m "..."
uvicorn main:app --reload              # http://127.0.0.1:8000/docs
python smoke_test.py                   # 80 проверок этапа 1
python smoke_test_ai.py                # 151 проверка этапа 2
python smoke_test_stage3.py            # 149 проверок этапа 3 (+ серия сообщений, контроль ответа)
python smoke_test_stage4.py            # 121 проверка этапа 4
python smoke_test_stage5.py            # 181 проверка этапа 5
python smoke_test_stage6.py            # 134 проверки этапа 6
python smoke_test_stage7.py            # 36 проверок этапа 7 (Sentry, срок хранения логов)
python smoke_test_stage8.py            # 39 проверок этапа 8 (подписка, onboarding)
python smoke_test_stage9.py            # 71 проверка этапа 9 (VK, rate limit в БД)
python smoke_test_stage10.py           # 114 проверок: мастера, расписание, записи, AI-запись, уведомления
python scripts/seed_demo.py            # демо-данные для кабинета (только на dev-БД)
ruff check . && ruff format --check .  # стиль
pyright                                # типы (LSP-плагин pyright-lsp)
bandit -r . -x ./migrations,./smoke_test.py,./smoke_test_ai.py,./brag-output   # SAST
pip-audit -r requirements.txt          # уязвимости зависимостей
```
Тесты — самодостаточные скрипты `smoke_test*.py` (httpx + временная SQLite). Новый этап = новый `smoke_test_stageN.py` в том же стиле.
Токены: тесты и линтеры запускать через `python scripts/run_checks.py [stage4 …]` и `… lint [ruff|pyright|bandit|pip-audit]` —
печатают итог и находки (без фильтрации), полный вывод в `.test_logs/` (открывать при падении). bandit: 44 LOW в `smoke_test_stage3-10.py`/`scripts/` — известный шум.
Hook `scripts/hooks/ruff_after_edit.py` проверяет правленый `.py` ruff'ом. Большие файлы (`README.md`, `docs/manual-check.md`, `smoke_test_stage*.py`, `models.py`,
`services/message_service.py`) — Grep, затем Read с `offset/limit`; широкий поиск — субагенту `Explore`; БД — MCP `sqlite` точечным SELECT.

## Соглашения кода
- Комментарии и docstring — на русском, с указанием раздела ТЗ; имена — английские; `from __future__ import annotations`.
- Enum-значения в БД — строки (`UserRole`, `BusinessStatus`, …); новые типы событий — в `audit_service.EventType`.
- Пароли — Argon2id; токены — JWT в HttpOnly cookie (кабинет) или Bearer (API).
- Не добавлять endpoint'ы вне §11 без явного пометки «вне ТЗ» (как `ai/preview`, выключен флагом).

## Инструменты, настроенные для проекта
- **Агенты** (`.claude/agents/`, 9) и **скиллы** (`.claude/skills/`, 9: `tenant-isolation-check`, `ai-guardrails-check`, `telegram-webhook`, `add-endpoint`, `acceptance-check`, `stage-runbook`,
  `admin-panel` (этап 6), `prod-readiness` (этап 7), `add-channel` (этап 9)) — какие подключать на этапе, см. `stage-runbook`.
- **Плагины** (project scope, включены): pyright-lsp, code-review, security-guidance, commit-commands. Глобально: context7 (актуальные доки), frontend-design.
  Выключены в `.claude/settings.json` ради токенов (каждый включённый плагин/MCP добавляет описания в каждый ход; вернуть `true` к нужному этапу):
  **supabase (включить к этапу 7)**, vercel, pr-review-toolkit, feature-dev, code-simplifier, hookify, claude-md-management, semgrep. Хостинг/мониторинг/платежи — по выбору пользователя (`stage-runbook`).
- **MCP** (`.mcp.json`): `sqlite` (dev-БД), `fetch` (доки Bot API), `playwright` (E2E кабинета, через Яндекс.Браузер — Chrome не установлен). Git — через Bash (MCP `git` убран как дубль).
