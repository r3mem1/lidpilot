"""
VK — сообщения сообщества через Callback API (разделы 1, 22 ТЗ, этап 9).

Контракт ядра — integrations/base.py; здесь только VK-специфика: формат событий
Callback API, методы API (groups.*, messages.send), коды ошибок и лимиты.
Документация: dev.vk.com/ru/api/callback/getting-started, версия API 5.199.

Callback API:
* type=confirmation — сервер отвечает строкой подтверждения (одна на сервер);
* остальные события — ответ строкой «ok» и HTTP 200, иначе VK повторяет доставку
  (через 10 с, 3, 10, 30 мин, 1 ч) и после серии ошибок перестаёт слать события;
* подлинность — поле secret в теле события (секретный ключ сервера).

Безопасность (раздел 16): ключ доступа сообщества передаётся в заголовке
Authorization, а не в URL; в тексты исключений и логи он не попадает.

Отказоустойчивость (раздел 18): «слишком много запросов», внутренняя ошибка VK,
5xx и сетевые ошибки повторяются с ограничением; запрет на сообщения от
сообщества (900/901/902) — «клиент заблокировал», без повторов.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from config import settings
from integrations.base import ChannelError, ChannelSendError, IncomingMessage, split_text

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger("leadpilot.vk")

CHANNEL = "VK"
API_VERSION = "5.199"
MAX_MESSAGE_CHARS = 4000  # лимит API 9000; короче — читабельнее в диалоге
SERVER_TITLE = "LeadPilot"  # groups.addCallbackServer: не длиннее 14 символов
# Ключ доступа сообщества: без пробелов, разумной длины (формат VK менялся — vk1.a.…).
COMMUNITY_TOKEN_RE = re.compile(r"^[A-Za-z0-9._\-]{40,400}$")
_PEER_CHAT_OFFSET = 2_000_000_000  # peer_id бесед; обрабатываем только личные диалоги

# Коды ошибок API: повторяемые и «пользователь запретил сообщения».
_RETRYABLE_CODES = {1, 6, 9, 10}
_BLOCKED_CODES = {900, 901, 902}
_ERROR_HINTS = {
    5: "ключ доступа недействителен или отозван",
    15: "нет доступа: ключу не хватает прав",
    27: "нужен ключ доступа сообщества, а не пользователя",
    912: "в сообществе выключены возможности ботов",
    2000: "в сообществе уже 10 серверов Callback API",
}


# --------------------------------------------------------------------------- #
# Разбор события Callback API
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class VkEvent:
    """Разобранное событие: kind — confirmation | message | deny | allow | ignored."""

    kind: str
    group_id: str | None
    secret: str | None
    event_id: str | None = None
    message: IncomingMessage | None = None
    user_id: str | None = None  # для deny/allow
    reason: str | None = None  # почему событие не обрабатывается


def parse_event(event: dict[str, Any]) -> VkEvent:
    """JSON Callback API → VkEvent. Неподдерживаемое — kind=ignored, без исключений:
    на любой ответ, кроме «ok», VK повторял бы доставку."""
    kind = event.get("type")
    group_id = event.get("group_id")
    secret = event.get("secret")
    base: dict[str, Any] = {
        "group_id": str(group_id) if isinstance(group_id, int) else None,
        "secret": secret if isinstance(secret, str) else None,
        "event_id": str(event.get("event_id")) if event.get("event_id") is not None else None,
    }
    if kind == "confirmation":
        return VkEvent(kind="confirmation", **base)

    obj = event.get("object")
    obj = obj if isinstance(obj, dict) else {}
    if kind in ("message_deny", "message_allow"):
        user_id = obj.get("user_id")
        if not isinstance(user_id, int):
            return VkEvent(kind="ignored", reason="no_user_id", **base)
        return VkEvent(
            kind="deny" if kind == "message_deny" else "allow", user_id=str(user_id), **base
        )
    if kind != "message_new":
        return VkEvent(kind="ignored", reason="unsupported_event_type", **base)

    message = obj.get("message")  # API ≥ 5.103: object = {message, client_info}
    if not isinstance(message, dict):
        return VkEvent(kind="ignored", reason="no_message", **base)
    peer_id, from_id = message.get("peer_id"), message.get("from_id")
    if not isinstance(peer_id, int) or not isinstance(from_id, int):
        return VkEvent(kind="ignored", reason="no_peer", **base)
    if peer_id >= _PEER_CHAT_OFFSET:
        return VkEvent(kind="ignored", reason="not_private_chat", **base)
    if from_id <= 0 or from_id != peer_id:
        return VkEvent(kind="ignored", reason="not_user_sender", **base)

    msg_id = message.get("id")
    conv_msg_id = message.get("conversation_message_id")
    if isinstance(msg_id, int) and msg_id > 0:
        external_id = str(msg_id)
    elif isinstance(conv_msg_id, int):
        external_id = f"c{conv_msg_id}"
    else:
        return VkEvent(kind="ignored", reason="no_message_id", **base)

    text = message.get("text")
    attachments = message.get("attachments")
    if isinstance(text, str) and text.strip():
        content_type, body = "text", text
    elif isinstance(attachments, list) and attachments:
        first = attachments[0] if isinstance(attachments[0], dict) else {}
        content_type, body = "attachment", f"[вложение: {first.get('type') or 'файл'}]"
    else:
        return VkEvent(kind="ignored", reason="unsupported_message", **base)

    return VkEvent(
        kind="message",
        message=IncomingMessage(
            channel=CHANNEL,
            external_chat_id=str(peer_id),
            external_message_id=external_id,
            text=body,
            sender_name=None,  # имени клиента в событии нет; в кабинете — ссылка id<номер>
            sender_username=f"id{from_id}",
            content_type=content_type,
        ),
        **base,
    )


# --------------------------------------------------------------------------- #
# Клиент API
# --------------------------------------------------------------------------- #
class VkClient:
    """Клиент VK API с ключом доступа сообщества (синхронный, как маршруты FastAPI)."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_id: Callable[[], int] = lambda: secrets.randbelow(2**31 - 1) + 1,
    ) -> None:
        self._token = token
        self._base_url = (base_url or settings.vk_api_base_url).rstrip("/")
        self._timeout = timeout if timeout is not None else settings.vk_timeout_seconds
        self._max_retries = max_retries if max_retries is not None else settings.vk_max_retries
        self._transport = transport
        self._sleep = sleep
        self._random_id = random_id

    def __repr__(self) -> str:  # ключ не должен появиться в repr/логах
        return "VkClient(token=***)"

    # -- сообщество и Callback API ------------------------------------------ #
    def get_group(self) -> dict[str, Any]:
        """Сообщество, которому принадлежит ключ: {id, name, screen_name}."""
        result = self._call("groups.getById", {})
        groups = result.get("groups") if isinstance(result, dict) else result  # ≥5.139: {groups}
        if not isinstance(groups, list) or not groups or not isinstance(groups[0], dict):
            raise ChannelError("VK не вернул данные сообщества")
        return groups[0]

    def get_confirmation_code(self, group_id: str) -> str:
        result = self._call("groups.getCallbackConfirmationCode", {"group_id": group_id})
        code = result.get("code") if isinstance(result, dict) else None
        if not isinstance(code, str) or not code:
            raise ChannelError("VK не вернул строку подтверждения сервера")
        return code

    def add_callback_server(self, group_id: str, url: str, secret_key: str) -> str:
        result = self._call(
            "groups.addCallbackServer",
            {"group_id": group_id, "url": url, "title": SERVER_TITLE, "secret_key": secret_key},
        )
        server_id = result.get("server_id") if isinstance(result, dict) else None
        if not isinstance(server_id, int):
            raise ChannelError("VK не вернул идентификатор сервера Callback API")
        return str(server_id)

    def set_callback_settings(self, group_id: str, server_id: str) -> None:
        self._call(
            "groups.setCallbackSettings",
            {
                "group_id": group_id,
                "server_id": server_id,
                "api_version": API_VERSION,
                "message_new": 1,
                "message_allow": 1,
                "message_deny": 1,
            },
        )

    def delete_callback_server(self, group_id: str, server_id: str) -> None:
        self._call("groups.deleteCallbackServer", {"group_id": group_id, "server_id": server_id})

    # -- сообщения ------------------------------------------------------------ #
    def send_message(self, chat_id: str, text: str, buttons: list[str] | None = None) -> str:
        """Отправить текст клиенту. Длинный текст режется на части; random_id
        защищает от дубля при повторе запроса после сетевой ошибки. buttons —
        текстовые кнопки (one_time) под последним фрагментом: нажатие приходит
        обычным сообщением с текстом кнопки."""
        chunks = split_text(text, MAX_MESSAGE_CHARS)
        if not chunks:
            raise ChannelSendError("Пустой текст сообщения")
        last_id = ""
        for i, chunk in enumerate(chunks):
            params: dict[str, Any] = {
                "peer_id": chat_id,
                "message": chunk,
                "random_id": self._random_id(),
            }
            if buttons and i == len(chunks) - 1:
                params["keyboard"] = json.dumps(
                    {
                        "one_time": True,
                        "buttons": [
                            [
                                {
                                    "action": {"type": "text", "label": b[:40]},
                                    "color": "positive" if n == 0 else "secondary",
                                }
                                for n, b in enumerate(buttons)
                            ]
                        ],
                    },
                    ensure_ascii=False,
                )
            result = self._call("messages.send", params)
            last_id = str(result)
        return last_id

    # -- транспорт ------------------------------------------------------------ #
    def _call(self, method: str, params: dict[str, Any]) -> Any:
        url = f"{self._base_url}/method/{method}"
        data = {**{k: str(v) for k, v in params.items()}, "v": API_VERSION}
        headers = {"Authorization": f"Bearer {self._token}"}
        last_error: ChannelSendError | None = None

        for attempt in range(1, self._max_retries + 2):
            try:
                with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                    response = client.post(url, data=data, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = ChannelSendError(
                    f"Сетевая ошибка VK ({type(exc).__name__})", retryable=True
                )
                logger.warning("VK %s: сетевая ошибка (попытка %s)", method, attempt)
                self._backoff(attempt)
                continue

            if response.status_code >= 500:
                last_error = ChannelSendError(
                    f"VK недоступен ({response.status_code})",
                    retryable=True,
                    status_code=response.status_code,
                )
                logger.warning("VK %s: HTTP %s (попытка %s)", method, response.status_code, attempt)
                self._backoff(attempt)
                continue

            payload = self._safe_json(response)
            if "response" in payload:
                return payload["response"]

            raw_error = payload.get("error")
            error: dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
            code = error.get("error_code")
            code = code if isinstance(code, int) else response.status_code
            text = str(error.get("error_msg") or f"HTTP {response.status_code}")[:200]
            hint = _ERROR_HINTS.get(code)
            description = f"{text} ({hint})" if hint else text

            if code in _RETRYABLE_CODES:
                last_error = ChannelSendError(
                    f"VK: временная ошибка {code}: {description}", retryable=True, status_code=code
                )
                logger.warning("VK %s: ошибка %s (попытка %s)", method, code, attempt)
                self._backoff(attempt)
                continue
            if code in _BLOCKED_CODES:
                raise ChannelSendError(
                    f"VK запретил отправку: {description}", blocked_by_user=True, status_code=code
                )
            raise ChannelSendError(f"VK отклонил запрос {method}: {description}", status_code=code)

        raise last_error or ChannelSendError("VK недоступен", retryable=True)

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
