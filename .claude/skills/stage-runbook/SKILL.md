---
name: stage-runbook
description: Оркестрация разработки этапов 3–9 LeadPilot по ТЗ §20 — какие агенты, скиллы и проверки подключать на каждом этапе, порядок работ и определение «этап готов». Используй, когда просят «делаем этап N», «что дальше по ТЗ» или «спланируй разработку».
---

# Runbook этапов LeadPilot

Общий цикл каждого этапа: **план → реализация → тесты → ревью → приёмка → обновление CLAUDE.md**.

1. `leadpilot-architect` — план (схема БД, слои, доступ, отказоустойчивость). Утвердить с пользователем, если в плане есть отклонения от ТЗ.
2. Исполнители (см. этапы ниже) — по одному слою за раз, без выхода за scope этапа.
3. `qa-tester` — тесты и негативные сценарии (`smoke_test_stageN.py`), регрессия этапов 1–2.
4. `security-reviewer` + скилл `tenant-isolation-check` (`ai-guardrails-check`, если тронут `ai/`).
5. `spec-reviewer` (и/или `/code-review`) — соответствие ТЗ и качество.
6. Скилл `acceptance-check` — таблица §21. Затем `/simplify` для чистки; обновить `CLAUDE.md` (статус) и README; коммит через `/commit` (плагин commit-commands) — только по просьбе пользователя.

## По этапам
| Этап | Исполнители | Ключевые скиллы/инструменты | Готово, когда |
|---|---|---|---|
| **3 Telegram** | `telegram-integration-engineer` (+ `backend-developer` для таблиц integrations/messages) | `telegram-webhook`, MCP fetch (Bot API) | webhook принимает, отвечает, дубли безопасны, секреты скрыты; §21 п.2,3,4,8,15 |
| **4 CRM-ядро** | `backend-developer`, `ai-pipeline-engineer` (подключение pipeline к messages/ai_responses/leads) | `add-endpoint`, `tenant-isolation-check`, `ai-guardrails-check`, MCP sqlite | customers/conversations/messages/ai_responses/leads, фильтры по статусу/приоритету/периоду, ручной ответ; §21 п.5,6,10,11 |
| **5 Кабинет** | `cabinet-frontend-developer` (+ `backend-developer` для данных дашборда/аналитики) | `frontend-design`, Playwright MCP | страницы §13–14 работают, роли соблюдаются, XSS/CSRF закрыты; §21 п.9,11 |
| **6 Admin** | `backend-developer`, `cabinet-frontend-developer` | `tenant-isolation-check` (ADMIN-only), `add-endpoint` | §15: компании, статусы, тарифы, логи, метрики; §21 п.12,13 |
| **7 Пилот** | `security-reviewer`, `qa-tester` | `acceptance-check`, semgrep, bandit, pip-audit, supabase MCP (prod-БД), плагин vercel/render для деплоя | 1–3 реальные компании; HTTPS, prod-конфиг, бэкапы, мониторинг ошибок |
| **8 SaaS-автоматизация** | `leadpilot-architect` → `backend-developer` | context7 (SDK платёжного провайдера) | самостоятельная регистрация, trial, платежи, onboarding (§19: сложный биллинг вне MVP) |
| **9 Масштабирование** | `leadpilot-architect` | — | новый канал через `ChannelClient` без правок ядра, очереди, кэш, мониторинг |

## Правила
- Не начинать этап N+1, пока этап N не прошёл `acceptance-check` для своих пунктов.
- Каждый этап расширяет `EventType` в аудите и добавляет собственный smoke-тест; тесты прошлых этапов остаются зелёными.
- Внешние секреты (токен бота, ключ LLM, БД) вводит пользователь в `.env`; не запрашивать их в чате, не коммитить.
