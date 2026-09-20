"""Этап 4: лиды, ручной ответ менеджера, передача диалога человеку

Раздел 10 ТЗ (leads), раздел 14 (рабочее место менеджера).

Revision ID: 0003_stage4
Revises: 0002_stage3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_stage4"
down_revision: str | None = "0002_stage3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "leads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "NEW", "IN_PROGRESS", "RESOLVED", "LOST",
                name="leadstatus", native_enum=False, length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "priority",
            sa.Enum("HOT", "WARM", "COLD", name="leadpriority", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("intent", sa.String(length=32), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("assigned_to", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["assigned_to"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", name="uq_leads_conversation"),
    )
    with op.batch_alter_table("leads", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_leads_assigned_to"), ["assigned_to"], unique=False)
        batch_op.create_index(batch_op.f("ix_leads_business_id"), ["business_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_leads_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_leads_priority"), ["priority"], unique=False)
        batch_op.create_index(batch_op.f("ix_leads_status"), ["status"], unique=False)

    with op.batch_alter_table("conversations", schema=None) as batch_op:
        # server_default: в таблице уже могут быть диалоги (этап 3).
        batch_op.add_column(
            sa.Column(
                "handled_by_manager", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )

    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("author_user_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_messages_author_user_id"), ["author_user_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_messages_author_user_id_users",
            "users",
            ["author_user_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_constraint("fk_messages_author_user_id_users", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_messages_author_user_id"))
        batch_op.drop_column("author_user_id")

    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.drop_column("handled_by_manager")

    with op.batch_alter_table("leads", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_leads_status"))
        batch_op.drop_index(batch_op.f("ix_leads_priority"))
        batch_op.drop_index(batch_op.f("ix_leads_created_at"))
        batch_op.drop_index(batch_op.f("ix_leads_business_id"))
        batch_op.drop_index(batch_op.f("ix_leads_assigned_to"))

    op.drop_table("leads")
