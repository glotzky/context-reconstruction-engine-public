"""Browser form for starting an investigation without JavaScript.

The POST handler reuses the JSON API's validation and submission logic and redirects to the
report page. Errors re-render the form; all output is autoescaped and served with the report CSP.
"""

from typing import Annotated
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import Connection

from cre import investigation_api
from cre.db import get_connection
from cre.ingest import read_limited_body
from cre.report import render_investigate_form
from cre.report_api import _html

# TODO(owner): unauthenticated and no CSRF token; add auth/CSRF before exposing beyond localhost.
router = APIRouter(tags=["investigate"])

FORM_FIELDS: dict[str, int] = {
    "symptom": 2000,
    "timestamp": 200,
    "trace_id": 200,
    "service": 200,
    "request_id": 200,
}
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"


def _form(
    status_code: int,
    errors: list[dict[str, str]] | None = None,
    values: dict[str, str] | None = None,
) -> HTMLResponse:
    return _html(render_investigate_form(values or {}, errors or []), status_code)


def _root_error(status_code: int, message: str) -> HTMLResponse:
    return _form(status_code, [{"locator": "<root>", "message": message}])


@router.get("/investigate", response_class=HTMLResponse)
def investigate_form() -> HTMLResponse:
    return _form(200)


@router.post("/investigate", response_class=HTMLResponse)
def submit_investigate_form(
    request: Request,
    body: Annotated[bytes, Depends(read_limited_body)],
    conn: Annotated[Connection, Depends(get_connection)],
) -> Response:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != FORM_CONTENT_TYPE:
        return _root_error(415, f"form must be submitted as {FORM_CONTENT_TYPE}")
    try:
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=20)
    except (UnicodeDecodeError, ValueError):
        return _root_error(400, "form body could not be parsed")
    values = {
        name: parsed[name][0][:limit]
        for name, limit in FORM_FIELDS.items()
        if name in parsed and parsed[name]
    }
    duplicates = [
        {"locator": name, "message": "field submitted more than once"}
        for name, items in parsed.items()
        if len(items) > 1
    ]
    if duplicates:
        return _form(422, duplicates, values)
    payload = {name: items[0] for name, items in parsed.items() if items[0].strip()}
    try:
        job_id = investigation_api.start_investigation(conn, payload)
    except ValidationError as exc:
        return _form(422, investigation_api.validation_problems(exc), values)
    return RedirectResponse(f"/investigations/{job_id}/report", status_code=303)
