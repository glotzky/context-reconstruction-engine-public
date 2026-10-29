"""Evidence ingestion endpoints: structured JSON logs and OTLP/JSON traces.

Batches are all-or-nothing: if any record fails normalization, nothing is stored. Request bodies
are untrusted; they are size-limited while streaming and never logged or echoed in errors.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from sqlalchemy import Connection

from cre.db import get_connection
from cre.evidence import Evidence, SourceRef
from cre.evidence_repo import store_evidence
from cre.normalizers import NormalizationError, normalize_log, normalize_span
from cre.otlp import OtlpError, flatten_otlp_spans

DEFAULT_LOG_SOURCE = SourceRef(source_type="structured_log", name="http-ingest")
OTLP_HTTP_SOURCE_NAME = "otlp-json http-ingest"


class LogIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: SourceRef | None = None
    records: Annotated[list[dict[str, JsonValue]], Field(min_length=1)]


class ItemResult(BaseModel):
    index: int
    locator: str
    status: Literal["accepted", "duplicate"]
    id: UUID | None


class IngestResponse(BaseModel):
    accepted: int
    duplicates: int
    results: list[ItemResult]


@dataclass(frozen=True)
class StoreSummary:
    accepted: int
    duplicates: int
    ids: list[UUID | None]


def _fail(
    status_code: int, error: str, message: str, errors: list[dict[str, Any]] | None = None
) -> NoReturn:
    detail: dict[str, Any] = {"error": error, "message": message}
    if errors is not None:
        detail["errors"] = errors
    raise HTTPException(status_code=status_code, detail=detail)


async def read_limited_body(request: Request) -> bytes:
    limit: int = request.app.state.settings.ingest_max_body_bytes
    too_large = f"request body exceeds {limit} bytes"
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            _fail(400, "invalid_content_length", "Content-Length must be an integer")
        if length > limit:
            _fail(413, "payload_too_large", too_large)
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            _fail(413, "payload_too_large", too_large)
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_object(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        _fail(
            400, "invalid_json", f"invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"
        )
    except UnicodeDecodeError:
        _fail(400, "invalid_json", "invalid JSON: body is not valid UTF-8")
    if not isinstance(payload, dict):
        _fail(400, "invalid_json", "request body must be a JSON object")
    return payload


def _check_item_count(field: str, count: int, limit: int) -> None:
    if count > limit:
        _fail(
            422,
            "invalid_payload",
            f"{field}: at most {limit} items allowed, got {count}",
        )


def store_items(conn: Connection, items: Sequence[Evidence]) -> StoreSummary:
    """Store items in order without committing; a None id means the item was a duplicate."""
    ids = [store_evidence(conn, item) for item in items]
    accepted = sum(1 for item_id in ids if item_id is not None)
    return StoreSummary(accepted=accepted, duplicates=len(ids) - accepted, ids=ids)


def _store_batch(
    conn: Connection, items: list[Evidence], locators: list[str], errors: list[dict[str, Any]]
) -> IngestResponse:
    if errors:
        _fail(
            422,
            "invalid_records",
            f"{len(errors)} of {len(locators)} records rejected; nothing was stored",
            errors,
        )
    summary = store_items(conn, items)
    conn.commit()
    return IngestResponse(
        accepted=summary.accepted,
        duplicates=summary.duplicates,
        results=[
            ItemResult(
                index=index,
                locator=locators[index],
                status="accepted" if item_id is not None else "duplicate",
                id=item_id,
            )
            for index, item_id in enumerate(summary.ids)
        ],
    )


# TODO(owner): ingest endpoints are unauthenticated; add auth before exposing beyond localhost.
router = APIRouter(prefix="/ingest", tags=["ingest"])


@router.post("/logs", response_model=IngestResponse)
def ingest_logs(
    request: Request,
    body: Annotated[bytes, Depends(read_limited_body)],
    conn: Annotated[Connection, Depends(get_connection)],
) -> IngestResponse:
    payload = _parse_object(body)
    try:
        envelope = LogIngestRequest.model_validate(payload)
    except ValidationError as exc:
        problems = [
            {
                "locator": ".".join(str(part) for part in error["loc"]) or "<root>",
                "message": error["msg"],
            }
            for error in exc.errors()
        ]
        message = "; ".join(f"{p['locator']}: {p['message']}" for p in problems)
        _fail(422, "invalid_payload", message, problems)
    _check_item_count("records", len(envelope.records), request.app.state.settings.ingest_max_items)

    base_source = envelope.source or DEFAULT_LOG_SOURCE
    retrieved_at = datetime.now(UTC)
    items: list[Evidence] = []
    locators: list[str] = []
    errors: list[dict[str, Any]] = []
    for index, record in enumerate(envelope.records):
        locator = f"records[{index}]"
        locators.append(locator)
        source = (
            base_source
            if base_source.locator is not None
            else base_source.model_copy(update={"locator": locator})
        )
        try:
            items.append(
                normalize_log(
                    record,
                    source=source,
                    retrieved_at=retrieved_at,
                    retrieval_method="http_ingest_log",
                )
            )
        except NormalizationError as exc:
            errors.append({"index": index, "locator": locator, "message": str(exc)})
    return _store_batch(conn, items, locators, errors)


@router.post("/traces", response_model=IngestResponse)
def ingest_traces(
    request: Request,
    body: Annotated[bytes, Depends(read_limited_body)],
    conn: Annotated[Connection, Depends(get_connection)],
) -> IngestResponse:
    payload = _parse_object(body)
    try:
        flat_spans = flatten_otlp_spans(payload)
    except OtlpError as exc:
        _fail(422, "invalid_payload", str(exc))
    if not flat_spans:
        _fail(422, "invalid_payload", "resourceSpans: no spans found")
    _check_item_count("spans", len(flat_spans), request.app.state.settings.ingest_max_items)

    retrieved_at = datetime.now(UTC)
    items: list[Evidence] = []
    locators: list[str] = []
    errors: list[dict[str, Any]] = []
    for index, flat in enumerate(flat_spans):
        locators.append(flat.locator)
        if flat.span is None:
            errors.append({"index": index, "locator": flat.locator, "message": flat.error})
            continue
        try:
            items.append(
                normalize_span(
                    flat.span,
                    source=SourceRef(
                        source_type="otel_span", name=OTLP_HTTP_SOURCE_NAME, locator=flat.locator
                    ),
                    retrieved_at=retrieved_at,
                    retrieval_method="otlp_json_http",
                )
            )
        except NormalizationError as exc:
            errors.append({"index": index, "locator": flat.locator, "message": str(exc)})
    return _store_batch(conn, items, locators, errors)
