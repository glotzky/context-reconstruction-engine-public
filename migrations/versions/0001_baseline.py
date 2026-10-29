"""Baseline schema: orders and jobs, with fixed fake seed data.

Revision ID: 0001_baseline
Revises: none

Rollback: alembic downgrade base (drops jobs and orders)
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEED_TIME = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def upgrade() -> None:
    orders = op.create_table(
        "orders",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("customer_name", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("total_cents", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    jobs = op.create_table(
        "jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.bulk_insert(
        orders,
        [
            {
                "id": 1,
                "customer_name": "Test Customer One",
                "status": "paid",
                "total_cents": 1999,
                "created_at": SEED_TIME,
            },
            {
                "id": 2,
                "customer_name": "Test Customer Two",
                "status": "pending",
                "total_cents": 4500,
                "created_at": SEED_TIME,
            },
            {
                "id": 3,
                "customer_name": "Example Person",
                "status": "shipped",
                "total_cents": 250,
                "created_at": SEED_TIME,
            },
        ],
    )
    op.bulk_insert(
        jobs,
        [
            {
                "id": 1,
                "kind": "noop",
                "status": "pending",
                "attempts": 0,
                "last_error": None,
                "created_at": SEED_TIME,
                "updated_at": SEED_TIME,
            }
        ],
    )
    op.execute(
        "SELECT setval(pg_get_serial_sequence('orders', 'id'), (SELECT MAX(id) FROM orders))"
    )
    op.execute("SELECT setval(pg_get_serial_sequence('jobs', 'id'), (SELECT MAX(id) FROM jobs))")


def downgrade() -> None:
    op.drop_table("jobs")
    op.drop_table("orders")
