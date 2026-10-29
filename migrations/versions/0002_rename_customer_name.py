"""Rename orders.customer_name to orders.customer_full_name.

SEEDED FAILURE #1 (intentional). The API still selects ``customer_name`` (see
``cre.orders.ORDER_QUERY``), so after this migration ``GET /orders/{order_id}`` returns a 500
and the API logs ``order_lookup_failed`` with ``error_type`` ``UndefinedColumn`` and
``sql_error`` ``column "customer_name" does not exist``, plus the request ID and trace ID.

Revision ID: 0002_rename_customer_name
Revises: 0001_baseline

Rollback: alembic downgrade 0001_baseline (renames customer_full_name back to customer_name)
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_rename_customer_name"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("orders", "customer_name", new_column_name="customer_full_name")


def downgrade() -> None:
    op.alter_column("orders", "customer_full_name", new_column_name="customer_name")
