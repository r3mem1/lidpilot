"""Начальная схема ядра: users, businesses, business_members, services, system_logs

Раздел 10 ТЗ. Этап 1 («Локальное ядро»).
Таблицы customers/conversations/messages/ai_responses/leads/integrations/
subscriptions добавят миграции этапов 3, 4 и 6.

Revision ID: 0001_initial
Revises:
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        # native_enum=False → VARCHAR + CHECK: одинаково в SQLite и PostgreSQL,
        # новое значение роли не требует ALTER TYPE.
        sa.Column(
            "role",
            sa.Enum("OWNER", "MANAGER", "ADMIN", name="userrole", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "SUSPENDED", name="userstatus", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_users_email"), ["email"], unique=True)

    op.create_table(
        "businesses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("category", sa.String(length=120), nullable=True),
        sa.Column("address", sa.String(length=500), nullable=True),
        sa.Column("phone", sa.String(length=50), nullable=True),
        sa.Column("working_hours", sa.String(length=500), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("escalation_contact", sa.String(length=255), nullable=True),
        sa.Column("ai_rules", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "TRIAL", "ACTIVE", "SUSPENDED", name="businessstatus", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # Владельца нельзя удалить, пока за ним есть компания.
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("businesses", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_businesses_owner_id"), ["owner_id"], unique=False)

    op.create_table(
        "business_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "role",
            sa.Enum("OWNER", "MANAGER", name="memberrole", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # Один пользователь — одна роль в компании.
        sa.UniqueConstraint("business_id", "user_id", name="uq_business_members_business_user"),
    )
    with op.batch_alter_table("business_members", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_business_members_business_id"), ["business_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_business_members_user_id"), ["user_id"], unique=False)

    op.create_table(
        "services",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("duration", sa.Integer(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("services", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_services_business_id"), ["business_id"], unique=False)

    op.create_table(
        "system_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        # NULL — событие уровня платформы (регистрация, неудачный вход).
        sa.Column("business_id", sa.Integer(), nullable=True),
        sa.Column(
            "level",
            sa.Enum(
                "INFO", "WARNING", "ERROR", "CRITICAL", name="loglevel", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        # JSON в SQLite, JSONB в PostgreSQL.
        sa.Column(
            "metadata",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("system_logs", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_system_logs_business_id"), ["business_id"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_system_logs_created_at"), ["created_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_system_logs_event_type"), ["event_type"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("system_logs", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_system_logs_event_type"))
        batch_op.drop_index(batch_op.f("ix_system_logs_created_at"))
        batch_op.drop_index(batch_op.f("ix_system_logs_business_id"))
    op.drop_table("system_logs")

    with op.batch_alter_table("services", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_services_business_id"))
    op.drop_table("services")

    with op.batch_alter_table("business_members", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_business_members_user_id"))
        batch_op.drop_index(batch_op.f("ix_business_members_business_id"))
    op.drop_table("business_members")

    with op.batch_alter_table("businesses", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_businesses_owner_id"))
    op.drop_table("businesses")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_users_email"))
    op.drop_table("users")
