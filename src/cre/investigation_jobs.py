"""Durable investigation jobs: the ``investigation_jobs`` / ``investigation_steps`` tables.

The tables mirror migration ``0005_investigation_jobs``. Functions do not commit; the caller
owns the transaction. Workers claim jobs with ``FOR UPDATE SKIP LOCKED`` plus a lease, and
every step write renews the lease in the same transaction. A worker that no longer holds the
lease gets ``LeaseLostError``. Step writes are idempotent on ``(job_id, step_key)``.
"""

import logging
from datetime import datetime
from typing import Annotated, Literal, NamedTuple, Self
from uuid import UUID, uuid4

import sqlalchemy as sa
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    JsonValue,
    StringConstraints,
    ValidationError,
    model_validator,
)
from sqlalchemy import Connection, select, text
from sqlalchemy.dialects import postgresql

from cre.evidence import TraceId, _Model
from cre.redaction import redact

logger = logging.getLogger("cre.worker")

JobStatus = Literal["pending", "running", "completed", "failed"]
JobError = Literal["no_initial_evidence", "max_attempts_exceeded", "internal_error"]

metadata = sa.MetaData()

jobs_table = sa.Table(
    "investigation_jobs",
    metadata,
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
    sa.Index("ix_investigation_jobs_status_created", "status", "created_at"),
)

steps_table = sa.Table(
    "investigation_steps",
    metadata,
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
    sa.CheckConstraint("status IN ('completed','failed')", name="ck_investigation_steps_status"),
)

CLAIM_JOB_SQL = text(
    "UPDATE investigation_jobs SET status = 'running', locked_by = :worker_id, "
    "attempts = attempts + 1, heartbeat_at = now(), "
    "lease_expires_at = now() + make_interval(secs => :lease_seconds), updated_at = now() "
    "WHERE id = (SELECT id FROM investigation_jobs "
    "WHERE status = 'pending' OR (status = 'running' AND lease_expires_at < now()) "
    "ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED) "
    "RETURNING id, request, attempts"
)
FAIL_CLAIMED_SQL = text(
    "UPDATE investigation_jobs SET status = 'failed', error = 'internal_error', "
    "locked_by = NULL, lease_expires_at = NULL, updated_at = now() "
    "WHERE id = :job_id AND locked_by = :worker_id AND status = 'running'"
)
RENEW_LEASE_SQL = text(
    "UPDATE investigation_jobs SET heartbeat_at = now(), "
    "lease_expires_at = now() + make_interval(secs => :lease_seconds), updated_at = now() "
    "WHERE id = :job_id AND locked_by = :worker_id AND status = 'running' "
    "RETURNING id"
)

BoundedText = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class InvestigationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symptom: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None
    timestamp: AwareDatetime
    trace_id: TraceId | None = None
    service: BoundedText | None = None
    request_id: BoundedText | None = None

    @model_validator(mode="after")
    def _require_filter(self) -> Self:
        if self.trace_id is None and self.service is None:
            raise ValueError("at least one of trace_id or service is required")
        return self


class CollectGap(_Model):
    kind: Literal["collector_failed"]
    error_type: str


class CollectStepPayload(_Model):
    query: Literal["trace", "service"]
    start: datetime | None = None
    end: datetime | None = None
    limit: int | None = None
    count: int = 0
    truncated: bool = False
    evidence_ids: list[UUID] = []
    gap: CollectGap | None = None


class StepView(_Model):
    step_key: str
    seq: int
    kind: str
    status: str
    payload: dict[str, JsonValue]
    created_at: datetime


class JobView(_Model):
    id: UUID
    status: JobStatus
    attempts: int
    request: dict[str, JsonValue]
    error: str | None
    result: dict[str, JsonValue] | None
    created_at: datetime
    updated_at: datetime
    steps: list[StepView]


class ClaimedJob(NamedTuple):
    id: UUID
    request: InvestigationRequest
    attempts: int


class LeaseLostError(Exception):
    """The job's lease is no longer held by this worker."""


def submit_job(conn: Connection, request: InvestigationRequest) -> UUID:
    """Store the redacted request. Raises ``ValidationError`` if redaction breaks validity."""
    stored = InvestigationRequest.model_validate(redact(request.model_dump(mode="json")))
    job_id = uuid4()
    conn.execute(
        sa.insert(jobs_table).values(
            id=job_id, status="pending", request=stored.model_dump(mode="json")
        )
    )
    return job_id


def load_steps(conn: Connection, job_id: UUID) -> list[StepView]:
    rows = (
        conn.execute(
            select(steps_table)
            .where(steps_table.c.job_id == job_id)
            .order_by(steps_table.c.seq, steps_table.c.id)
        )
        .mappings()
        .all()
    )
    return [
        StepView(
            step_key=row["step_key"],
            seq=row["seq"],
            kind=row["kind"],
            status=row["status"],
            payload=row["payload"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


def get_job(conn: Connection, job_id: UUID) -> JobView | None:
    row = conn.execute(select(jobs_table).where(jobs_table.c.id == job_id)).mappings().first()
    if row is None:
        return None
    return JobView(
        id=row["id"],
        status=row["status"],
        attempts=row["attempts"],
        request=row["request"],
        error=row["error"],
        result=row["result"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        steps=load_steps(conn, job_id),
    )


def claim_job(conn: Connection, worker_id: str, lease_seconds: float) -> ClaimedJob | None:
    """Claim the next job. A claimed row whose request is invalid is failed with
    ``internal_error`` in the same transaction and the next job is claimed instead."""
    while True:
        row = conn.execute(
            CLAIM_JOB_SQL, {"worker_id": worker_id, "lease_seconds": lease_seconds}
        ).first()
        if row is None:
            return None
        try:
            request = InvestigationRequest.model_validate(row[1])
        except ValidationError:
            conn.execute(FAIL_CLAIMED_SQL, {"job_id": row[0], "worker_id": worker_id})
            logger.error(
                "investigation_invalid_request",
                extra={
                    "event": "investigation_invalid_request",
                    "job_id": str(row[0]),
                    "error": "internal_error",
                },
            )
            continue
        return ClaimedJob(id=row[0], request=request, attempts=int(row[2]))


def record_step(
    conn: Connection,
    job_id: UUID,
    worker_id: str,
    lease_seconds: float,
    *,
    step_key: str,
    seq: int,
    kind: str,
    status: str,
    payload: dict[str, JsonValue],
) -> bool:
    """Renew the lease and insert the step. Returns False when the step key already exists."""
    renewed = conn.execute(
        RENEW_LEASE_SQL,
        {"job_id": job_id, "worker_id": worker_id, "lease_seconds": lease_seconds},
    ).first()
    if renewed is None:
        raise LeaseLostError
    stmt = (
        postgresql.insert(steps_table)
        .values(
            job_id=job_id, step_key=step_key, seq=seq, kind=kind, status=status, payload=payload
        )
        .on_conflict_do_nothing(index_elements=["job_id", "step_key"])
        .returning(steps_table.c.id)
    )
    return conn.execute(stmt).scalar_one_or_none() is not None


def _finish(conn: Connection, job_id: UUID, worker_id: str, **values: object) -> None:
    stmt = (
        sa.update(jobs_table)
        .where(
            jobs_table.c.id == job_id,
            jobs_table.c.locked_by == worker_id,
            jobs_table.c.status == "running",
        )
        .values(locked_by=None, lease_expires_at=None, updated_at=sa.func.now(), **values)
    )
    if conn.execute(stmt).rowcount == 0:
        raise LeaseLostError


def complete_job(
    conn: Connection, job_id: UUID, worker_id: str, result: dict[str, JsonValue]
) -> None:
    _finish(conn, job_id, worker_id, status="completed", result=result)


def fail_job(conn: Connection, job_id: UUID, worker_id: str, error: JobError) -> None:
    _finish(conn, job_id, worker_id, status="failed", error=error)
