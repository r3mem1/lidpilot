"""Приглашение мастера привязывается к уже заведённому мастеру (invitations.master_id)

Вне ТЗ (§22), проверка сайта 2026-10-01: приглашённый мастер создавался заново
(с именем из email), а смены и записи мастера без аккаунта оставались у дубля.

Revision ID: 0009_invitation_master
Revises: 0008_booking_requests
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_invitation_master"
down_revision: str | None = "0008_booking_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("invitations") as batch:
        batch.add_column(sa.Column("master_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_invitations_master_id", "masters", ["master_id"], ["id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    with op.batch_alter_table("invitations") as batch:
        batch.drop_constraint("fk_invitations_master_id", type_="foreignkey")
        batch.drop_column("master_id")
