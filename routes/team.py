"""
Сотрудники и приглашения — раздел 13 ТЗ (вне минимального списка раздела 11):
    PATCH  /businesses/{business_id}/members/{user_id}     роль сотрудника
    DELETE /businesses/{business_id}/members/{user_id}     удалить сотрудника
    GET    /businesses/{business_id}/invitations           действующие приглашения
    POST   /businesses/{business_id}/invitations           создать приглашение
    DELETE /businesses/{business_id}/invitations/{id}      отозвать
    POST   /invitations/accept                             принять (любой вошедший)

Управление командой — только OWNER (раздел 5: MANAGER без системных настроек).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Request, Response, status
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models import MemberRole, User
from schemas import (
    BusinessMemberOut,
    InvitationAccept,
    InvitationCreate,
    InvitationCreated,
    InvitationOut,
    MemberRoleUpdate,
    MembershipOut,
)
from services import team_service
from services.access_service import BusinessContext, get_current_user, require_business_roles

router = APIRouter(tags=["team"])

OWNER_ONLY = (MemberRole.OWNER,)


def invitation_url(request: Request, token: str) -> str:
    base = settings.public_base_url or str(request.base_url).rstrip("/")
    return f"{base}/invite/{token}"


@router.patch("/businesses/{business_id}/members/{user_id}", response_model=BusinessMemberOut)
def update_member_role(
    payload: MemberRoleUpdate,
    user_id: int = Path(..., ge=1),
    ctx: BusinessContext = Depends(require_business_roles(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    member, user = team_service.update_member_role(db, ctx, user_id, payload.role)
    return BusinessMemberOut(
        id=member.id,
        business_id=member.business_id,
        user_id=member.user_id,
        email=user.email,
        role=member.role,
        created_at=member.created_at,
    )


@router.delete(
    "/businesses/{business_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT
)
def remove_member(
    user_id: int = Path(..., ge=1),
    ctx: BusinessContext = Depends(require_business_roles(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    team_service.remove_member(db, ctx, user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/businesses/{business_id}/invitations", response_model=list[InvitationOut])
def list_invitations(
    ctx: BusinessContext = Depends(require_business_roles(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    return team_service.list_invitations(db, ctx)


@router.post(
    "/businesses/{business_id}/invitations",
    response_model=InvitationCreated,
    status_code=status.HTTP_201_CREATED,
)
def create_invitation(
    payload: InvitationCreate,
    request: Request,
    ctx: BusinessContext = Depends(require_business_roles(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    """Ссылка-приглашение возвращается один раз; передайте её сотруднику сами."""
    invitation, token = team_service.create_invitation(db, ctx, payload.email, payload.role)
    return InvitationCreated(
        id=invitation.id,
        email=invitation.email,
        role=invitation.role,
        expires_at=invitation.expires_at,
        created_at=invitation.created_at,
        invite_url=invitation_url(request, token),
    )


@router.delete(
    "/businesses/{business_id}/invitations/{invitation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def revoke_invitation(
    invitation_id: int = Path(..., ge=1),
    ctx: BusinessContext = Depends(require_business_roles(*OWNER_ONLY)),
    db: Session = Depends(get_db),
):
    team_service.revoke_invitation(db, ctx, invitation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/invitations/accept", response_model=MembershipOut)
def accept_invitation(
    payload: InvitationAccept,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    member, business = team_service.accept_invitation(db, user, payload.token.get_secret_value())
    return MembershipOut(business_id=business.id, business_name=business.name, role=member.role)
