"""Deterministic, read-only evidence retrieval by trace ID and/or service around an anchor.

Every query is bounded by ``[anchor - window_before, anchor + window_after]`` (inclusive, UTC),
ordered by ``(occurred_at, id)`` and capped by a limit. Windows and limits outside the configured
maxima are rejected, never clamped. Stored provenance is returned unchanged.
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, NamedTuple, Self

import sqlalchemy as sa
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)
from sqlalchemy import Connection, select, text

from cre.config import Settings
from cre.evidence import EventKind, Evidence, TraceId
from cre.evidence_repo import evidence_from_row, evidence_table


class RetrievalQueryError(ValueError):
    """An out-of-range window or limit. Messages never echo raw input values."""


class EvidenceQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: TraceId | None = None
    service: Annotated[str, StringConstraints(min_length=1, max_length=200)] | None = None
    anchor: AwareDatetime
    window_before: timedelta | None = None
    window_after: timedelta | None = None
    limit: int | None = None
    kinds: Annotated[frozenset[EventKind], Field(min_length=1)] | None = None

    @model_validator(mode="after")
    def _require_filter(self) -> Self:
        if self.trace_id is None and self.service is None:
            raise ValueError("at least one of trace_id or service is required")
        return self


class ResolvedQuery(NamedTuple):
    start: datetime
    end: datetime
    limit: int


class EvidenceQueryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trace_id: str | None
    service: str | None
    anchor: datetime
    start: datetime
    end: datetime
    limit: int
    count: int
    truncated: bool
    items: list[Evidence]


def _check_window(name: str, window: timedelta, max_seconds: int) -> None:
    if window < timedelta(0):
        raise RetrievalQueryError(f"{name} must not be negative")
    if window > timedelta(seconds=max_seconds):
        raise RetrievalQueryError(f"{name} must be at most {max_seconds} seconds")


def resolve_query(query: EvidenceQuery, settings: Settings) -> ResolvedQuery:
    """Apply defaults and validate bounds. Pure: no DB access."""
    default_window = timedelta(seconds=settings.retrieval_default_window_seconds)
    before = query.window_before if query.window_before is not None else default_window
    after = query.window_after if query.window_after is not None else default_window
    limit = query.limit if query.limit is not None else settings.retrieval_default_limit
    _check_window("window_before", before, settings.retrieval_max_window_seconds)
    _check_window("window_after", after, settings.retrieval_max_window_seconds)
    if limit < 1 or limit > settings.retrieval_max_limit:
        raise RetrievalQueryError(f"limit must be between 1 and {settings.retrieval_max_limit}")
    anchor = query.anchor.astimezone(UTC)
    try:
        start = anchor - before
        end = anchor + after
    except OverflowError:
        raise RetrievalQueryError("time window is outside the supported date range") from None
    return ResolvedQuery(start=start, end=end, limit=limit)


def build_select(query: EvidenceQuery, resolved: ResolvedQuery) -> sa.Select[Any]:
    stmt = select(evidence_table)
    if query.trace_id is not None:
        stmt = stmt.where(evidence_table.c.trace_id == query.trace_id)
    if query.service is not None:
        stmt = stmt.where(evidence_table.c.service == query.service)
    if query.kinds is not None:
        stmt = stmt.where(evidence_table.c.kind.in_(sorted(query.kinds)))
    return (
        stmt.where(
            evidence_table.c.occurred_at >= resolved.start,
            evidence_table.c.occurred_at <= resolved.end,
        )
        .order_by(evidence_table.c.occurred_at.asc(), evidence_table.c.id.asc())
        .limit(resolved.limit + 1)
    )


def retrieve_evidence(
    conn: Connection, query: EvidenceQuery, settings: Settings
) -> EvidenceQueryResult:
    """Return matching evidence in ``(occurred_at, id)`` order.

    Marks the caller's current transaction ``READ ONLY`` and sets a transaction-local
    ``statement_timeout``, so call it on a connection with no prior statements in the
    transaction (for example the per-request connection). Does not commit; DB errors propagate.
    """
    resolved = resolve_query(query, settings)
    conn.execute(text("SET TRANSACTION READ ONLY"))
    conn.execute(
        text("SELECT set_config('statement_timeout', :ms, true)"),
        {"ms": str(settings.retrieval_statement_timeout_ms)},
    )
    rows = conn.execute(build_select(query, resolved)).mappings().all()
    items = [evidence_from_row(dict(row)) for row in rows[: resolved.limit]]
    return EvidenceQueryResult(
        trace_id=query.trace_id,
        service=query.service,
        anchor=query.anchor.astimezone(UTC),
        start=resolved.start,
        end=resolved.end,
        limit=resolved.limit,
        count=len(items),
        truncated=len(rows) > resolved.limit,
        items=items,
    )
