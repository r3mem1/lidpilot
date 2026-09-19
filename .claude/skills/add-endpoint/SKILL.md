---
name: add-endpoint
description: Пошаговый рецепт добавления нового endpoint/таблицы в LeadPilot в стиле проекта — модель, миграция Alembic, схемы, service, route, доступ через BusinessContext, аудит, тесты. Используй при реализации любого endpoint из ТЗ §11 (этапы 3–6).
---

# Добавление endpoint по слоям

Образец в коде: услуги — `routes/businesses.py` + `services/business_service.py`. Читай их перед началом.

1. **ТЗ.** Найди endpoint в §11 и таблицы в §10. Нет в §11 → пометь «вне ТЗ» и согласуй.
2. **Модель** (`models.py`): SQLAlchemy 2.0 `Mapped[]`, поля из §10, `business_id` (индексированный FK) в каждой таблице тенанта либо связь через `conversation`; `created_at` UTC; enum — строковые. Уникальные ограничения там, где нужна идемпотентность.
3. **Миграция:**
   ```bash
   alembic revision --autogenerate -m "stage4 conversations messages leads"
   # проверить вручную: индексы, FK, server_default, batch-режим для SQLite
   DATABASE_URL=sqlite:///./_mig.db alembic upgrade head && rm _mig.db
   ```
4. **Схемы** (`schemas.py`): отдельные `Create/Update/Read`; `Read` без секретов; в `Update` нет `business_id/role/status/owner_id`; ограничения длины и enum.
5. **Service** (`services/*_service.py`): принимает `Session` и данные, работает внутри переданной транзакции; все запросы фильтруются по `business_id`; исключения предметной области → route переводит в HTTP; значимое действие → `audit_service.log_event(db, event_type=..., business_id=..., ...)`; новый `EventType` добавь в класс.
6. **Route** (`routes/*.py`): тонкий; зависимость доступа из `access_service` (`BusinessContext` для `/businesses/{business_id}/...`; для `/services/{id}` — загрузи ресурс и проверь принадлежность); роль указана явно (OWNER/MANAGER/ADMIN); чужое → 404; `response_model` = `Read`-схема.
7. **Регистрация:** роутер уже подключён в `main.py`; новый файл роутера — добавь `include_router`.
8. **Тесты:** в `smoke_test*.py` — успех, чужая компания (404), недостаточная роль, без токена (401), невалидный ввод (422), запись в аудит.
9. **Проверка:**
   ```bash
   ruff check . && ruff format --check . && pyright
   python smoke_test.py && python smoke_test_ai.py
   ```
   Затем скилл `tenant-isolation-check`.
10. **Документация:** обнови статус этапа в `CLAUDE.md` и README, если этап закрыт.
