"""
Telegram Bot API — разделы 1, 11 и 19 ТЗ (этап 3).

Ядро системы знает только общий контракт каналов (integrations/base.py);
всё, что относится к Telegram (формат Update, методы Bot API, коды ошибок),
остаётся в этом модуле. Другие каналы (VK — integrations/vk.py) добавляются
отдельным модулем без переписывания ядра (раздел 1).

Безопасность (раздел 16):
* токен бота входит в URL запроса (…/bot<TOKEN>/method), поэтому он не попадает
  ни в логи, ни в тексты исключений: httpx-исключения заменяются обезличенными,
  а логгеры httpx/httpcore переведены на WARNING (иначе INFO-строка
  «HTTP Request: POST https://…/bot<TOKEN>/…» уходила бы в консоль);
* подлинность webhook проверяется на стороне маршрута по secret_token.

Отказоустойчивость (раздел 18): 429 (retry_after), 5xx и сетевые ошибки
повторяются с ограничением; 403 (бот заблокирован) и 4xx — нет.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from config import settings
from integrations.base import (
    ChannelClient,
    ChannelError,
    ChannelSendError,
    IncomingMessage,
)
from integrations.base import split_text as _split_text

# httpx на уровне INFO печатает полный URL запроса, а в нём токен бота.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("leadpilot.telegram")

CHANNEL = "TELEGRAM"
MAX_MESSAGE_CHARS = 4096  # лимит Bot API на длину одного сообщения
_MAX_RETRY_AFTER_SECONDS = 10.0  # дольше воркер не ждём: сообщение уйдёт при повторе
BOT_TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,80}$")

# Поля Message, означающие вложение: AI их не видит, решает человек.
_ATTACHMENT_KEYS = (
    "photo", "document", "voice", "audio", "video", "video_note", "sticker",
    "animation", "location", "contact", "venue", "poll",
)  # fmt: skip


# --------------------------------------------------------------------------- #
# Разбор входящего Update
# --------------------------------------------------------------------------- #
def parse_update(update: dict[str, Any]) -> tuple[IncomingMessage | None, str | None]:
    """Update Bot API → (IncomingMessage, None) либо (None, причина игнорирования).

    Поддерживаются личные сообщения (message). Правки, группы, каналы и
    служебные апдейты не обрабатываются, но не приводят к ошибке: Telegram
    повторял бы доставку при любом не-2xx ответе.
    """
    message = update.get("message")
    if not isinstance(message, dict):
        return None, "unsupported_update_type"

    chat = message.get("chat")
    sender = message.get("from")
    if not isinstance(chat, dict) or chat.get("id") is None:
        return None, "no_chat"
    if chat.get("type") != "private":
        return None, "not_private_chat"
    if isinstance(sender, dict) and sender.get("is_bot"):
        return None, "bot_sender"
    if message.get("message_id") is None:
        return None, "no_message_id"

    text = message.get("text")
    caption = message.get("caption")
    if isinstance(text, str) and text.strip():
        content_type, body = "text", text
    elif any(key in message for key in _ATTACHMENT_KEYS):
        kind = next(key for key in _ATTACHMENT_KEYS if key in message)
        content_type = "attachment"
        body = caption if isinstance(caption, str) and caption.strip() else f"[вложение: {kind}]"
    else:
        return None, "unsupported_message"

    sender = sender if isinstance(sender, dict) else {}
    name = " ".join(
        part
        for part in (sender.get("first_name"), sender.get("last_name"))
        if isinstance(part, str)
    ).strip()
    username = sender.get("username")

    update_id = update.get("update_id")
    return (
        IncomingMessage(
            channel=CHANNEL,
            external_chat_id=str(chat["id"]),
            external_message_id=str(message["message_id"]),
            text=body,
            sender_name=name or None,
            sender_username=username if isinstance(username, str) else None,
            content_type=content_type,
            update_id=update_id if isinstance(update_id, int) else None,
        ),
        None,
    )


def split_text(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Разбить длинный текст на фрагменты под лимит Bot API."""
    return _split_text(text, limit)


# --------------------------------------------------------------------------- #
# Клиент Bot API
# --------------------------------------------------------------------------- #
class TelegramClient:
    """Клиент Telegram Bot API поверх httpx (синхронный: маршруты FastAPI — sync)."""

    def __init__(
        self,
        bot_token: str,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._bot_token = bot_token
        self._base_url = (base_url or settings.telegram_api_base_url).rstrip("/")
        self._timeout = timeout if timeout is not None else settings.telegram_timeout_seconds
        self._max_retries = (
            max_retries if max_retries is not None else settings.telegram_max_retries
        )
        self._transport = transport
        self._sleep = sleep

    def __repr__(self) -> str:  # токен не должен появиться в repr/логах
        return "TelegramClient(bot_token=***)"

    # -- публичный API ---------------------------------------------------- #
    def get_me(self) -> dict[str, Any]:
        return self._call("getMe", {})

    def set_webhook(self, url: str, secret_token: str) -> None:
        self._call(
            "setWebhook",
            {"url": url, "secret_token": secret_token, "allowed_updates": ["message"]},
        )

    def delete_webhook(self) -> None:
        self._call("deleteWebhook", {})

    def send_message(self, chat_id: str, text: str) -> str:
        """Отправить текст без parse_mode (клиентский и AI-текст не должны
        интерпретироваться как разметка). Длинный текст режется на части."""
        chunks = split_text(text)
        if not chunks:
            raise ChannelSendError("Пустой текст сообщения")
        last_id = ""
        for chunk in chunks:
            result = self._call("sendMessage", {"chat_id": chat_id, "text": chunk})
            last_id = str(result.get("message_id", ""))
        return last_id

    # -- транспорт -------------------------------------------------------- #
    def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base_url}/bot{self._bot_token}/{method}"
        last_error: ChannelSendError | None = None

        for attempt in range(1, self._max_retries + 2):
            try:
                with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                    response = client.post(url, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                # Сообщение httpx содержит URL с токеном — наружу только тип ошибки.
                last_error = ChannelSendError(
                    f"Сетевая ошибка Telegram ({type(exc).__name__})", retryable=True
                )
                logger.warning("Telegram %s: сетевая ошибка (попытка %s)", method, attempt)
                self._backoff(attempt)
                continue

            data = self._safe_json(response)
            if response.status_code == 200 and data.get("ok") is True:
                result = data.get("result")
                return result if isinstance(result, dict) else {}

            description = str(data.get("description") or f"HTTP {response.status_code}")[:200]
            code = int(data.get("error_code") or response.status_code)

            if code == 429:
                retry_after = self._retry_after(data)
                last_error = ChannelSendError(
                    f"Telegram: слишком много запросов ({description})",
                    retryable=True,
                    status_code=code,
                )
                logger.warning(
                    "Telegram %s: 429, пауза %.1f с (попытка %s)", method, retry_after, attempt
                )
                if attempt <= self._max_retries:
                    self._sleep(retry_after)
                continue
            if code >= 500:
                last_error = ChannelSendError(
                    f"Telegram недоступен ({code})", retryable=True, status_code=code
                )
                logger.warning("Telegram %s: HTTP %s (попытка %s)", method, code, attempt)
                self._backoff(attempt)
                continue
            if code == 403:
                # Клиент заблокировал бота или бот удалён из чата: повторять бессмысленно.
                raise ChannelSendError(
                    f"Telegram запретил отправку: {description}",
                    blocked_by_user=True,
                    status_code=code,
                )
            raise ChannelSendError(
                f"Telegram отклонил запрос {method}: {description}", status_code=code
            )

        raise last_error or ChannelSendError("Telegram недоступен", retryable=True)

    def _backoff(self, attempt: int) -> None:
        if attempt <= self._max_retries:
            self._sleep(min(0.5 * (2 ** (attempt - 1)), 4.0))

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _retry_after(data: dict[str, Any]) -> float:
        parameters = data.get("parameters")
        value = parameters.get("retry_after") if isinstance(parameters, dict) else None
        seconds = float(value) if isinstance(value, (int, float)) else 1.0
        return max(0.0, min(seconds, _MAX_RETRY_AFTER_SECONDS))


__all__ = [
    "BOT_TOKEN_RE",
    "CHANNEL",
    "ChannelClient",
    "ChannelError",
    "ChannelSendError",
    "IncomingMessage",
    "TelegramClient",
    "parse_update",
    "split_text",
]
