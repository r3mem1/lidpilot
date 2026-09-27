"""Заявки на запись без брони (booking_requests)

Вне ТЗ (§22), решение заказчика 2026-09-28: AI понял просьбу о записи, но расписание
не подключено или окон нет — заявка сохраняется и видна администратору в «Записях».

Revision ID: 0008_booking_requests
Revises: 0007_booking
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_booking_requests"
down_revision: str | None = "0007_booking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "booking_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("conversation_id", sa.Integer(), nullable=True),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("client_name", sa.String(length=255), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=True),
        sa.Column("master_id", sa.Integer(), nullable=True),
        sa.Column("desired_day", sa.Date(), nullable=True),
        sa.Column("desired_time", sa.Time(), nullable=True),
        sa.Column("part_of_day", sa.String(length=16), nullable=True),
        sa.Column("last_text", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "OPEN", "DONE", "CLOSED", name="bookingrequeststatus", native_enum=False, length=32
            ),
            nullable=False,
        ),
        sa.Column("booking_id", sa.Integer(), nullable=True),
        sa.Column("closed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["master_id"], ["masters.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["closed_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("booking_requests", schema=None) as batch_op:
        batch_op.create_index(
            "ix_booking_requests_business_status", ["business_id", "status"], unique=False
        )
        batch_op.create_index("ix_booking_requests_conversation", ["conversation_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("booking_requests", schema=None) as batch_op:
        batch_op.drop_index("ix_booking_requests_conversation")
        batch_op.drop_index("ix_booking_requests_business_status")
    op.drop_table("booking_requests")
