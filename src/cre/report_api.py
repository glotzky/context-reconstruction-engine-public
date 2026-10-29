"""HTML investigation report and evidence pages.

Every request re-loads the job. Evidence is served only if its id belongs to that job's result.
Unknown, malformed or non-member ids return a generic 404 page that never echoes the id.
"""

import logging
from collections.abc import Iterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from cre.db import get_connection
from cre.report import (
    DbReportSource,
    ReportSource,
    allowed_evidence_ids,
    load_report,
    render_evidence,
    render_not_found,
    render_report,
)

# TODO(owner): report pages are unauthenticated; add auth before exposing beyond localhost.
router = APIRouter(tags=["reports"])
logger = logging.getLogger("cre.report_api")

CSP = "default-src 'none'; style-src 'unsafe-inline'"


def get_report_source(request: Request) -> Iterator[ReportSource]:
    source: ReportSource | None = request.app.state.report_source
    if source is not None:
        yield source
        return
    for conn in get_connection(request):
        yield DbReportSource(conn)


def _html(content: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(content, status_code=status_code, headers={"Content-Security-Policy": CSP})


def _not_found() -> HTMLResponse:
    return _html(render_not_found(), 404)


def _parse_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


@router.get("/investigations/{job_id}/report", response_class=HTMLResponse)
def read_report(
    job_id: str, source: Annotated[ReportSource, Depends(get_report_source)]
) -> HTMLResponse:
    parsed = _parse_uuid(job_id)
    job = source.get_job(parsed) if parsed is not None else None
    if job is None:
        return _not_found()
    view = load_report(source, job)
    if job.status == "completed" and not view.complete:
        logger.warning(
            "report_result_unreadable",
            extra={"event": "report_result_unreadable", "job_id": str(job.id)},
        )
    return _html(render_report(view, lambda eid: f"/investigations/{job.id}/evidence/{eid}"))


@router.get("/investigations/{job_id}/evidence/{evidence_id}", response_class=HTMLResponse)
def read_evidence(
    job_id: str, evidence_id: str, source: Annotated[ReportSource, Depends(get_report_source)]
) -> HTMLResponse:
    parsed_job, parsed_evidence = _parse_uuid(job_id), _parse_uuid(evidence_id)
    if parsed_job is None or parsed_evidence is None:
        return _not_found()
    job = source.get_job(parsed_job)
    if job is None or parsed_evidence not in allowed_evidence_ids(job):
        return _not_found()
    items = [item for item in source.load_evidence([parsed_evidence]) if item.id == parsed_evidence]
    if not items:
        return _not_found()
    return _html(render_evidence(job.id, items[0], f"/investigations/{job.id}/report"))
