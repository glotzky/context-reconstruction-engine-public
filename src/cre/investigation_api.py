"""Investigation job endpoints: submit an investigation and read its status, steps and result.

Submissions are untrusted: they are size-limited, strictly validated, redacted before storage
and never echoed in errors.
"""

from collections.abc import Mapping
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import ValidationError
from sqlalchemy import Connection

from cre.db import get_connection
from cre.ingest import _fail, _parse_object, read_limited_body
from cre.investigation_jobs import InvestigationRequest, JobView, get_job, submit_job

# TODO(owner): investigation endpoints are unauthenticated; add auth before exposing beyond
# localhost.
router = APIRouter(tags=["investigations"])


def validation_problems(exc: ValidationError) -> list[dict[str, str]]:
    return [
        {
            "locator": ".".join(str(part) for part in error["loc"]) or "<root>",
            "message": error["msg"],
        }
        for error in exc.errors()
    ]


def start_investigation(conn: Connection, payload: Mapping[str, Any]) -> UUID:
    """Validate and enqueue a submission, then commit. Raises ValidationError without committing."""
    request = InvestigationRequest.model_validate(payload)
    job_id = submit_job(conn, request)
    conn.commit()
    return job_id


@router.post("/investigations", status_code=202)
def submit_investigation(
    body: Annotated[bytes, Depends(read_limited_body)],
    conn: Annotated[Connection, Depends(get_connection)],
) -> dict[str, str]:
    payload = _parse_object(body)
    try:
        job_id = start_investigation(conn, payload)
    except ValidationError as exc:
        problems = validation_problems(exc)
        message = "; ".join(f"{p['locator']}: {p['message']}" for p in problems)
        _fail(422, "invalid_submission", message, problems)
    return {"id": str(job_id), "status": "pending"}


@router.get("/investigations/{job_id}", response_model=JobView)
def read_investigation(
    job_id: str, conn: Annotated[Connection, Depends(get_connection)]
) -> JobView:
    try:
        parsed = UUID(job_id)
    except ValueError:
        _fail(422, "invalid_job_id", "job_id must be a UUID")
    job = get_job(conn, parsed)
    if job is None:
        _fail(404, "not_found", "investigation not found")
    return job
