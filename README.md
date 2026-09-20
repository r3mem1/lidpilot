# LeadPilot — этапы 1–5

Реализовано строго по ТЗ v1.0, раздел 20.

**Этап 1 «Локальное ядро»:** FastAPI-ядро, конфигурация через `.env`,
БД (SQLite на разработке / PostgreSQL в production), регистрация и авторизация,
роли OWNER / MANAGER / ADMIN с проверкой `business_id` на каждом защищённом
endpoint, компании и услуги.

**Этап 2 «AI pipeline»:** модуль `ai/` — нормализация, классификация intent и
priority (правила + LLM), контекст бизнеса из БД, генерация ответа только по
данным компании, структурированная проверка ответа и эскалации раздела 6.7.
LLM работает через OpenRouter: в `.env` достаточно задать `AI_API_KEY` и
`AI_MODEL` (например `openai/gpt-4o-mini`) при `AI_PROVIDER=openrouter`; другой
OpenAI-совместимый сервис — `AI_PROVIDER=openai_compatible` + `AI_API_BASE_URL`.
Режим `AI_PROVIDER=stub` позволяет работать локально без ключа.

**Этап 3 «Telegram»:** webhook `POST /webhooks/telegram` (проверка `secret_token`),
идемпотентное сохранение сообщений, обработка AI, отправка ответов, подключение
бота компании, повторная обработка после сбоев.

**Этап 4 «CRM-ядро»:** лиды создаются автоматически по классификации (приоритет
HOT/WARM/COLD, причина, статус, ответственный), ручной ответ менеджера клиенту в
Telegram, отметка «решено», клиенты и история их обращений.

**Этап 5 «Кабинет бизнеса»:** веб-интерфейс на Jinja2 (без SPA) для владельца и менеджера — обзор,
сообщения с ручным ответом, лиды, клиенты, услуги, настройки AI, сотрудники и приглашения,
интеграции, аналитика.

Панель ADMIN (этап 6) не реализована — в проекте присутствуют только файлы структуры Приложения A
с объявленными контрактами.

## Запуск

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # вставить в JWT_SECRET
# для локального HTTP выставить AUTH_COOKIE_SECURE=false

alembic upgrade head          # миграции схемы
uvicorn main:app --reload     # http://127.0.0.1:8000/docs
```

Первый администратор платформы (роль ADMIN) создаётся при старте из
`BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD`; через публичную
регистрацию роль ADMIN получить нельзя.

Быстрый старт без Alembic (только локально): `AUTO_CREATE_TABLES=true`.

## Проверка

```bash
pip install httpx
python smoke_test.py       # 80 проверок этапа 1: роли, изоляция компаний, аудит, rate limit
python smoke_test_ai.py    # 141 проверка этапа 2: классификация, валидатор, эскалации, логи
python smoke_test_stage3.py  # 139 проверок этапа 3: webhook, идемпотентность, сбои, изоляция
python smoke_test_stage4.py  # 121 проверка этапа 4: лиды, ручной ответ, «решено», клиенты
python smoke_test_stage5.py  # 181 проверка этапа 5: страницы и роли, XSS/CSRF/CSP, команда, аналитика
```

Пошаговая инструкция ручной проверки (Swagger, OpenRouter, настоящий Telegram) —
[docs/manual-check.md](docs/manual-check.md); журнал событий: `python scripts/show_logs.py`.

Скрипты — вспомогательные, частью приложения не являются. Сеть не нужна:
Telegram подменяется `httpx.MockTransport`, LLM — офлайн-режимом.

## AI pipeline (этап 2)

```
сообщение → normalize → intent + priority → контекст компании из БД
          → генерация ответа → валидация → SEND либо ESCALATE
```

Ответ уходит клиенту только если прошёл валидатор. Диалог передаётся менеджеру
при жалобе, запросе записи, нехватке данных, спаме, ошибке LLM API и при любом
нарушении проверки (раздел 6.7 ТЗ).

Валидатор сверяет ответ с БД: денежные суммы должны существовать в прайсе
(или быть суммой реальных цен), запрещены обещания записи и свободного времени,
необъявленные скидки, чужие телефоны и адреса, утечка инструкций.

Проверить настройки AI без Telegram (включив `AI_PREVIEW_ENABLED=true`):

```bash
curl -X POST http://127.0.0.1:8000/businesses/1/ai/preview \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"text": "Сколько стоит стрижка и борода?"}'
```

## Telegram (этап 3)

1. Создайте бота у `@BotFather`, получите токен.
2. Нужен публичный HTTPS-адрес: в production — адрес хостинга, локально —
   `cloudflared tunnel --url http://localhost:8000` (или ngrok). Впишите его в
   `PUBLIC_BASE_URL` в `.env`.
3. Владелец компании подключает бота (токен вводится один раз и хранится в БД
   зашифрованным, в `.env` его нет):

```bash
curl -X POST http://127.0.0.1:8000/businesses/1/integrations/telegram \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"bot_token": "123456:ABC..."}'
```

Приложение проверит токен (`getMe`), сохранит интеграцию и зарегистрирует webhook
с секретным заголовком `X-Telegram-Bot-Api-Secret-Token`. Сообщения клиента
сохраняются в БД до любых внешних вызовов; ответ AI (или безопасный ответ при
передаче менеджеру) отправляется после ответа Telegram. Сбой AI, БД или Telegram
не теряет сообщение: оно обрабатывается повторно фоновым циклом
(`REPROCESS_INTERVAL_SECONDS`), а после `MESSAGE_MAX_ATTEMPTS` неудач диалог
получает статус «требует внимания».

| Метод  | Путь                                         | Доступ                               |
|--------|----------------------------------------------|--------------------------------------|
| POST   | `/webhooks/telegram`                         | Telegram (секретный заголовок)       |
| GET    | `/businesses/{id}/conversations`             | OWNER, MANAGER, ADMIN; фильтры `status`, `priority`, `date_from`, `date_to` |
| GET    | `/conversations/{id}`                        | OWNER, MANAGER, ADMIN                |
| GET    | `/businesses/{id}/integrations`              | OWNER, ADMIN (вне §11)               |
| POST   | `/businesses/{id}/integrations/telegram`     | OWNER, ADMIN (вне §11)               |
| DELETE | `/businesses/{id}/integrations/telegram`     | OWNER, ADMIN (вне §11)               |

## Кабинет бизнеса (этап 5)

Запуск: `uvicorn main:app --reload`, затем откройте <http://127.0.0.1:8000> — откроется вход.
Чтобы посмотреть кабинет без Telegram и реальных клиентов, создайте демо-данные:

```bash
python scripts/seed_demo.py     # владелец demo@example.com и менеджер manager@example.com, пароль Demo-Pass-123
```

| Раздел | Адрес | Кто видит |
|---|---|---|
| Обзор: показатели, очередь «нужен человек», новые горячие лиды | `/cabinet/{id}` | владелец, менеджер |
| Сообщения: список диалогов с фильтрами и переписка, ручной ответ, «решено» | `/cabinet/{id}/messages` | владелец, менеджер |
| Лиды: фильтры по приоритету, статусу, ответственному, периоду | `/cabinet/{id}/leads` | владелец, менеджер |
| Клиенты и история обращений | `/cabinet/{id}/customers` | владелец, менеджер |
| Услуги (менеджер — только просмотр) | `/cabinet/{id}/services` | владелец, менеджер |
| AI: правила, стиль ответов, автоответы, проверка ответа | `/cabinet/{id}/ai` | владелец |
| Сотрудники: роли, удаление, приглашения по ссылке | `/cabinet/{id}/team` | владелец |
| Настройки компании и подключение Telegram | `/cabinet/{id}/settings` | владелец |
| Аналитика за период | `/cabinet/{id}/analytics` | владелец |

Чужая компания — 404, недостаточная роль — страница «Недостаточно прав». Приглашение сотрудника —
одноразовая ссылка (письма система не шлёт): владелец копирует её и передаёт сам; принять её может
только человек с той же почтой.

Настройки AI (раздел 13): **стиль ответов** (дружелюбный, официальный, краткий) меняет манеру речи, но не
ослабляет запреты на выдуманные цены и обещания записи; **автоответы** можно выключить — тогда ассистент
только классифицирует обращения, а отвечает менеджер.

Безопасность интерфейса: экранирование всего клиентского текста, строгий CSP (только свои скрипты, стили
и шрифты, без inline), защита от CSRF проверкой источника запроса для cookie-сессий, заголовки
`X-Frame-Options`, `X-Content-Type-Options`, `no-store`. Изменения данных кабинет делает через тот же JSON API,
поэтому роли, изоляция компаний и аудит проверяются в одном месте.

Проверка в браузере (нужны `pip install playwright` и Edge или Chrome):
`python scripts/e2e_browser.py` — проходит основные сценарии и сохраняет скриншоты в `e2e-shots/`.

Новые endpoints (вне минимального списка §11, нужны для раздела 13):

| Метод | Путь | Доступ |
|---|---|---|
| PATCH / DELETE | `/businesses/{id}/members/{user_id}` | OWNER: роль / удаление (последнего владельца нельзя) |
| GET / POST | `/businesses/{id}/invitations` | OWNER |
| DELETE | `/businesses/{id}/invitations/{invitation_id}` | OWNER |
| POST | `/invitations/accept` | любой вошедший (нужна почта из приглашения) |
| GET | `/businesses/{id}/analytics` | OWNER; `date_from`, `date_to` |
| GET | `/businesses/{id}/inbox/state` | OWNER, MANAGER |

## CRM-ядро (этап 4)

**Лиды.** Каждое обращение клиента (кроме спама) становится лидом: один лид на диалог.
Приоритет внутри открытого диалога только растёт — «горячая» запись не остывает от
следующего сообщения «спасибо». Причина классификации хранится в лиде.

**Рабочее место менеджера.** Менеджер отвечает клиенту через API; после его ответа
**AI перестаёт отвечать в этом диалоге** и не перебивает человека — новые сообщения клиента
сохраняются, а диалог получает статус «требует внимания» (`CUSTOMER_REPLIED`). Кнопка
«решено» закрывает диалог и лид; следующее сообщение клиента открывает новый диалог, и AI
снова отвечает. Статусы лида: `NEW → IN_PROGRESS → RESOLVED | LOST`; закрытие лида
закрывает диалог, возврат в работу — переоткрывает.

| Метод  | Путь                                      | Доступ                    |
|--------|-------------------------------------------|---------------------------|
| GET    | `/businesses/{id}/leads`                  | OWNER, MANAGER, ADMIN; фильтры `priority`, `status`, `assigned_to`, `unassigned`, `date_from`, `date_to`, `limit`, `offset`; сначала горячие |
| PATCH  | `/leads/{id}`                             | OWNER, MANAGER, ADMIN (вне §11, раздел 14): `status`, `assigned_to` |
| POST   | `/conversations/{id}/reply`               | OWNER, MANAGER, ADMIN; тело `{"text": "..."}` |
| POST   | `/conversations/{id}/resolve`             | OWNER, MANAGER, ADMIN (вне §11, раздел 14) |
| GET    | `/businesses/{id}/customers`              | OWNER, MANAGER, ADMIN (вне §11, раздел 13); `search`, `limit`, `offset` |
| GET    | `/customers/{id}`                         | OWNER, MANAGER, ADMIN (вне §11, раздел 13): история обращений |

Ответ на `reply` возвращается со статусом 201, когда сообщение **сохранено**; результат
отправки показывает `delivery_status`: `SENT`, `PENDING` (временный сбой Telegram — повторит
фоновый цикл) или `FAILED` (например, клиент заблокировал бота).

## Endpoints этапа 1 (раздел 11 ТЗ)

| Метод  | Путь                                | Доступ                     |
|--------|-------------------------------------|----------------------------|
| POST   | `/auth/register`                    | публичный                  |
| POST   | `/auth/login`                       | публичный                  |
| POST   | `/auth/logout`                      | любой вошедший             |
| GET    | `/me`                               | любой вошедший             |
| POST   | `/businesses`                       | любой вошедший             |
| GET    | `/businesses/{id}`                  | OWNER, MANAGER, ADMIN      |
| PUT    | `/businesses/{id}`                  | OWNER, ADMIN               |
| GET    | `/businesses/{id}/services`         | OWNER, MANAGER, ADMIN      |
| POST   | `/businesses/{id}/services`         | OWNER, ADMIN               |
| PUT    | `/services/{id}`                    | OWNER, ADMIN               |
| DELETE | `/services/{id}`                    | OWNER, ADMIN               |
| GET    | `/businesses/{id}/members`          | OWNER, ADMIN               |
| POST   | `/businesses/{id}/members`          | OWNER, ADMIN               |
| POST   | `/businesses/{id}/ai/preview`       | OWNER, ADMIN; только при `AI_PREVIEW_ENABLED=true` |
| GET    | `/health`, `/`                      | публичный                  |

Данные другой компании недоступны: при отсутствии записи в `business_members`
возвращается `404` (не `403`), чтобы перебором id нельзя было узнать состав
компаний в системе. Попытка такого доступа пишется в `system_logs`.

## Структура

Соответствует Приложению A ТЗ. Добавлено сверх приложения:
`services/access_service.py` (RBAC-зависимости), `services/auth_service.py`,
`services/rate_limit_service.py`, `services/ai_service.py` (AI Service
раздела 8), `ai/context.py`, `ai/prompts.py`, `ai/llm_client.py`,
`ai/pipeline.py`, `services/integration_service.py`, `services/secret_store.py`,
`routes/integrations.py`, `routes/cabinet.py`, `routes/team.py`, `services/lead_service.py`,
`services/team_service.py`, `services/analytics_service.py`, `templating.py`, `cabinet_labels.py`,
`templates/`, `static/`, `scripts/`, `alembic.ini` + `migrations/`, `.env.example`, `.gitignore`,
`README.md`, `smoke_test.py`, `smoke_test_ai.py`, `smoke_test_stage3.py`, `smoke_test_stage4.py`, `smoke_test_stage5.py`.
