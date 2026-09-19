"""
Хранилище секретов интеграций — раздел 16 ТЗ.

Токен бота компании нельзя держать в БД открытым текстом, но и в .env для
каждой компании его не положишь (компания подключает бота сама). Поэтому в
integrations.credentials_ref хранится ССЫЛКА двух видов:

    enc:<шифртекст>   токен, зашифрованный Fernet (AES-128-CBC + HMAC-SHA256);
                      ключ берётся из окружения и в БД не попадает;
    env:<ИМЯ>         токен лежит в переменной окружения / секрет-хранилище
                      хостинга (для компаний, которые подключает оператор).

Ключ шифрования — SECRETS_ENCRYPTION_KEY (любая случайная строка от 32 символов).
Если он не задан, ключ выводится из JWT_SECRET; в production задавать явно
(проверка при старте), иначе смена JWT_SECRET сделает токены нечитаемыми.
"""

from __future__ import annotations

import base64
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from config import settings

_ENC_PREFIX = "enc:"
_ENV_PREFIX = "env:"
_HKDF_INFO = b"leadpilot-secrets-v1"


class SecretStoreError(Exception):
    """Секрет не удалось сохранить или прочитать. Текст безопасен для логов."""


def _fernet() -> Fernet:
    material = settings.secrets_encryption_key or settings.jwt_secret
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_HKDF_INFO).derive(
        material.encode("utf-8")
    )
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(plain: str) -> str:
    """Секрет → ссылка для integrations.credentials_ref."""
    return _ENC_PREFIX + _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def resolve_secret(reference: str) -> str:
    """Ссылка из integrations.credentials_ref → секрет."""
    if reference.startswith(_ENC_PREFIX):
        try:
            return _fernet().decrypt(reference[len(_ENC_PREFIX) :].encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise SecretStoreError(
                "Не удалось расшифровать секрет: изменился ключ шифрования "
                "(SECRETS_ENCRYPTION_KEY / JWT_SECRET). Подключите канал заново."
            ) from exc
    if reference.startswith(_ENV_PREFIX):
        name = reference[len(_ENV_PREFIX) :]
        value = os.environ.get(name)
        if not value:
            raise SecretStoreError(f"Переменная окружения {name} не задана")
        return value
    raise SecretStoreError("Неизвестный формат credentials_ref")
