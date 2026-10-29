import json
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from cre import investigation_api, report_api
from cre.config import Settings
from cre.investigation_jobs import InvestigationRequest
from tests.evidence_samples import TRACE_ID

JOB_ID = UUID(int=43)
TIMESTAMP = "2026-01-01T00:00:05Z"
VALID = {"timestamp": TIMESTAMP, "trace_id": TRACE_ID, "symptom": "fake 500s"}
FIELDS = ["symptom", "timestamp", "trace_id", "service", "request_id"]
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


@pytest.fixture
def submitted(monkeypatch: pytest.MonkeyPatch) -> list[InvestigationRequest]:
    calls: list[InvestigationRequest] = []

    def fake_submit(_conn: Any, request: InvestigationRequest) -> UUID:
        calls.append(request)
        return JOB_ID

    monkeypatch.setattr(investigation_api, "submit_job", fake_submit)
    return calls


def _post(client: TestClient, data: Any) -> Any:
    return client.post("/investigate", data=data, follow_redirects=False)


def _assert_form_error(response: Any, status_code: int) -> None:
    assert response.status_code == status_code
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-security-policy"] == report_api.CSP
    assert 'id="errors"' in response.text
    assert 'action="/investigate"' in response.text


def test_get_form_renders_with_csp(client: TestClient) -> None:
    response = client.get("/investigate")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-security-policy"] == report_api.CSP
    assert 'action="/investigate"' in response.text
    assert 'method="post"' in response.text
    for field in FIELDS:
        assert f'name="{field}"' in response.text
        assert f'<label for="{field}">' in response.text
    assert '<textarea id="symptom" name="symptom" maxlength="2000"' in response.text
    assert response.text.count('maxlength="200"') == 4
    assert 'placeholder="2026-01-01T00:00:05Z"' in response.text
    assert "<script" not in response.text
    assert 'id="errors"' not in response.text


def test_get_form_renders_in_demo_mode_without_db() -> None:
    from cre.api import create_app

    app = create_app(Settings(DATABASE_URL="", CRE_OTEL_EXPORTER="none", CRE_REPORT_DEMO=True))
    with TestClient(app, raise_server_exceptions=False) as demo:
        assert demo.get("/investigate").status_code == 200
        response = demo.post("/investigate", data=VALID, follow_redirects=False)
        assert response.status_code == 503


def test_valid_submission_redirects_to_report(
    client: TestClient, fake_conn: MagicMock, submitted: list[InvestigationRequest]
) -> None:
    response = _post(client, VALID)
    assert response.status_code == 303
    assert response.headers["location"] == f"/investigations/{JOB_ID}/report"
    assert len(submitted) == 1
    assert submitted[0].trace_id == TRACE_ID
    assert submitted[0].timestamp.isoformat() == "2026-01-01T00:00:05+00:00"
    fake_conn.commit.assert_called_once()


def test_content_type_with_charset_is_accepted(
    client: TestClient, submitted: list[InvestigationRequest]
) -> None:
    body = f"timestamp={TIMESTAMP}&service=fake-svc"
    headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
    response = client.post("/investigate", content=body, headers=headers, follow_redirects=False)
    assert response.status_code == 303
    assert submitted[0].service == "fake-svc"


def test_blank_optional_fields_are_ignored(
    client: TestClient, submitted: list[InvestigationRequest]
) -> None:
    data = {"timestamp": TIMESTAMP, "trace_id": TRACE_ID, "service": "", "request_id": ""}
    response = _post(client, {**data, "symptom": "  "})
    assert response.status_code == 303
    request = submitted[0]
    assert request.service is None
    assert request.request_id is None
    assert request.symptom is None


@pytest.mark.parametrize("data", [{}, dict.fromkeys(FIELDS, " ")])
def test_empty_submission_rejected(
    client: TestClient,
    fake_conn: MagicMock,
    submitted: list[InvestigationRequest],
    data: dict[str, str],
) -> None:
    response = client.post("/investigate", data=data, headers=FORM, follow_redirects=False)
    _assert_form_error(response, 422)
    assert "<code>timestamp</code>" in response.text
    assert submitted == []
    fake_conn.commit.assert_not_called()


@pytest.mark.parametrize(
    ("data", "locator"),
    [
        ({**VALID, "trace_id": "fake-bad-trace-xyz"}, "trace_id"),
        ({**VALID, "timestamp": "2026-01-01T00:00:05"}, "timestamp"),
        ({"timestamp": TIMESTAMP, "request_id": "fake-req-1"}, "&lt;root&gt;"),
    ],
)
def test_invalid_input_rerenders_with_error(
    client: TestClient,
    fake_conn: MagicMock,
    submitted: list[InvestigationRequest],
    data: dict[str, str],
    locator: str,
) -> None:
    response = _post(client, data)
    _assert_form_error(response, 422)
    assert f"<code>{locator}</code>" in response.text
    for name, value in data.items():
        if name == "symptom":
            assert f">{value}</textarea>" in response.text
        else:
            assert f'value="{value}"' in response.text
    assert submitted == []
    fake_conn.commit.assert_not_called()


def test_oversized_field_rejected(
    client: TestClient, fake_conn: MagicMock, submitted: list[InvestigationRequest]
) -> None:
    response = _post(client, {**VALID, "symptom": "a" * 2001})
    _assert_form_error(response, 422)
    assert "<code>symptom</code>" in response.text
    textarea = response.text.split('maxlength="2000" rows="4" cols="60">', 1)[1]
    content = textarea.split("</textarea>", 1)[0]
    assert content == "a" * 2000
    assert submitted == []
    fake_conn.commit.assert_not_called()


def test_oversized_body_rejected(
    client: TestClient, settings: Settings, submitted: list[InvestigationRequest]
) -> None:
    response = _post(client, {**VALID, "symptom": "a" * (settings.ingest_max_body_bytes + 1)})
    assert response.status_code == 413
    assert response.json()["detail"]["error"] == "payload_too_large"
    assert submitted == []


def test_html_injection_is_escaped(
    client: TestClient, fake_conn: MagicMock, submitted: list[InvestigationRequest]
) -> None:
    payload = '<script>alert("fake")</script>'
    response = _post(client, {**VALID, "symptom": payload, "trace_id": "fake-bad"})
    _assert_form_error(response, 422)
    assert "&lt;script&gt;alert(&#34;fake&#34;)&lt;/script&gt;" in response.text
    assert "<script" not in response.text

    response = _post(client, {**VALID, payload: "fake-value"})
    _assert_form_error(response, 422)
    assert "<code>&lt;script&gt;alert(&#34;fake&#34;)&lt;/script&gt;</code>" in response.text
    assert "<script" not in response.text
    assert "fake-value" not in response.text
    assert submitted == []
    fake_conn.commit.assert_not_called()


def test_duplicate_field_rejected(
    client: TestClient, fake_conn: MagicMock, submitted: list[InvestigationRequest]
) -> None:
    body = f"timestamp={TIMESTAMP}&trace_id={TRACE_ID}&trace_id={TRACE_ID}"
    response = client.post("/investigate", content=body, headers=FORM, follow_redirects=False)
    _assert_form_error(response, 422)
    assert "<code>trace_id</code>: field submitted more than once" in response.text
    assert submitted == []
    fake_conn.commit.assert_not_called()


def test_wrong_content_type_rejected(
    client: TestClient, fake_conn: MagicMock, submitted: list[InvestigationRequest]
) -> None:
    response = client.post(
        "/investigate",
        content=json.dumps(VALID),
        headers={"Content-Type": "application/json"},
        follow_redirects=False,
    )
    _assert_form_error(response, 415)
    assert "form must be submitted as application/x-www-form-urlencoded" in response.text
    assert submitted == []
    fake_conn.commit.assert_not_called()


@pytest.mark.parametrize(
    "body", [b"timestamp=\xff\xfe-fake", "&".join(f"f{i}=x" for i in range(25)).encode()]
)
def test_unparseable_body_rejected(
    client: TestClient,
    fake_conn: MagicMock,
    submitted: list[InvestigationRequest],
    body: bytes,
) -> None:
    response = client.post("/investigate", content=body, headers=FORM, follow_redirects=False)
    _assert_form_error(response, 400)
    assert "form body could not be parsed" in response.text
    assert "-fake" not in response.text
    assert submitted == []
    fake_conn.commit.assert_not_called()


def test_redaction_failure_rerenders_without_commit(
    client: TestClient, fake_conn: MagicMock
) -> None:
    response = _post(client, {"timestamp": TIMESTAMP, "service": "token=x," * 25})
    _assert_form_error(response, 422)
    fake_conn.execute.assert_not_called()
    fake_conn.commit.assert_not_called()
