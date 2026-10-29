"""Create the evidence_items table for normalized evidence.

Revision ID: 0003_evidence_items
Revises: 0001_baseline

Rollback: alembic downgrade 0001_baseline (drops evidence_items and its indexes)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_evidence_items"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evidence_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trace_id", sa.Text, nullable=True),
        sa.Column("span_id", sa.Text, nullable=True),
        sa.Column("request_id", sa.Text, nullable=True),
        sa.Column("service", sa.Text, nullable=True),
        sa.Column("deployment_version", sa.Text, nullable=True),
        sa.Column("source", postgresql.JSONB, nullable=False),
        sa.Column("retrieval_method", sa.Text, nullable=False),
        sa.Column("identifiers", postgresql.JSONB, nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed", postgresql.JSONB, nullable=False),
        sa.Column(
            "inferred", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "stored_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "kind IN ('log','span','deployment','commit','config_change')",
            name="ck_evidence_items_kind",
        ),
    )
    op.create_index("ix_evidence_items_occurred_at", "evidence_items", ["occurred_at"])
    op.create_index("ix_evidence_items_trace_id", "evidence_items", ["trace_id"])
    op.create_index("ix_evidence_items_request_id", "evidence_items", ["request_id"])


def downgrade() -> None:
    op.drop_index("ix_evidence_items_request_id", table_name="evidence_items")
    op.drop_index("ix_evidence_items_trace_id", table_name="evidence_items")
    op.drop_index("ix_evidence_items_occurred_at", table_name="evidence_items")
    op.drop_table("evidence_items")
