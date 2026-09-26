"""
Сотрудники компании: роли, удаление, приглашения — раздел 13 ТЗ («Сотрудники:
приглашение менеджеров и роли»), разделы 5 и 16.

Правила:
* в компании всегда остаётся хотя бы один OWNER: последнего владельца нельзя ни
  понизить, ни удалить;
* при удалении сотрудника его лиды остаются без ответственного (не теряются);
* приглашение — одноразовая ссылка, токен хранится только как SHA-256, принять его
  может пользователь с тем же email; письма система не шлёт (почтовой
  инфраструктуры в MVP нет), владелец передаёт ссылку сам.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from config import settings
from models import (
    Business,
    BusinessMember,
    Invitation,
    Lead,
    MemberRole,
    User,
    utcnow,
)
from services import audit_service, master_service
from services.access_service import BusinessContext
from services.auth_service import get_user_by_email


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    """SQLite возвращает datetime без часового пояса; в БД всё хранится в UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _owner_count(db: Session, business_id: int) -> int:
    return len(
        db.scalars(
            select(BusinessMember.id).where(
                BusinessMember.business_id == business_id,
                BusinessMember.role == MemberRole.OWNER,
            )
        ).all()
    )


def _get_member(db: Session, business_id: int, user_id: int) -> BusinessMember:
    member = db.scalar(
        select(BusinessMember).where(
            BusinessMember.business_id == business_id, BusinessMember.user_id == user_id
        )
    )
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Сотрудник не найден")
    return member


# --------------------------------------------------------------------------- #
# Роли и удаление
# --------------------------------------------------------------------------- #
def update_member_role(
    db: Session, ctx: BusinessContext, user_id: int, role: MemberRole
) -> tuple[BusinessMember, User]:
    member = _get_member(db, ctx.business_id, user_id)
    if (
        member.role is MemberRole.OWNER
        and role is not MemberRole.OWNER
        and _owner_count(db, ctx.business_id) <= 1
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="В компании должен остаться хотя бы один владелец",
        )
    previous = member.role
    member.role = role
    user = db.get_one(User, user_id)
    if role is MemberRole.MASTER:
        master_service.ensure_master_for_member(db, ctx.business_id, user)
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.BUSINESS_MEMBER_UPDATED,
        message=f"Роль {user.email}: {previous.value} → {role.value}",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={"member_user_id": user_id, "from": previous.value, "to": role.value},
    )
    db.commit()
    db.refresh(member)
    return member, user


def remove_member(db: Session, ctx: BusinessContext, user_id: int) -> None:
    member = _get_member(db, ctx.business_id, user_id)
    if member.role is MemberRole.OWNER and _owner_count(db, ctx.business_id) <= 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Нельзя удалить единственного владельца компании",
        )
    user = db.get_one(User, user_id)
    # Лиды удаляемого не пропадают, а возвращаются в общую очередь.
    unassigned = db.execute(
        update(Lead)
        .where(Lead.business_id == ctx.business_id, Lead.assigned_to == user_id)
        .values(assigned_to=None)
    )
    db.delete(member)
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.BUSINESS_MEMBER_REMOVED,
        message=f"Сотрудник {user.email} удалён из компании",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={
            "member_user_id": user_id,
            "role": member.role.value,
            "leads_unassigned": getattr(unassigned, "rowcount", None),
        },
    )
    db.commit()


# --------------------------------------------------------------------------- #
# Приглашения
# --------------------------------------------------------------------------- #
def _is_pending(invitation: Invitation) -> bool:
    return (
        invitation.accepted_at is None
        and invitation.revoked_at is None
        and _aware(invitation.expires_at) > utcnow()
    )


def create_invitation(
    db: Session, ctx: BusinessContext, email: str, role: MemberRole
) -> tuple[Invitation, str]:
    """Создать приглашение. Возвращает (запись, токен): токен показывается один раз."""
    email = email.strip().lower()
    existing_user = get_user_by_email(db, email)
    if existing_user is not None:
        already = db.scalar(
            select(BusinessMember).where(
                BusinessMember.business_id == ctx.business_id,
                BusinessMember.user_id == existing_user.id,
            )
        )
        if already is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Этот сотрудник уже в компании"
            )

    # Прежние неиспользованные приглашения на этот адрес аннулируются: рабочая ссылка одна.
    for old in db.scalars(
        select(Invitation).where(
            Invitation.business_id == ctx.business_id, Invitation.email == email
        )
    ):
        if _is_pending(old):
            old.revoked_at = utcnow()

    token = secrets.token_urlsafe(32)
    invitation = Invitation(
        business_id=ctx.business_id,
        email=email,
        role=role,
        token_hash=hash_token(token),
        expires_at=utcnow() + timedelta(days=settings.invitation_ttl_days),
        created_by=ctx.user.id,
    )
    db.add(invitation)
    db.flush()
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.INVITATION_CREATED,
        message=f"Создано приглашение для {email} ({role.value})",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={"invitation_id": invitation.id, "email": email, "role": role.value},
    )
    db.commit()
    db.refresh(invitation)
    return invitation, token


def list_invitations(db: Session, ctx: BusinessContext) -> list[Invitation]:
    """Действующие приглашения компании (не принятые, не отозванные, не истёкшие)."""
    rows = db.scalars(
        select(Invitation)
        .where(Invitation.business_id == ctx.business_id)
        .order_by(Invitation.id.desc())
    ).all()
    return [row for row in rows if _is_pending(row)]


def revoke_invitation(db: Session, ctx: BusinessContext, invitation_id: int) -> None:
    invitation = db.scalar(
        select(Invitation).where(
            Invitation.id == invitation_id, Invitation.business_id == ctx.business_id
        )
    )
    if invitation is None or not _is_pending(invitation):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Приглашение не найдено")
    invitation.revoked_at = utcnow()
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.INVITATION_REVOKED,
        message=f"Приглашение для {invitation.email} отозвано",
        business_id=ctx.business_id,
        actor_user_id=ctx.user.id,
        payload={"invitation_id": invitation.id},
    )
    db.commit()


def find_valid_invitation(db: Session, token: str | None) -> Invitation | None:
    if not token:
        return None
    invitation = db.scalar(select(Invitation).where(Invitation.token_hash == hash_token(token)))
    return invitation if invitation is not None and _is_pending(invitation) else None


def accept_invitation(db: Session, user: User, token: str) -> tuple[BusinessMember, Business]:
    """Принять приглашение. Нужен вход под тем же email, на который оно выдано."""
    invitation = find_valid_invitation(db, token)
    if invitation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Приглашение недействительно или срок его действия истёк",
        )
    if user.email != invitation.email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Приглашение выдано на другой адрес электронной почты",
        )

    business = db.get_one(Business, invitation.business_id)
    member = db.scalar(
        select(BusinessMember).where(
            BusinessMember.business_id == invitation.business_id,
            BusinessMember.user_id == user.id,
        )
    )
    if member is None:
        member = BusinessMember(
            business_id=invitation.business_id, user_id=user.id, role=invitation.role
        )
        db.add(member)
    if member.role is MemberRole.MASTER:
        # Мастер сразу получает профиль: расписание и записи (вне ТЗ, §22).
        master_service.ensure_master_for_member(db, invitation.business_id, user)
    invitation.accepted_at = utcnow()
    invitation.accepted_by = user.id
    audit_service.log_event(
        db,
        event_type=audit_service.EventType.INVITATION_ACCEPTED,
        message=f"{user.email} принял приглашение ({invitation.role.value})",
        business_id=invitation.business_id,
        actor_user_id=user.id,
        payload={"invitation_id": invitation.id, "role": invitation.role.value},
    )
    db.commit()
    db.refresh(member)
    return member, business
