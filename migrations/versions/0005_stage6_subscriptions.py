"""Этап 6: подписки (тариф и пробный период)

Раздел 10 ТЗ (subscriptions), раздел 15 (тариф, trial). Компании из прошлых этапов
получают запись TRIAL: срок пробного периода — created_at + 14 суток.

Revision ID: 0005_stage6
Revises: 0004_stage5
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_stage6"
down_revision: str | None = "0004_stage5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column(
            "plan",
            sa.Enum("TRIAL", "START", "PRO", name="subscriptionplan", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "CANCELED", name="subscriptionstatus", native_enum=False, length=32),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", name="uq_subscriptions_business_id"),
    )

    # Перенос существующих компаний: пробный тариф, срок — 14 суток от регистрации.
    # Даты считаются в Python: одинаково для SQLite и PostgreSQL.
    from datetime import UTC, datetime, timedelta

    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, created_at FROM businesses")).fetchall()
    subscriptions = sa.table(
        "subscriptions",
        sa.column("business_id", sa.Integer),
        sa.column("plan", sa.String),
        sa.column("status", sa.String),
        sa.column("started_at", sa.DateTime(timezone=True)),
        sa.column("expires_at", sa.DateTime(timezone=True)),
    )
    for business_id, created_at in rows:
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        op.bulk_insert(
            subscriptions,
            [
                {
                    "business_id": business_id,
                    "plan": "TRIAL",
                    "status": "ACTIVE",
                    "started_at": created_at,
                    "expires_at": created_at + timedelta(days=14),
                }
            ],
        )


def downgrade() -> None:
    op.drop_table("subscriptions")
