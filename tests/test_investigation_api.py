from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from cre import investigation_api
from cre.api import create_app
from cre.config import Settings
from cre.investigation_jobs import InvestigationRequest, JobView, StepView
from tests.evidence_samples import TRACE_ID

JOB_ID = UUID(int=42)
CREATED = datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)
VALID = {"timestamp": "2026-01-01T00:00:05Z", "trace_id": TRACE_ID, "symptom": "fake 500s"}


@pytest.fixture
def submitted(monkeypatch: pytest.MonkeyPatch) -> list[InvestigationRequest]:
    calls: list[InvestigationRequest] = []

    def fake_submit(_conn: Any, request: InvestigationRequest) -> UUID:
        calls.append(request)
        return JOB_ID

    monkeypatch.setattr(investigation_api, "submit_job", fake_submit)
    return calls


def test_post_valid_returns_202(
    client: TestClient, fake_conn: MagicMock, submitted: list[InvestigationRequest]
) -> None:
    response = client.post("/investigations", json=VALID)
    assert response.status_code == 202
    assert response.json() == {"id": str(JOB_ID), "status": "pending"}
    assert submitted[0].trace_id == TRACE_ID
    fake_conn.commit.assert_called_once()


@pytest.mark.parametrize(
    ("payload", "secret"),
    [
        ({**VALID, "trace_id": "fake-bad-trace-xyz"}, "fake-bad-trace-xyz"),
        ({**VALID, "timestamp": "2026-01-01T00:00:05"}, "2026-01-01T00:00:05"),
        ({"timestamp": "2026-01-01T00:00:05Z", "symptom": "fake-only-symptom"}, "fake-only"),
        ({**VALID, "extra": "fake-extra-value"}, "fake-extra-value"),
        ({**VALID, "symptom": "fakesym" * 300}, "fakesymfakesym"),
    ],
)
def test_post_invalid_returns_422_without_echo(
    client: TestClient,
    fake_conn: MagicMock,
    submitted: list[InvestigationRequest],
    payload: dict[str, Any],
    secret: str,
) -> None:
    response = client.post("/investigations", json=payload)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_submission"
    assert all(set(p) == {"locator", "message"} for p in detail["errors"])
    assert secret not in response.text
    assert submitted == []
    fake_conn.commit.assert_not_called()


@pytest.mark.parametrize(
    ("payload", "secret"),
    [
        ({**VALID, "symptom": "pwd=a " * 333}, "pwd=a"),
        ({"timestamp": VALID["timestamp"], "service": "token=x," * 25}, "token=x"),
    ],
)
def test_post_invalid_after_redaction_returns_422_and_is_not_stored(
    client: TestClient, fake_conn: MagicMock, payload: dict[str, Any], secret: str
) -> None:
    response = client.post("/investigations", json=payload)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_submission"
    assert all(set(p) == {"locator", "message"} for p in detail["errors"])
    assert secret not in response.text
    fake_conn.execute.assert_not_called()
    fake_conn.commit.assert_not_called()


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]"])
def test_post_non_json_or_non_object_returns_400(
    client: TestClient, submitted: list[InvestigationRequest], body: bytes
) -> None:
    response = client.post(
        "/investigations", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_json"
    assert submitted == []


def test_post_oversized_returns_413(
    client: TestClient, settings: Settings, submitted: list[InvestigationRequest]
) -> None:
    body = b'{"symptom": "' + b"x" * settings.ingest_max_body_bytes + b'"}'
    response = client.post(
        "/investigations", content=body, headers={"content-type": "application/json"}
    )
    assert response.status_code == 413
    assert submitted == []


def _view() -> JobView:
    return JobView(
        id=JOB_ID,
        status="completed",
        attempts=1,
        request={"timestamp": "2026-01-01T00:00:05Z", "trace_id": TRACE_ID},
        error=None,
        result={"stop_reason": "concluded"},
        created_at=CREATED,
        updated_at=CREATED,
        steps=[
            StepView(
                step_key="collect:trace",
                seq=0,
                kind="collect",
                status="completed",
                payload={"query": "trace"},
                created_at=CREATED,
            )
        ],
    )


def test_get_existing_returns_job_with_steps(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested: list[UUID] = []

    def fake_get(_conn: Any, job_id: UUID) -> JobView:
        requested.append(job_id)
        return _view()

    monkeypatch.setattr(investigation_api, "get_job", fake_get)
    response = client.get(f"/investigations/{JOB_ID}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(JOB_ID) and body["status"] == "completed"
    assert body["result"] == {"stop_reason": "concluded"}
    assert [s["step_key"] for s in body["steps"]] == ["collect:trace"]
    assert requested == [JOB_ID]


def test_get_unknown_returns_404(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(investigation_api, "get_job", lambda _conn, _id: None)
    response = client.get(f"/investigations/{JOB_ID}")
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "not_found"


def test_get_non_uuid_returns_422(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    get = MagicMock(name="get_job")
    monkeypatch.setattr(investigation_api, "get_job", get)
    response = client.get("/investigations/not-a-uuid")
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "invalid_job_id"
    assert "not-a-uuid" not in response.text
    get.assert_not_called()


def test_post_without_engine_returns_503(
    settings: Settings, submitted: list[InvestigationRequest]
) -> None:
    app = create_app(settings.model_copy(update={"database_url": ""}))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/investigations", json=VALID)
    assert response.status_code == 503
    assert submitted == []
