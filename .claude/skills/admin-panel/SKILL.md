---
name: admin-panel
description: Рецепт и чек-лист этапа 6 LeadPilot — административная панель /admin (ТЗ §11, §15, §21 п.12–13) - список компаний, статусы active/trial/suspended, тарифы, логи, интеграции, метрики SaaS (MRR). Используй при любой работе с routes/admin.py, admin-шаблонами и admin-сервисом.
---

# Admin-панель (§15) — этап 6

Образцы в коде: `routes/cabinet.py` + `templates/` (кабинет), `services/analytics_service.py` (агрегаты), `services/audit_service.py`. Реализовано: JSON API — `routes/admin.py`, страницы — `routes/admin_pages.py` (адреса `/admin`, `/admin/companies`, `/admin/events` не пересекаются с API `/admin/businesses`, `/admin/logs`), логика — `services/admin_service.py`.

## Endpoint'ы (§11)
`GET /admin/businesses`, `GET /admin/logs`, `PUT /admin/businesses/{id}/status`. Всё остальное из §15 (тариф, trial, интеграции, метрики, пользователи) — либо страницы кабинета `/admin`, либо endpoint'ы с пометкой «вне ТЗ» в docstring.

## Обязательное
1. **Доступ:** каждый маршрут — `Depends(require_platform_admin)`; не-ADMIN → отказ, без ADMIN-данных в теле. ADMIN не создаётся публичной регистрацией (проверь, что `/auth/register` этого не допускает).
2. **ADMIN видит все компании**, но это единственное место, где нет фильтра по `business_id` — вынеси в отдельный `services/admin_service.py`, не размазывай «обход фильтра» по коду кабинета. `business_id` в `{id}` — из path, но только для ADMIN.
3. **Данные §15:** название, статус (`BusinessStatus`), тариф/trial (`subscriptions`), дата регистрации, число пользователей, число сообщений, последняя активность, интеграции (без `credentials_ref`-содержимого и токенов), системные ошибки (`system_logs` с level ≥ ERROR).
4. **Поиск/фильтры/пагинация** на стороне БД (не тянуть все компании в память): по названию, статусу, тарифу.
5. **Метрики SaaS:** число компаний, активные, trial, подписки, MRR. MRR считай по тарифам активных подписок; тарифная сетка — простой справочник (конфиг/константа), не биллинг (§19: сложный биллинг вне MVP).
6. **Смена статуса/тарифа/trial** → `audit_service.log_event` (`EventType`: `ADMIN_BUSINESS_STATUS_CHANGED`, `ADMIN_SUBSCRIPTION_CHANGED` — тариф, срок, trial) с актором, старым и новым значением. `suspended` реально блокирует: webhook компании отвечает 200, но AI/отправка не выполняются, событие в логе (сообщение не теряется, §18); кабинет владельца показывает причину.
7. **Логи:** фильтры level/event_type/business_id/период; в `metadata` секреты не показываются (маскировать при выдаче).
8. **Кабинет:** правила инварианта 7 (автоэкранирование, без `|safe`, без inline JS/CSS, изменения только через JSON API, проверка Origin).

## Тесты — `smoke_test_stage6.py`
Не-ADMIN (OWNER, MANAGER, аноним) → отказ на каждом `/admin/*`; ADMIN видит компании A и B; смена статуса пишет аудит; suspended-компания не получает AI-ответ, но входящее сохранено; секреты не в ответах; метрики совпадают с ручным подсчётом. Затем `tenant-isolation-check`, `acceptance-check` (п.12, 13) и `python scripts/run_checks.py`.
