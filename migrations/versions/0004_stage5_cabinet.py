"""Этап 5: настройки AI компании и приглашения сотрудников

Раздел 13 ТЗ («AI: правила, стиль ответа, разрешённые действия»,
«Сотрудники: приглашение менеджеров и роли»).

Revision ID: 0004_stage5
Revises: 0003_stage4
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_stage5"
down_revision: str | None = "0003_stage4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("businesses", schema=None) as batch_op:
        # server_default: компании из прошлых этапов получают прежнее поведение.
        batch_op.add_column(
            sa.Column(
                "ai_tone",
                sa.Enum("FRIENDLY", "FORMAL", "BRIEF", name="aitone", native_enum=False, length=32),
                nullable=False,
                server_default="FRIENDLY",
            )
        )
        batch_op.add_column(
            sa.Column("ai_auto_reply", sa.Boolean(), nullable=False, server_default=sa.true())
        )

    op.create_table(
        "invitations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column(
            "role",
            sa.Enum("OWNER", "MANAGER", name="memberrole", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by", sa.Integer(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["accepted_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("invitations", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_invitations_business_id"), ["business_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_invitations_email"), ["email"], unique=False)
        batch_op.create_index(batch_op.f("ix_invitations_token_hash"), ["token_hash"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("invitations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_invitations_token_hash"))
        batch_op.drop_index(batch_op.f("ix_invitations_email"))
        batch_op.drop_index(batch_op.f("ix_invitations_business_id"))
    op.drop_table("invitations")

    with op.batch_alter_table("businesses", schema=None) as batch_op:
        batch_op.drop_column("ai_auto_reply")
        batch_op.drop_column("ai_tone")
