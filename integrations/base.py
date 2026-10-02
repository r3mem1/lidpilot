"""
Общий контракт каналов коммуникации — разделы 1, 11 ТЗ, этап 9.

Ядро (services/, ai/) знает только эти типы: нейтральное входящее сообщение,
ошибки канала и Protocol клиента. Всё канал-специфичное — формат входящих
событий, методы API, коды ошибок, лимиты — живёт в модуле канала
(integrations/telegram.py, integrations/vk.py). Новый канал = новый модуль
с тем же контрактом, без правок ядра (раздел 1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class IncomingMessage:
    """Входящее сообщение клиента в канало-независимом виде."""

    channel: str
    external_chat_id: str
    external_message_id: str
    text: str
    sender_name: str | None = None
    sender_username: str | None = None
    content_type: str = "text"  # "text" | "attachment"
    update_id: int | None = None


class ChannelError(Exception):
    """Ошибка канала. Текст безопасен для логов: токенов и URL с токеном в нём нет."""


class ChannelSendError(ChannelError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        blocked_by_user: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.blocked_by_user = blocked_by_user
        self.status_code = status_code


class ChannelClient(Protocol):
    """Общий контракт клиента канала (раздел 1)."""

    def send_message(self, chat_id: str, text: str, buttons: list[str] | None = None) -> str:
        """Отправить сообщение, вернуть внешний id (последнего фрагмента).
        buttons — кнопки-ответы (решение 2026-10-02: «Приду» / «Перенести запись»):
        нажатие приходит обычным текстовым сообщением клиента и после него
        клавиатура скрывается."""
        ...


def split_text(text: str, limit: int) -> list[str]:
    """Разбить длинный текст на фрагменты по границам строк/слов (лимит канала)."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        chunks.append(rest)
    return chunks
