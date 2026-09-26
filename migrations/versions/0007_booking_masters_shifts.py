"""Запись к мастерам: роль MASTER, мастера, смены, записи, уведомления мастеру

Вне ТЗ (§19 «автоматическая запись» вне MVP, §22 — развитие), по решению заказчика.
masters / master_services / master_shifts / bookings / master_notifications (outbox);
businesses: timezone, booking_enabled, slot_step_minutes. Роль MASTER миграции не требует
(role — VARCHAR без CHECK).

Revision ID: 0007_booking
Revises: 0006_stage9
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_booking"
down_revision: str | None = "0006_stage9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "masters",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column(
            "notify_channel",
            sa.Enum("TELEGRAM", "VK", name="channel", native_enum=False, length=32),
            nullable=True,
        ),
        sa.Column("notify_chat_id", sa.String(length=64), nullable=True),
        sa.Column("notify_code_hash", sa.String(length=64), nullable=True),
        sa.Column("notify_code_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "user_id", name="uq_masters_business_user"),
        sa.UniqueConstraint("notify_code_hash"),
    )
    with op.batch_alter_table("masters", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_masters_business_id"), ["business_id"], unique=False)

    op.create_table(
        "master_notifications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("master_id", sa.Integer(), nullable=False),
        sa.Column(
            "channel",
            sa.Enum("TELEGRAM", "VK", name="channel", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("chat_id", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING", "SENT", "FAILED", name="notificationstatus", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["master_id"], ["masters.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("master_notifications", schema=None) as batch_op:
        batch_op.create_index("ix_master_notifications_status", ["status"], unique=False)

    op.create_table(
        "master_services",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("master_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["master_id"], ["masters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("master_id", "service_id", name="uq_master_services_pair"),
    )
    with op.batch_alter_table("master_services", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_master_services_master_id"), ["master_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_master_services_service_id"), ["service_id"], unique=False
        )

    op.create_table(
        "master_shifts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("master_id", sa.Integer(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["master_id"], ["masters.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("master_shifts", schema=None) as batch_op:
        batch_op.create_index("ix_master_shifts_business_day", ["business_id", "day"], unique=False)
        batch_op.create_index("ix_master_shifts_master_day", ["master_id", "day"], unique=False)

    op.create_table(
        "bookings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("master_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=True),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("client_name", sa.String(length=255), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "CONFIRMED",
                "REJECTED",
                "CANCELLED",
                name="bookingstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "source",
            sa.Enum("AI", "STAFF", name="bookingsource", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("decided_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["master_id"], ["masters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("bookings", schema=None) as batch_op:
        batch_op.create_index(
            "ix_bookings_business_starts", ["business_id", "starts_at"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_bookings_customer_id"), ["customer_id"], unique=False)
        batch_op.create_index("ix_bookings_master_starts", ["master_id", "starts_at"], unique=False)

    with op.batch_alter_table("businesses", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "timezone", sa.String(length=64), server_default="Europe/Moscow", nullable=False
            )
        )
        batch_op.add_column(
            sa.Column("booking_enabled", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch_op.add_column(
            sa.Column("slot_step_minutes", sa.Integer(), server_default="30", nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("businesses", schema=None) as batch_op:
        batch_op.drop_column("slot_step_minutes")
        batch_op.drop_column("booking_enabled")
        batch_op.drop_column("timezone")

    with op.batch_alter_table("bookings", schema=None) as batch_op:
        batch_op.drop_index("ix_bookings_master_starts")
        batch_op.drop_index(batch_op.f("ix_bookings_customer_id"))
        batch_op.drop_index("ix_bookings_business_starts")

    op.drop_table("bookings")
    with op.batch_alter_table("master_shifts", schema=None) as batch_op:
        batch_op.drop_index("ix_master_shifts_master_day")
        batch_op.drop_index("ix_master_shifts_business_day")

    op.drop_table("master_shifts")
    with op.batch_alter_table("master_services", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_master_services_service_id"))
        batch_op.drop_index(batch_op.f("ix_master_services_master_id"))

    op.drop_table("master_services")
    with op.batch_alter_table("master_notifications", schema=None) as batch_op:
        batch_op.drop_index("ix_master_notifications_status")

    op.drop_table("master_notifications")
    with op.batch_alter_table("masters", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_masters_business_id"))

    op.drop_table("masters")
