"""Read-only evidence retrieval endpoint. Query values are never logged or echoed in errors."""

from typing import Annotated, Any

import psycopg.errors
from fastapi import APIRouter, Depends, Request
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import Connection
from sqlalchemy.exc import DBAPIError

from cre.db import get_connection
from cre.ingest import _fail
from cre.retrieval import EvidenceQuery, EvidenceQueryResult, RetrievalQueryError, retrieve_evidence

_INT = TypeAdapter(int)
_PARAM_NAMES = {
    "window_before": "window_before_seconds",
    "window_after": "window_after_seconds",
}

# TODO(owner): the evidence endpoint is unauthenticated; add auth before exposing beyond localhost.
router = APIRouter(tags=["evidence"])


def _problem(loc: tuple[int | str, ...], message: str) -> dict[str, str]:
    locator = ".".join(_PARAM_NAMES.get(str(part), str(part)) for part in loc) or "<root>"
    return {"locator": locator, "message": message}


def _invalid(problems: list[dict[str, str]]) -> None:
    message = "; ".join(f"{p['locator']}: {p['message']}" for p in problems)
    _fail(422, "invalid_query", message, list(problems))


def _parse_query(raw: dict[str, str | None]) -> EvidenceQuery:
    problems: list[dict[str, str]] = []
    data: dict[str, Any] = {}
    for field in ("anchor", "trace_id", "service"):
        if raw[field] is not None:
            data[field] = raw[field]
    for field, param in (*_PARAM_NAMES.items(), ("limit", "limit")):
        value = raw[param]
        if value is None:
            continue
        try:
            data[field] = _INT.validate_python(value)
        except ValidationError as exc:
            problems.extend(_problem((param,), error["msg"]) for error in exc.errors())
    if problems:
        _invalid(problems)
    try:
        return EvidenceQuery.model_validate(data)
    except ValidationError as exc:
        _invalid([_problem(tuple(error["loc"]), error["msg"]) for error in exc.errors()])
        raise


@router.get("/evidence", response_model=EvidenceQueryResult)
def get_evidence(
    request: Request,
    conn: Annotated[Connection, Depends(get_connection)],
    anchor: str | None = None,
    trace_id: str | None = None,
    service: str | None = None,
    window_before_seconds: str | None = None,
    window_after_seconds: str | None = None,
    limit: str | None = None,
) -> EvidenceQueryResult:
    query = _parse_query(
        {
            "anchor": anchor,
            "trace_id": trace_id,
            "service": service,
            "window_before_seconds": window_before_seconds,
            "window_after_seconds": window_after_seconds,
            "limit": limit,
        }
    )
    try:
        return retrieve_evidence(conn, query, request.app.state.settings)
    except RetrievalQueryError as exc:
        _fail(422, "invalid_query", str(exc))
    except DBAPIError as exc:
        if isinstance(exc.orig, psycopg.errors.QueryCanceled):
            _fail(504, "query_timeout", "evidence query exceeded the statement timeout")
        raise
