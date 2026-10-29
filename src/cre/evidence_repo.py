"""Evidence storage: the ``evidence_items`` table plus store and load by id.

The table mirrors migrations ``0003_evidence_items`` and ``0004_evidence_idempotency_key``.
Functions do not commit; the caller owns the transaction. Items are redacted again before
storage, and inserts skip rows whose ``idempotency_key`` already exists.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import Connection, select
from sqlalchemy.dialects import postgresql

from cre.evidence import EVIDENCE_ADAPTER, Evidence
from cre.redaction import redact_evidence

MAX_BATCH_LOAD = 500

metadata = sa.MetaData()

evidence_table = sa.Table(
    "evidence_items",
    metadata,
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
    sa.Column("inferred", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    sa.Column("idempotency_key", sa.Text, nullable=True),
    sa.Column(
        "stored_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
    sa.UniqueConstraint("idempotency_key", name="uq_evidence_items_idempotency_key"),
    sa.CheckConstraint(
        "kind IN ('log','span','deployment','commit','config_change')",
        name="ck_evidence_items_kind",
    ),
    sa.Index("ix_evidence_items_occurred_at", "occurred_at"),
    sa.Index("ix_evidence_items_trace_id", "trace_id"),
    sa.Index("ix_evidence_items_request_id", "request_id"),
)


def idempotency_key(item: Evidence) -> str:
    """Stable key for duplicate detection; ignores ``id``, ``retrieved_at`` and locator/uri."""
    encoded = json.dumps(
        {
            "kind": item.kind,
            "source_type": item.provenance.source.source_type,
            "source_name": item.provenance.source.name,
            "identifiers": dict(item.provenance.identifiers),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def evidence_to_row(item: Evidence) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": item.kind,
        "occurred_at": item.timestamp,
        "trace_id": item.trace_id,
        "span_id": item.span_id,
        "request_id": item.request_id,
        "service": item.service,
        "deployment_version": item.deployment_version,
        "source": item.provenance.source.model_dump(mode="json"),
        "retrieval_method": item.provenance.retrieval_method,
        "identifiers": dict(item.provenance.identifiers),
        "retrieved_at": item.provenance.retrieved_at,
        "observed": item.observed.model_dump(mode="json"),
        "inferred": [inference.model_dump(mode="json") for inference in item.inferred],
        "idempotency_key": idempotency_key(item),
    }


def evidence_from_row(row: Mapping[str, Any]) -> Evidence:
    return EVIDENCE_ADAPTER.validate_python(
        {
            "id": row["id"],
            "kind": row["kind"],
            "timestamp": row["occurred_at"],
            "trace_id": row["trace_id"],
            "span_id": row["span_id"],
            "request_id": row["request_id"],
            "service": row["service"],
            "deployment_version": row["deployment_version"],
            "provenance": {
                "source": row["source"],
                "retrieval_method": row["retrieval_method"],
                "identifiers": row["identifiers"],
                "retrieved_at": row["retrieved_at"],
            },
            "observed": row["observed"],
            "inferred": row["inferred"],
        }
    )


def store_evidence(conn: Connection, item: Evidence) -> UUID | None:
    """Insert one item. Returns its id, or None when the idempotency key already exists."""
    redacted = redact_evidence(item)
    stmt = (
        postgresql.insert(evidence_table)
        .values(**evidence_to_row(redacted))
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(evidence_table.c.id)
    )
    inserted: UUID | None = conn.execute(stmt).scalar_one_or_none()
    return inserted


def load_evidence(conn: Connection, evidence_id: UUID) -> Evidence | None:
    row = (
        conn.execute(select(evidence_table).where(evidence_table.c.id == evidence_id))
        .mappings()
        .first()
    )
    if row is None:
        return None
    return evidence_from_row(dict(row))


def load_evidence_many(conn: Connection, ids: Sequence[UUID]) -> list[Evidence]:
    """Load items by id in one query. Missing ids are absent; result order is unspecified."""
    unique = list(dict.fromkeys(ids))
    if not unique:
        return []
    if len(unique) > MAX_BATCH_LOAD:
        raise ValueError(f"at most {MAX_BATCH_LOAD} evidence ids can be loaded at once")
    rows = conn.execute(select(evidence_table).where(evidence_table.c.id.in_(unique))).mappings()
    return [evidence_from_row(dict(row)) for row in rows.all()]
