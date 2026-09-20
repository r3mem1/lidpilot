---
name: prod-readiness
description: Чек-лист готовности к пилоту LeadPilot (ТЗ §16–18, §20 этап 7) - PostgreSQL/Supabase, prod-конфиг и секреты, HTTPS, webhook на публичном URL, rate limit, бэкапы, мониторинг, отказоустойчивость. Используй перед деплоем, подключением реальных компаний и при переходе с SQLite на PostgreSQL.
---

# Готовность к пилоту (этап 7)

Ответственный: `devops-engineer` (+ `security-reviewer` для финальной проверки). Всё необратимое — только после подтверждения пользователя.

## 1. БД: SQLite → PostgreSQL/Supabase
- `DATABASE_URL` из окружения; драйвер в `requirements.txt` (psycopg), pooler Supabase (порт 6543 / transaction mode) — учесть в настройках engine (`pool_pre_ping`, отсутствие prepared statements при pgbouncer).
- `alembic upgrade head` на **чистой** PostgreSQL проходит; `downgrade -1` → `upgrade head` тоже. Проверить: типы дат (timezone-aware), JSON-поля (`metadata` в `system_logs`), enum-значения-строки, `server_default`, уникальные индексы идемпотентности (`external_message_id`/`update_id`) и составные индексы с `business_id`.
- Скилл `supabase:supabase-postgres-best-practices` перед изменением схемы/индексов; MCP `supabase`: `get_advisors` (security/performance) после миграции. Помни: RLS — дополнительный слой, не замена фильтра `business_id` в сервисах.
- Бэкапы: включены и **проверено восстановление** на тестовой БД; периодичность и срок хранения записать в `docs/`.

## 2. Конфиг и секреты (§16)
- Заданы отдельно: `JWT_SECRET`, `SECRETS_ENCRYPTION_KEY` (≥32 символов, не выводится из JWT_SECRET), ключ LLM, `DATABASE_URL`, `DEBUG=false`, `TRUSTED_PROXY_COUNT` = числу прокси хостинга (иначе rate limit видит IP прокси).
- `.env` не в git; `.env.example` — только заглушки; в логах и ответах API секретов нет (grep по `token`, `secret`, `password` в логах прогона); логгеры `httpx`/`httpcore` на WARNING.
- Смена `SECRETS_ENCRYPTION_KEY` делает старые `enc:…` токены нечитаемыми — план ротации до пилота.

## 3. Сеть и запуск
- HTTPS на границе (HSTS уже в `main.py`), запуск `uvicorn`/`gunicorn` без `--reload`, `/health` для проб хостинга (БД-проба — отдельно, без утечки деталей).
- **Rate limit в памяти процесса** (`services/rate_limit_service.py`): при >1 воркере/реплике лимит не общий. На пилоте — 1 воркер или лимит на прокси; Redis — этап 9. Зафиксировать выбор в `docs/`.
- CSP/Origin-проверка кабинета работают на боевом домене (Origin = публичный хост).

## 4. Telegram на публичном URL
`setWebhook` на реальный HTTPS-URL с `secret_token` (в БД — только SHA-256) — **только по подтверждению пользователя**, токен бота вводит он сам (не в чат). Проверка: неверный secret → отказ; дубль `update_id` → без второго ответа; сбой отправки → сообщение сохранено, событие в логе; повторная обработка после временной ошибки (§18, §21 п.14).

## 5. Мониторинг и эксплуатация (§17)
- Алерт/просмотр ошибок: `system_logs` (ERROR) в admin-панели; внешний трекер ошибок (например Sentry) — по выбору пользователя, без PII клиентов в событиях.
- Ретеншн `system_logs` (рост таблицы), сбои LLM/Telegram видны как события, а не молчат.
- Процедура отката релиза и восстановления БД описана.

## 6. Финал
`python scripts/run_checks.py` и `… lint` зелёные (`pip-audit` без критичных уязвимостей) → `security-reviewer` (вердикт «допускается к пилоту») → `acceptance-check` по всем 15 пунктам §21 на боевом окружении. Плагины хостинга/мониторинга включай по решению пользователя (см. `stage-runbook`).
