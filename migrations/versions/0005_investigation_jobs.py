"""Create investigation_jobs and investigation_steps for durable investigation jobs.

Each job is claimed by one worker under a lease. Every completed step is stored once per
``(job_id, step_key)`` so a resumed run never repeats it.

Revision ID: 0005_investigation_jobs
Revises: 0004_evidence_idempotency_key

Rollback: alembic downgrade 0004_evidence_idempotency_key (drops investigation_steps and
investigation_jobs)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_investigation_jobs"
down_revision: str | None = "0004_evidence_idempotency_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "investigation_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("status", sa.Text, nullable=False, server_default=sa.text("'pending'")),
        sa.Column("request", postgresql.JSONB, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("locked_by", sa.Text, nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", postgresql.JSONB, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','completed','failed')",
            name="ck_investigation_jobs_status",
        ),
    )
    op.create_index(
        "ix_investigation_jobs_status_created", "investigation_jobs", ["status", "created_at"]
    )
    op.create_table(
        "investigation_steps",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("investigation_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_key", sa.Text, nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("job_id", "step_key", name="uq_investigation_steps_job_step"),
        sa.CheckConstraint("kind IN ('collect','checkpoint')", name="ck_investigation_steps_kind"),
        sa.CheckConstraint(
            "status IN ('completed','failed')", name="ck_investigation_steps_status"
        ),
    )


def downgrade() -> None:
    op.drop_table("investigation_steps")
    op.drop_index("ix_investigation_jobs_status_created", table_name="investigation_jobs")
    op.drop_table("investigation_jobs")
