import asyncio
import copy
import json
import re
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from cre.api import create_app
from cre.config import Settings
from cre.db import get_connection
from cre.ingest import read_limited_body
from cre.redaction import REDACTED
from tests.evidence_samples import (
    FAKE_DB_PASSWORD,
    FAKE_DSN,
    SAMPLE_LOG,
    SAMPLE_OTLP,
    SPAN_ID,
    TRACE_ID,
)

HEX64 = re.compile(r"^[0-9a-f]{64}$")
FAKE_API_KEY = "sk-fakefakefakefakefake0000"
FIRST_SPAN_LOCATOR = "resourceSpans[0].scopeSpans[0].spans[0]"


def _record(**overrides: Any) -> dict[str, Any]:
    return {**SAMPLE_LOG, **overrides}


def _params(fake_conn: MagicMock) -> list[dict[str, Any]]:
    return [call.args[0].compile().params for call in fake_conn.execute.call_args_list]


def _inserted(fake_conn: MagicMock, *ids: Any) -> None:
    fake_conn.execute.return_value.scalar_one_or_none.side_effect = list(ids)


@pytest.fixture
def limited_client(settings: Settings, fake_conn: MagicMock) -> Iterator[TestClient]:
    limited = settings.model_copy(update={"ingest_max_items": 2, "ingest_max_body_bytes": 1024})
    application = create_app(limited)

    def _fake_connection() -> Iterator[MagicMock]:
        yield fake_conn

    application.dependency_overrides[get_connection] = _fake_connection
    with TestClient(application, raise_server_exceptions=False) as test_client:
        yield test_client


def test_valid_log_batch_is_stored_and_committed(client: TestClient, fake_conn: MagicMock) -> None:
    ids = [uuid4(), uuid4()]
    _inserted(fake_conn, *ids)
    records = [_record(), _record(request_id="demo-2", message="second")]
    response = client.post("/ingest/logs", json={"records": records})
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 2 and body["duplicates"] == 0
    assert body["results"] == [
        {"index": 0, "locator": "records[0]", "status": "accepted", "id": str(ids[0])},
        {"index": 1, "locator": "records[1]", "status": "accepted", "id": str(ids[1])},
    ]
    assert fake_conn.execute.call_count == 2
    fake_conn.commit.assert_called_once()


def test_log_insert_params(client: TestClient, fake_conn: MagicMock) -> None:
    _inserted(fake_conn, uuid4())
    assert client.post("/ingest/logs", json={"records": [_record()]}).status_code == 200
    [params] = _params(fake_conn)
    assert params["retrieval_method"] == "http_ingest_log"
    assert params["source"]["source_type"] == "structured_log"
    assert params["source"]["name"] == "http-ingest"
    assert params["source"]["locator"] == "records[0]"
    assert HEX64.match(params["idempotency_key"])
    assert params["kind"] == "log"


def test_client_source_is_stored_with_its_locator(client: TestClient, fake_conn: MagicMock) -> None:
    _inserted(fake_conn, uuid4())
    source = {"source_type": "structured_log", "name": "fake-agent", "locator": "line:7"}
    response = client.post("/ingest/logs", json={"source": source, "records": [_record()]})
    assert response.status_code == 200
    [params] = _params(fake_conn)
    assert params["source"]["name"] == "fake-agent"
    assert params["source"]["locator"] == "line:7"


def test_duplicate_delivery_across_requests(client: TestClient, fake_conn: MagicMock) -> None:
    first_id = uuid4()
    _inserted(fake_conn, first_id, None)
    payload = {"records": [_record()]}
    first = client.post("/ingest/logs", json=payload)
    second = client.post("/ingest/logs", json=payload)
    assert first.status_code == 200 and second.status_code == 200
    keys = [params["idempotency_key"] for params in _params(fake_conn)]
    assert len(keys) == 2 and keys[0] == keys[1]
    assert first.json()["accepted"] == 1
    body = second.json()
    assert body["accepted"] == 0 and body["duplicates"] == 1
    assert body["results"][0]["status"] == "duplicate"
    assert body["results"][0]["id"] is None


def test_duplicate_within_one_batch(client: TestClient, fake_conn: MagicMock) -> None:
    _inserted(fake_conn, uuid4(), None)
    response = client.post("/ingest/logs", json={"records": [_record(), _record()]})
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 1 and body["duplicates"] == 1
    assert [r["status"] for r in body["results"]] == ["accepted", "duplicate"]
    keys = [params["idempotency_key"] for params in _params(fake_conn)]
    assert keys[0] == keys[1]


def test_naive_timestamp_rejects_whole_batch(client: TestClient, fake_conn: MagicMock) -> None:
    records = [_record(), _record(timestamp="2026-01-01T00:00:05"), _record(request_id="x")]
    response = client.post("/ingest/logs", json={"records": records})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_records"
    assert detail["message"] == "1 of 3 records rejected; nothing was stored"
    [error] = detail["errors"]
    assert error["index"] == 1 and error["locator"] == "records[1]"
    assert "timestamp" in error["message"] and "timezone" in error["message"]
    fake_conn.execute.assert_not_called()
    fake_conn.commit.assert_not_called()


def test_missing_message_is_rejected(client: TestClient, fake_conn: MagicMock) -> None:
    record = {k: v for k, v in SAMPLE_LOG.items() if k != "message"}
    response = client.post("/ingest/logs", json={"records": [record]})
    assert response.status_code == 422
    assert response.json()["detail"]["errors"][0]["message"] == "missing required field: message"
    fake_conn.execute.assert_not_called()


@pytest.mark.parametrize("path", ["/ingest/logs", "/ingest/traces"])
def test_invalid_json_body(client: TestClient, path: str) -> None:
    response = client.post(path, content=b'{"records": [', headers={"content-type": "x/y"})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_json"
    assert detail["message"].startswith("invalid JSON:")


def test_empty_body_is_invalid_json(client: TestClient) -> None:
    response = client.post("/ingest/logs", content=b"")
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_json"


@pytest.mark.parametrize("path", ["/ingest/logs", "/ingest/traces"])
def test_non_object_body(client: TestClient, path: str) -> None:
    response = client.post(path, json=[{"records": []}])
    assert response.status_code == 400
    assert response.json()["detail"]["message"] == "request body must be a JSON object"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"records": []},
        {"records": [_record()], "extra": 1},
        {"records": ["not an object"]},
    ],
    ids=["missing_records", "empty_records", "unknown_key", "non_object_record"],
)
def test_envelope_errors(client: TestClient, fake_conn: MagicMock, payload: Any) -> None:
    response = client.post("/ingest/logs", json=payload)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_payload"
    assert detail["errors"] and all("locator" in e and "message" in e for e in detail["errors"])
    assert "not an object" not in json.dumps(detail)
    fake_conn.execute.assert_not_called()


def test_too_many_records(limited_client: TestClient, fake_conn: MagicMock) -> None:
    response = limited_client.post("/ingest/logs", json={"records": [{"a": 1}] * 3})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_payload"
    assert detail["message"] == "records: at most 2 items allowed, got 3"
    fake_conn.execute.assert_not_called()


@pytest.mark.parametrize("path", ["/ingest/logs", "/ingest/traces"])
def test_oversized_body(limited_client: TestClient, fake_conn: MagicMock, path: str) -> None:
    response = limited_client.post(path, content=b"{" + b" " * 2000 + b"}")
    assert response.status_code == 413
    detail = response.json()["detail"]
    assert detail == {"error": "payload_too_large", "message": "request body exceeds 1024 bytes"}
    fake_conn.execute.assert_not_called()


def _request(headers: list[tuple[bytes, bytes]], chunks: list[bytes], limit: int) -> Request:
    app = SimpleNamespace(
        state=SimpleNamespace(settings=SimpleNamespace(ingest_max_body_bytes=limit))
    )
    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers, "app": app}
    messages = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    ]

    async def receive() -> dict[str, Any]:
        return messages.pop(0)

    return Request(scope, receive)


def test_read_limited_body_chunked_over_limit() -> None:
    request = _request([], [b"a" * 600, b"b" * 600], limit=1024)
    with pytest.raises(HTTPException) as info:
        asyncio.run(read_limited_body(request))
    assert info.value.status_code == 413


def test_read_limited_body_chunked_under_limit() -> None:
    request = _request([], [b"ab", b"cd", b""], limit=1024)
    assert asyncio.run(read_limited_body(request)) == b"abcd"


def test_read_limited_body_declared_length_over_limit() -> None:
    request = _request([(b"content-length", b"5000")], [b"{}"], limit=1024)
    with pytest.raises(HTTPException) as info:
        asyncio.run(read_limited_body(request))
    assert info.value.status_code == 413


def test_read_limited_body_non_integer_content_length() -> None:
    request = _request([(b"content-length", b"abc")], [b"{}"], limit=1024)
    with pytest.raises(HTTPException) as info:
        asyncio.run(read_limited_body(request))
    assert info.value.status_code == 400


def test_log_secrets_are_redacted_before_insert(client: TestClient, fake_conn: MagicMock) -> None:
    _inserted(fake_conn, uuid4())
    record = _record(message=f"db down {FAKE_DSN}", config={"api_key": FAKE_API_KEY})
    response = client.post("/ingest/logs", json={"records": [record]})
    assert response.status_code == 200
    [params] = _params(fake_conn)
    dumped = json.dumps(params, default=str)
    assert FAKE_DB_PASSWORD not in dumped
    assert FAKE_API_KEY not in dumped
    assert REDACTED in dumped


def test_error_responses_do_not_echo_secrets(client: TestClient, fake_conn: MagicMock) -> None:
    bad = _record(timestamp="2026-01-01T00:00:05", message=f"{FAKE_DSN} {FAKE_API_KEY}")
    for payload in (
        {"records": [bad]},
        {"records": [bad], "token": FAKE_API_KEY},
        {"records": [FAKE_DSN]},
    ):
        response = client.post("/ingest/logs", json=payload)
        assert response.status_code == 422
        assert FAKE_DB_PASSWORD not in response.text
        assert FAKE_API_KEY not in response.text
    fake_conn.execute.assert_not_called()


def test_valid_otlp_is_stored(client: TestClient, fake_conn: MagicMock) -> None:
    _inserted(fake_conn, uuid4(), uuid4())
    response = client.post("/ingest/traces", json=SAMPLE_OTLP)
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] == 2 and body["duplicates"] == 0
    assert body["results"][0]["locator"] == FIRST_SPAN_LOCATOR
    first, _ = _params(fake_conn)
    assert first["trace_id"] == TRACE_ID
    assert first["span_id"] == SPAN_ID
    assert first["service"] == "cre-lab-api"
    assert first["retrieval_method"] == "otlp_json_http"
    assert first["source"] == {
        "source_type": "otel_span",
        "name": "otlp-json http-ingest",
        "uri": None,
        "locator": FIRST_SPAN_LOCATOR,
    }
    fake_conn.commit.assert_called_once()


def test_otlp_db_password_attribute_is_redacted(client: TestClient, fake_conn: MagicMock) -> None:
    _inserted(fake_conn, uuid4(), uuid4())
    assert client.post("/ingest/traces", json=SAMPLE_OTLP).status_code == 200
    first, _ = _params(fake_conn)
    assert first["observed"]["attributes"]["db.password"] == REDACTED
    assert FAKE_DB_PASSWORD not in json.dumps(_params(fake_conn), default=str)


def test_otlp_duplicate_redelivery(client: TestClient, fake_conn: MagicMock) -> None:
    _inserted(fake_conn, uuid4(), uuid4(), None, None)
    first = client.post("/ingest/traces", json=SAMPLE_OTLP)
    second = client.post("/ingest/traces", json=SAMPLE_OTLP)
    keys = [params["idempotency_key"] for params in _params(fake_conn)]
    assert keys[:2] == keys[2:]
    assert first.json()["accepted"] == 2
    assert second.json()["duplicates"] == 2 and second.json()["accepted"] == 0


def test_otlp_bad_time_rejects_batch(client: TestClient, fake_conn: MagicMock) -> None:
    payload = copy.deepcopy(SAMPLE_OTLP)
    payload["resourceSpans"][0]["scopeSpans"][0]["spans"][1]["startTimeUnixNano"] = "soon"
    response = client.post("/ingest/traces", json=payload)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_records"
    assert detail["errors"] == [
        {
            "index": 1,
            "locator": "resourceSpans[0].scopeSpans[0].spans[1]",
            "message": "startTimeUnixNano: must be a decimal integer",
        }
    ]
    fake_conn.execute.assert_not_called()
    fake_conn.commit.assert_not_called()


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"resourceSpans": {}}, "resourceSpans: required list"),
        ({"resourceSpans": []}, "resourceSpans: no spans found"),
    ],
)
def test_otlp_invalid_payload(
    client: TestClient, fake_conn: MagicMock, payload: Any, message: str
) -> None:
    response = client.post("/ingest/traces", json=payload)
    assert response.status_code == 422
    assert response.json()["detail"] == {"error": "invalid_payload", "message": message}
    fake_conn.execute.assert_not_called()


def test_otlp_too_many_spans(limited_client: TestClient, fake_conn: MagicMock) -> None:
    span = {"traceId": TRACE_ID, "spanId": SPAN_ID, "name": "s"}
    payload = {"resourceSpans": [{"scopeSpans": [{"spans": [span] * 3}]}]}
    response = limited_client.post("/ingest/traces", json=payload)
    assert response.status_code == 422
    assert response.json()["detail"]["message"] == "spans: at most 2 items allowed, got 3"
    fake_conn.execute.assert_not_called()
