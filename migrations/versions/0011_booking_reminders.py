"""Напоминания клиенту о записи: за сутки и за 2 часа

Вне ТЗ (§22), решение заказчика 2026-10-01: напоминания снижают неявки.
bookings.reminded_day_at / reminded_soon_at — когда отправлено (без дублей),
businesses.reminders_enabled — владелец может выключить.

Revision ID: 0011_booking_reminders
Revises: 0010_customer_contact
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_booking_reminders"
down_revision: str | None = "0010_customer_contact"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("bookings") as batch:
        batch.add_column(sa.Column("reminded_day_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("reminded_soon_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("businesses") as batch:
        batch.add_column(
            sa.Column(
                "reminders_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("businesses") as batch:
        batch.drop_column("reminders_enabled")
    with op.batch_alter_table("bookings") as batch:
        batch.drop_column("reminded_soon_at")
        batch.drop_column("reminded_day_at")
