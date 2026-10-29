"""Add a unique idempotency_key to evidence_items so duplicate deliveries are no-ops.

Existing rows keep a NULL key; PostgreSQL allows multiple NULLs under a unique constraint.

Revision ID: 0004_evidence_idempotency_key
Revises: 0003_evidence_items

Rollback: alembic downgrade 0003_evidence_items (drops the idempotency_key unique constraint
and column)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_evidence_idempotency_key"
down_revision: str | None = "0003_evidence_items"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("evidence_items", sa.Column("idempotency_key", sa.Text, nullable=True))
    op.create_unique_constraint(
        "uq_evidence_items_idempotency_key", "evidence_items", ["idempotency_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_evidence_items_idempotency_key", "evidence_items", type_="unique")
    op.drop_column("evidence_items", "idempotency_key")
