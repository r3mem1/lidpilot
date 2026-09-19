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
6. Роль проверяется на **каждом** защищённом endpoint; `/admin/*` — только ADMIN (`require_platform_admin`); ADMIN не создаётся публичной регистрацией.

## Статус этапов (§20)
| Этап | Состояние |
|---|---|
| 1 Локальное ядро (auth, RBAC, компании, услуги) | ✅ реализован, `smoke_test.py` |
| 2 AI pipeline | ✅ реализован, `smoke_test_ai.py` |
| 3 Telegram (webhook, отправка) | ✅ реализован, `smoke_test_stage3.py` |
| 4 CRM-ядро (лиды, статусы, ручной ответ, фильтры) | ⏳ таблицы customers/conversations/messages/ai_responses уже есть (этап 3); нет `leads`, `POST /conversations/{id}/reply`, смены статусов |
| 5 Кабинет бизнеса (dashboard, сообщения, услуги, настройки, аналитика) | ⏳ шаблоны-заготовки |
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
ruff check . && ruff format --check .  # стиль
pyright                                # типы (LSP-плагин pyright-lsp)
bandit -r . -x ./migrations,./smoke_test.py,./smoke_test_ai.py   # SAST
pip-audit -r requirements.txt          # уязвимости зависимостей
```
Тесты — самодостаточные скрипты `smoke_test*.py` (httpx + временная SQLite). Новый этап = новый `smoke_test_stageN.py` в том же стиле.

## Соглашения кода
- Комментарии и docstring — на русском, с указанием раздела ТЗ; имена — английские; `from __future__ import annotations`.
- Enum-значения в БД — строки (`UserRole`, `BusinessStatus`, …); новые типы событий — в `audit_service.EventType`.
- Пароли — Argon2id; токены — JWT в HttpOnly cookie (кабинет) или Bearer (API).
- Не добавлять endpoint'ы вне §11 без явного пометки «вне ТЗ» (как `ai/preview`, выключен флагом).

## Инструменты, настроенные для проекта
- **Агенты** (`.claude/agents/`): `leadpilot-architect`, `backend-developer`, `ai-pipeline-engineer`, `telegram-integration-engineer`,
  `cabinet-frontend-developer`, `security-reviewer`, `qa-tester`, `spec-reviewer`.
- **Скиллы** (`.claude/skills/`): `tenant-isolation-check`, `ai-guardrails-check`, `telegram-webhook`, `add-endpoint`, `acceptance-check`, `stage-runbook`.
- **Плагины** (project scope): pyright-lsp, code-review, pr-review-toolkit, security-guidance, feature-dev, code-simplifier,
  commit-commands, claude-md-management, hookify, supabase, playwright, semgrep. Глобально: context7 (актуальные доки библиотек), frontend-design, vercel.
- **MCP** (`.mcp.json`): `sqlite` (dev-БД), `fetch` (доки Telegram Bot API), `git`; из плагинов — `supabase` (HTTP, нужна OAuth-авторизация), `playwright` (E2E кабинета).
