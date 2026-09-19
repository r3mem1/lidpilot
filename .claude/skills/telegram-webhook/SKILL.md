---
name: telegram-webhook
description: Рецепт и чек-лист интеграции с Telegram Bot API для LeadPilot (этап 3) — webhook /webhooks/telegram, secret_token, идемпотентность, отправка, повторная обработка, тесты без сети. Используй при работе с integrations/telegram.py, routes/messages.py, services/message_service.py.
---

# Telegram webhook для LeadPilot (ТЗ §7, §11, §16–18, Приложение B)

Актуальные методы и поля сверяй по https://core.telegram.org/bots/api (MCP `fetch`/WebFetch) — не по памяти.

## Поток обработки
```
Telegram → POST /webhooks/telegram
  1. проверить X-Telegram-Bot-Api-Secret-Token (hmac.compare_digest) → иначе 401, ничего не обрабатывать
  2. разобрать Update; поддерживаем message.text; иное → лог + 200 (игнор)
  3. определить integration/business по секрету/боту → business_id
  4. get_or_create customer (business_id, external_id=chat.id), conversation
  5. сохранить message (sender_type=CUSTOMER, external_message_id=update_id/message_id) — идемпотентно
  6. вернуть 200 Telegram быстро
  7. AI pipeline (services/ai_service) → SEND: отправить и сохранить ai_response + message(AI)
                                    → ESCALATE: статус диалога «требует внимания», lead HOT/WARM/COLD с reason
  8. на каждом шаге system_logs (получение, ошибки, время ответа AI, отправка ок/неудача)
```

## Реализация
- **Секрет webhook:** генерируется на компанию (`secrets.token_urlsafe(32)`), передаётся в `setWebhook(secret_token=...)`, хранится в `integrations` (хеш или ссылка на секрет-хранилище), не возвращается API (§16). Сопоставление тенанта — по секрету либо по уникальному пути (`/webhooks/telegram/{integration_public_id}`; согласовать с §11 — базовый путь остаётся `POST /webhooks/telegram`).
- **Токен бота** входит в URL `https://api.telegram.org/bot<TOKEN>/method` → маскируй в логах, исключениях httpx и `repr`.
- **Клиент:** `TelegramClient.send_message` через `httpx` с таймаутом; обработка `429` (`parameters.retry_after`), `403` (бот заблокирован — пометить клиента, не ретраить), `5xx`/сетевых — ретрай с backoff, затем статус «не доставлено» + лог + задача менеджеру. Разбивать текст > 4096 символов.
- **Идемпотентность:** `UNIQUE(conversation_id, external_message_id)`; дубликат → `200` без повторной отправки ответа. Telegram повторяет при не-2xx — поэтому 5xx отдавать только если сообщение НЕ сохранено.
- **Повторная обработка (§18):** входящее сохранено, AI/отправка упали → сообщение остаётся в состоянии, из которого его можно перезапустить (статус/флаг, endpoint или фоновая задача `BackgroundTasks`), плюс событие в логе. Молча терять нельзя (§21, п.15).
- **Локально:** нужен публичный HTTPS — `cloudflared tunnel --url http://localhost:8000` или ngrok; затем `setWebhook`. Токен бота пользователь кладёт в `.env` сам; в чат и в git его не выкладывать.
- **Расширяемость (§1):** ядро зависит только от `ChannelClient` Protocol; парсинг Update — внутри `integrations/telegram.py`, наружу отдаётся нейтральный `IncomingMessage(channel, external_chat_id, external_message_id, text, sender_name)`.

## Тесты без сети
`httpx.MockTransport` для исходящих вызовов + `TestClient` для входящих. Кейсы: неверный секрет; валидное сообщение → сохранено, ответ отправлен один раз; тот же update повторно → без дубля; Telegram 500/429/403 при отправке; LLM ошибка → эскалация; не-текстовый апдейт; чужая компания не видит диалог. Файл — `smoke_test_stage3.py`.

## Готово, когда
Критерии §21 п. 2–4, 8, 15 подтверждены тестом; в `system_logs` по одному сообщению читается вся цепочка.
