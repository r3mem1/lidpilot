"""Карточка клиента: имя со слов клиента и заметка администратора

Вне ТЗ (§22), решение заказчика 2026-10-01: при записи собирать имя и телефон
клиента (телефон — существующее поле customers.phone), администратор ведёт заметку.

Revision ID: 0010_customer_contact
Revises: 0009_invitation_master
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_customer_contact"
down_revision: str | None = "0009_invitation_master"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("customers") as batch:
        batch.add_column(sa.Column("contact_name", sa.String(length=255), nullable=True))
        batch.add_column(sa.Column("notes", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("customers") as batch:
        batch.drop_column("notes")
        batch.drop_column("contact_name")
