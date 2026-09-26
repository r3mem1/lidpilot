"""
Подключение каналов компании — разделы 10, 11, 16 ТЗ (критерий приёмки 2:
«Компания может подключить Telegram-бота»).

Сценарий подключения (порядок важен):
1. проверить токен вызовом getMe (заодно узнаём id и имя бота);
2. СОХРАНИТЬ интеграцию (токен — шифртекстом, секрет webhook — SHA-256);
3. только затем зарегистрировать webhook: Telegram начинает присылать
   накопленные апдейты сразу после setWebhook, и к этому моменту запись
   с секретом уже должна быть зафиксирована в БД, иначе первые сообщения
   были бы отклонены.

Секрет webhook показывается Telegram один раз и в открытом виде нигде не
хранится; по нему компания определяется хешем (раздел 16).

Этап 9 — VK (сообщения сообщества, Callback API), тот же порядок:
1. проверить ключ доступа сообщества (groups.getById — id и название группы) и
   получить строку подтверждения (groups.getCallbackConfirmationCode);
2. СОХРАНИТЬ интеграцию (ключ — шифртекстом, секрет — SHA-256, строка
   подтверждения и id сервера — в channel_settings);
3. только затем добавить сервер Callback API: VK сразу присылает на него
   запрос confirmation, и ответ уже должен быть в БД.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from contextlib import suppress

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import settings
from integrations import vk
from integrations.base import ChannelClient, ChannelError
from integrations.telegram import BOT_TOKEN_RE, TelegramClient
from integrations.vk import VkClient
from models import Channel, Integration, IntegrationStatus, LogLevel
from services import audit_service, secret_store
from services.access_service import BusinessContext

WEBHOOK_PATHS = {Channel.TELEGRAM: "/webhooks/telegram", Channel.VK: "/webhooks/vk"}
WEBHOOK_PATH = WEBHOOK_PATHS[Channel.TELEGRAM]
CHANNEL_TITLES = {Channel.TELEGRAM: "Telegram", Channel.VK: "VK"}


def _default_client_factory(bot_token: str) -> TelegramClient:
    return TelegramClient(bot_token)


def _default_vk_factory(token: str) -> VkClient:
    return VkClient(token)


# Точки подмены в тестах: клиенты с httpx.MockTransport вместо реальной сети.
build_telegram_client: Callable[[str], TelegramClient] = _default_client_factory
build_vk_client: Callable[[str], VkClient] = _default_vk_factory


def hash_webhook_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def webhook_url(channel: Channel = Channel.TELEGRAM) -> str | None:
    base = settings.public_base_url
    return f"{base}{WEBHOOK_PATHS[channel]}" if base else None


def _require_https_url(channel: Channel) -> str:
    url = webhook_url(channel)
    if not url or not url.lower().startswith("https://"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Не задан PUBLIC_BASE_URL (публичный https-адрес приложения): "
                f"{CHANNEL_TITLES[channel]} принимает события только по HTTPS"
            ),
        )
    return url


# --------------------------------------------------------------------------- #
# Чтение
# --------------------------------------------------------------------------- #
def list_integrations(db: Session, ctx: BusinessContext) -> list[Integration]:
    return list(
        db.scalars(
            select(Integration)
            .where(Integration.business_id == ctx.business_id)
            .order_by(Integration.id)
        )
    )


def get_active_integration(db: Session, business_id: int, channel: Channel) -> Integration | None:
    return db.scalar(
        select(Integration).where(
            Integration.business_id == business_id,
            Integration.channel == channel,
            Integration.status == IntegrationStatus.ACTIVE,
        )
    )


def find_by_webhook_secret(db: Session, secret: str | None) -> Integration | None:
    """Компания по секрету webhook. Сравнение идёт по SHA-256 через уникальный
    индекс: подобрать секрет по времени ответа БД нельзя (хеш от входа)."""
    if not secret:
        return None
    return db.scalar(
        select(Integration).where(
            Integration.webhook_secret_hash == hash_webhook_secret(secret),
            Integration.channel == Channel.TELEGRAM,
            Integration.status == IntegrationStatus.ACTIVE,
        )
    )


def find_vk_integration(db: Session, group_id: str | None) -> Integration | None:
    """Интеграция VK по id сообщества из события (подлинность — verify_secret)."""
    if not group_id:
        return None
    return db.scalar(
        select(Integration).where(
            Integration.channel == Channel.VK,
            Integration.external_account_id == group_id,
            Integration.status != IntegrationStatus.DISABLED,
        )
    )


def verify_secret(integration: Integration, secret: str | None) -> bool:
    """Секрет события совпадает с сохранённым хешем (сравнение за постоянное время)."""
    if not secret or not integration.webhook_secret_hash:
        return False
    return hmac.compare_digest(hash_webhook_secret(secret), integration.webhook_secret_hash)


def get_channel_client(integration: Integration) -> ChannelClient:
    """Клиент канала интеграции (токен расшифровывается только здесь)."""
    token = secret_store.resolve_secret(integration.credentials_ref)
    if integration.channel is Channel.VK:
        return build_vk_client(token)
    return build_telegram_client(token)


# --------------------------------------------------------------------------- #
# Подключение / отключение Telegram
# --------------------------------------------------------------------------- #
def connect_telegram(db: Session, ctx: BusinessContext, bot_token: str) -> Integration:
    url = _require_https_url(Channel.TELEGRAM)
    if not BOT_TOKEN_RE.match(bot_token):
        raise HTTPException(
            status_code=422,
            detail="Токен бота имеет неверный формат (ожидается 123456:ABC…)",
        )

    client = build_telegram_client(bot_token)
    try:
        bot = client.get_me()
    except ChannelError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Telegram не принял токен бота: {exc}",
        ) from exc
    bot_id, bot_username = str(bot.get("id", "")), bot.get("username")
    if not bot_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="Telegram не вернул данные бота"
        )

    other = db.scalar(
        select(Integration).where(
            Integration.channel == Channel.TELEGRAM,
            Integration.external_account_id == bot_id,
            Integration.business_id != ctx.business_id,
        )
    )
    if other is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Этот бот уже подключён к другой компании",
        )

    try:
        secret_store_ref = secret_store.encrypt_secret(bot_token)
    except Exception as exc:  # noqa: BLE001 - без деталей: рядом лежит секрет
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Не удалось защитить токен бота",
        ) from exc

    webhook_secret = secrets.token_urlsafe(32)
    integration = db.scalar(
        select(Integration).where(
            Integration.business_id == ctx.business_id, Integration.channel == Channel.TELEGRAM
        )
    )
    if integration is None:
        integration = Integration(business_id=ctx.business_id, channel=Channel.TELEGRAM)
        db.add(integration)
    integration.credentials_ref = secret_store_ref
    integration.webhook_secret_hash = hash_webhook_secret(webhook_secret)
    integration.external_account_id = bot_id
    integration.external_account_name = bot_username if isinstance(bot_username, str) else None
    integration.status = IntegrationStatus.ACTIVE
    integration.last_error = None
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Этот бот уже подключён"
        ) from exc
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.INTEGRATION_CONNECTED,
        message=f"Подключён Telegram-бот @{integration.external_account_name or bot_id}",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={"channel": "TELEGRAM", "bot_id": bot_id},
    )
    db.commit()  # шаг 2: секрет зафиксирован до регистрации webhook

    try:
        client.set_webhook(url, webhook_secret)
    except ChannelError as exc:
        integration.status = IntegrationStatus.ERROR
        integration.last_error = str(exc)[:500]
        audit_service.log_event(
            db,
            event_type=audit_service.EventType.INTEGRATION_ERROR,
            message="Не удалось зарегистрировать webhook Telegram",
            level=LogLevel.ERROR,
            business_id=ctx.business_id,
            actor_user_id=ctx.user.id,
            payload={"error": str(exc)[:300]},
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Не удалось зарегистрировать webhook в Telegram: {exc}",
        ) from exc

    db.refresh(integration)
    return integration


def disconnect_telegram(db: Session, ctx: BusinessContext) -> None:
    integration = db.scalar(
        select(Integration).where(
            Integration.business_id == ctx.business_id, Integration.channel == Channel.TELEGRAM
        )
    )
    if integration is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Интеграция не найдена")

    # Снять webhook на стороне Telegram — по возможности: даже если запрос не
    # удался, секрет обнуляется, и входящие всё равно будут отклонены.
    if integration.credentials_ref:
        try:
            build_telegram_client(
                secret_store.resolve_secret(integration.credentials_ref)
            ).delete_webhook()
        except (ChannelError, secret_store.SecretStoreError) as exc:
            audit_service.log_event(
                db,
                event_type=audit_service.EventType.INTEGRATION_ERROR,
                message="Не удалось снять webhook Telegram при отключении",
                level=LogLevel.WARNING,
                business_id=ctx.business_id,
                actor_user_id=ctx.user.id,
                payload={"error": str(exc)[:300]},
            )

    integration.status = IntegrationStatus.DISABLED
    integration.webhook_secret_hash = None
    integration.credentials_ref = ""
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.INTEGRATION_DISCONNECTED,
        message="Telegram-бот отключён",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={"channel": "TELEGRAM"},
    )
    db.commit()


# --------------------------------------------------------------------------- #
# Подключение / отключение VK (этап 9)
# --------------------------------------------------------------------------- #
def _integration_error(
    db: Session, ctx: BusinessContext, integration: Integration, message: str, exc: Exception
) -> None:
    integration.status = IntegrationStatus.ERROR
    integration.last_error = str(exc)[:500]
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.INTEGRATION_ERROR,
        message=message,
        level=LogLevel.ERROR,
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={"channel": integration.channel.value, "error": str(exc)[:300]},
    )
    db.commit()


def connect_vk(db: Session, ctx: BusinessContext, token: str) -> Integration:
    url = _require_https_url(Channel.VK)
    if not vk.COMMUNITY_TOKEN_RE.match(token):
        raise HTTPException(status_code=422, detail="Ключ доступа сообщества имеет неверный формат")

    client = build_vk_client(token)
    try:
        group = client.get_group()
        group_id = str(group.get("id") or "")
        if not group_id:
            raise ChannelError("VK не вернул идентификатор сообщества")
        confirmation_code = client.get_confirmation_code(group_id)
    except ChannelError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"VK не принял ключ доступа сообщества: {exc}",
        ) from exc
    name = group.get("name")
    group_name = name if isinstance(name, str) else None

    other = db.scalar(
        select(Integration).where(
            Integration.channel == Channel.VK,
            Integration.external_account_id == group_id,
            Integration.business_id != ctx.business_id,
        )
    )
    if other is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Это сообщество VK уже подключено к другой компании",
        )

    try:
        token_ref = secret_store.encrypt_secret(token)
    except Exception as exc:  # noqa: BLE001 - без деталей: рядом лежит секрет
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Не удалось защитить ключ доступа",
        ) from exc

    integration = db.scalar(
        select(Integration).where(
            Integration.business_id == ctx.business_id, Integration.channel == Channel.VK
        )
    )
    previous = dict(integration.channel_settings or {}) if integration is not None else {}
    if integration is None:
        integration = Integration(business_id=ctx.business_id, channel=Channel.VK)
        db.add(integration)

    # Повторное подключение: старый сервер Callback API убираем (в группе их не больше 10).
    old_server = previous.get("server_id")
    if old_server and previous.get("group_id") == group_id:
        with suppress(ChannelError):  # сервер могли удалить вручную — подключению не мешает
            client.delete_callback_server(group_id, str(old_server))

    secret_key = secrets.token_urlsafe(24)  # не длиннее 50 символов (ограничение VK)
    integration.credentials_ref = token_ref
    integration.webhook_secret_hash = hash_webhook_secret(secret_key)
    integration.external_account_id = group_id
    integration.external_account_name = group_name
    integration.channel_settings = {"group_id": group_id, "confirmation_code": confirmation_code}
    integration.status = IntegrationStatus.ACTIVE
    integration.last_error = None
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Это сообщество уже подключено"
        ) from exc
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.INTEGRATION_CONNECTED,
        message=f"Подключено сообщество VK «{group_name or group_id}»",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={"channel": "VK", "group_id": group_id},
    )
    db.commit()  # шаг 2: секрет и строка подтверждения зафиксированы до регистрации сервера

    try:
        server_id = client.add_callback_server(group_id, url, secret_key)
        integration.channel_settings = {
            **(integration.channel_settings or {}),
            "server_id": server_id,
        }
        db.commit()
        client.set_callback_settings(group_id, server_id)
    except ChannelError as exc:
        _integration_error(db, ctx, integration, "Не удалось настроить Callback API VK", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Не удалось настроить Callback API в VK: {exc}",
        ) from exc

    db.refresh(integration)
    return integration


def disconnect_vk(db: Session, ctx: BusinessContext) -> None:
    integration = db.scalar(
        select(Integration).where(
            Integration.business_id == ctx.business_id, Integration.channel == Channel.VK
        )
    )
    if integration is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Интеграция не найдена")

    stored = integration.channel_settings or {}
    group_id, server_id = stored.get("group_id"), stored.get("server_id")
    if integration.credentials_ref and group_id and server_id:
        try:
            client = build_vk_client(secret_store.resolve_secret(integration.credentials_ref))
            client.delete_callback_server(str(group_id), str(server_id))
        except (ChannelError, secret_store.SecretStoreError) as exc:
            audit_service.log_event(
                db,
                event_type=audit_service.EventType.INTEGRATION_ERROR,
                message="Не удалось удалить сервер Callback API VK при отключении",
                level=LogLevel.WARNING,
                business_id=ctx.business_id,
                actor_user_id=ctx.user.id,
                payload={"error": str(exc)[:300]},
            )

    integration.status = IntegrationStatus.DISABLED
    integration.webhook_secret_hash = None
    integration.credentials_ref = ""
    integration.channel_settings = {"group_id": group_id}
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.INTEGRATION_DISCONNECTED,
        message="Сообщество VK отключено",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={"channel": "VK"},
    )
    db.commit()
