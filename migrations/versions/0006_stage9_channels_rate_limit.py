"""Этап 9: канал VK и общий rate limit

Разделы 1, 16 ТЗ, этап 9. integrations.channel_settings — несекретные параметры
канала (для VK: код подтверждения и id сервера Callback API). rate_limit_counters —
счётчики ограничения частоты в общей БД (лимит един для всех воркеров и реплик).
Значение VK в колонках channel миграции не требует: это VARCHAR без CHECK.

Revision ID: 0006_stage9
Revises: 0005_stage6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_stage9"
down_revision: str | None = "0005_stage6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "integrations",
        sa.Column(
            "channel_settings",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
    )
    op.create_table(
        "rate_limit_counters",
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("window_start", sa.BigInteger(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index("ix_rate_limit_counters_window_start", "rate_limit_counters", ["window_start"])


def downgrade() -> None:
    op.drop_index("ix_rate_limit_counters_window_start", table_name="rate_limit_counters")
    op.drop_table("rate_limit_counters")
    op.drop_column("integrations", "channel_settings")
