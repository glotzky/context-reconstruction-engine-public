import re
from collections.abc import Iterator
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cre.api import create_app
from cre.config import Settings
from cre.report import DEMO_JOB_ID, MemoryReportSource
from cre.report_api import CSP, get_report_source
from tests.report_samples import (
    DEPLOY_ID,
    FAILED_ID,
    INVALID_ID,
    JOB_ID,
    LOG_ID,
    MISSING_ID,
    OUTSIDER_ID,
    PENDING_ID,
    SCRIPT,
    completed_job,
    make_deploy,
    make_job,
    make_log,
)

SECTIONS = [
    "observed-facts",
    "inferred-relationships",
    "hypotheses",
    "uncertainty",
    "unknowns",
    "next-steps",
    "provenance",
]


@pytest.fixture
def source() -> MemoryReportSource:
    jobs = {
        JOB_ID: completed_job(),
        PENDING_ID: make_job(PENDING_ID, status="pending"),
        FAILED_ID: make_job(FAILED_ID, status="failed", error="max_attempts_exceeded"),
        INVALID_ID: make_job(INVALID_ID, result={"not": "a result"}),
    }
    return MemoryReportSource(jobs, [make_log(LOG_ID), make_deploy(), make_log(OUTSIDER_ID)])


@pytest.fixture
def report_client(app: FastAPI, source: MemoryReportSource) -> Iterator[TestClient]:
    def _source() -> Iterator[MemoryReportSource]:
        yield source

    app.dependency_overrides[get_report_source] = _source
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def _report(job_id: object = JOB_ID) -> str:
    return f"/investigations/{job_id}/report"


def _evidence(evidence_id: object, job_id: object = JOB_ID) -> str:
    return f"/investigations/{job_id}/evidence/{evidence_id}"


def test_report_ok(report_client: TestClient) -> None:
    response = report_client.get(_report())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-security-policy"] == CSP
    for section in SECTIONS:
        assert f'<section id="{section}">' in response.text
    assert SCRIPT not in response.text


def test_every_evidence_link_resolves(report_client: TestClient) -> None:
    hrefs = set(re.findall(r'href="([^"]+)"', report_client.get(_report()).text))
    assert hrefs == {_evidence(LOG_ID), _evidence(DEPLOY_ID)}
    for href in hrefs:
        response = report_client.get(href)
        assert response.status_code == 200
        assert response.headers["content-security-policy"] == CSP
        assert f'href="{_report()}"' in response.text


@pytest.mark.parametrize("path", [_report(UUID(int=7)), _report("fake-bad-id-xyz")])
def test_report_not_found(report_client: TestClient, path: str) -> None:
    response = report_client.get(path)
    assert response.status_code == 404
    assert response.headers["content-security-policy"] == CSP
    assert "Not found" in response.text
    assert "fake-bad-id-xyz" not in response.text
    assert str(UUID(int=7)) not in response.text


@pytest.mark.parametrize(
    "path",
    [
        _evidence(UUID(int=8)),
        _evidence(MISSING_ID),
        _evidence(OUTSIDER_ID),
        _evidence("fake-bad-evidence"),
        _evidence(LOG_ID, "fake-bad-job"),
        _evidence(LOG_ID, UUID(int=7)),
        _evidence(LOG_ID, PENDING_ID),
        _evidence(LOG_ID, INVALID_ID),
    ],
)
def test_evidence_not_found(report_client: TestClient, path: str) -> None:
    response = report_client.get(path)
    assert response.status_code == 404
    assert response.headers["content-security-policy"] == CSP
    assert "fake-bad" not in response.text


def test_evidence_outside_job_is_not_loaded(
    report_client: TestClient, source: MemoryReportSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(source, "load_evidence", lambda ids: calls.append(ids) or [])
    assert report_client.get(_evidence(OUTSIDER_ID)).status_code == 404
    assert calls == []


def test_pending_report_shows_status(report_client: TestClient) -> None:
    response = report_client.get(_report(PENDING_ID))
    assert response.status_code == 200
    assert 'id="status"' in response.text and 'id="hypotheses"' not in response.text
    assert "pending" in response.text


def test_failed_report_shows_error_code(report_client: TestClient) -> None:
    response = report_client.get(_report(FAILED_ID))
    assert response.status_code == 200
    assert "max_attempts_exceeded" in response.text
    assert 'id="hypotheses"' not in response.text


def test_invalid_result_report(report_client: TestClient) -> None:
    response = report_client.get(_report(INVALID_ID))
    assert response.status_code == 200
    assert "result could not be read" in response.text
    assert 'id="hypotheses"' not in response.text


def test_no_engine_and_no_demo_returns_503() -> None:
    app = create_app(Settings(DATABASE_URL="", CRE_OTEL_EXPORTER="none", CRE_REPORT_DEMO=False))
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get(_report()).status_code == 503
        assert client.get(_evidence(LOG_ID)).status_code == 503


def test_demo_mode_serves_report_without_db() -> None:
    app = create_app(Settings(DATABASE_URL="", CRE_OTEL_EXPORTER="none", CRE_REPORT_DEMO=True))
    assert app.state.engine is None
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(_report(DEMO_JOB_ID))
        assert response.status_code == 200
        assert 'id="hypotheses"' in response.text
        hrefs = re.findall(r'href="([^"]+)"', response.text)
        assert hrefs
        assert all(client.get(href).status_code == 200 for href in set(hrefs))
