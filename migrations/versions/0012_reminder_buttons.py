"""Кнопки «Приду» / «Перенести запись» в напоминаниях

Вне ТЗ (§22), решение заказчика 2026-10-02: messages.buttons — кнопки-ответы
исходящего сообщения (повтор отправки уходит с ними же); bookings.client_confirmed_at
— клиент подтвердил визит кнопкой «Приду».

Revision ID: 0012_reminder_buttons
Revises: 0011_booking_reminders
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_reminder_buttons"
down_revision: str | None = "0011_booking_reminders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.add_column(sa.Column("buttons", JSONType, nullable=True))
    with op.batch_alter_table("bookings") as batch:
        batch.add_column(
            sa.Column("client_confirmed_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("bookings") as batch:
        batch.drop_column("client_confirmed_at")
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("buttons")
