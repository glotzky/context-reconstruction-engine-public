from typing import Any
from unittest.mock import MagicMock

import psycopg.errors
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError, OperationalError

from cre.api import create_app
from cre.config import Settings
from cre.evidence_repo import evidence_to_row
from cre.normalizers import normalize_log, normalize_span
from tests.evidence_samples import (
    LOG_SOURCE,
    RETRIEVED_AT,
    SAMPLE_LOG,
    SAMPLE_SPAN,
    SPAN_SOURCE,
    TRACE_ID,
)

ANCHOR = "2026-01-01T00:00:05Z"
LOG = normalize_log(SAMPLE_LOG, source=LOG_SOURCE, retrieved_at=RETRIEVED_AT)
SPAN = normalize_span(SAMPLE_SPAN, source=SPAN_SOURCE, retrieved_at=RETRIEVED_AT)


def _db_returns(conn: MagicMock, rows: list[dict[str, Any]]) -> None:
    result = MagicMock(name="select_result")
    result.mappings.return_value.all.return_value = rows
    conn.execute.side_effect = [MagicMock(), MagicMock(), result]


def _assert_invalid(response: Any) -> dict[str, Any]:
    assert response.status_code == 422
    detail: dict[str, Any] = response.json()["detail"]
    assert detail["error"] == "invalid_query"
    assert isinstance(detail["message"], str)
    return detail


def test_happy_path(client: TestClient, fake_conn: MagicMock) -> None:
    _db_returns(
        fake_conn, [{**evidence_to_row(item), "stored_at": RETRIEVED_AT} for item in (LOG, SPAN)]
    )
    response = client.get(
        "/evidence",
        params={
            "anchor": ANCHOR,
            "service": "cre-lab-api",
            "window_before_seconds": "60",
            "window_after_seconds": "0",
            "limit": "5",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["start"].startswith("2025-12-31T23:59:05")
    assert body["end"].startswith("2026-01-01T00:00:05")
    assert body["limit"] == 5
    assert body["count"] == 2
    assert body["truncated"] is False
    assert [item["id"] for item in body["items"]] == [str(LOG.id), str(SPAN.id)]
    assert body["items"][0]["provenance"] == LOG.provenance.model_dump(mode="json")
    fake_conn.commit.assert_not_called()


def test_trace_lookup_uses_defaults(client: TestClient, fake_conn: MagicMock) -> None:
    _db_returns(fake_conn, [])
    response = client.get("/evidence", params={"anchor": ANCHOR, "trace_id": TRACE_ID})
    assert response.status_code == 200
    body = response.json()
    assert body["trace_id"] == TRACE_ID
    assert body["limit"] == 200
    assert body["items"] == [] and body["count"] == 0 and body["truncated"] is False


@pytest.mark.parametrize(
    ("params", "locator"),
    [
        ({"service": "svc"}, "anchor"),
        ({"anchor": ANCHOR}, "<root>"),
        ({"anchor": "2026-01-01T00:00:05", "service": "svc"}, "anchor"),
        ({"anchor": "not-a-date", "service": "svc"}, "anchor"),
        ({"anchor": ANCHOR, "trace_id": "XYZ-not-a-trace"}, "trace_id"),
        ({"anchor": ANCHOR, "service": "svc", "limit": "ten"}, "limit"),
        ({"anchor": ANCHOR, "service": "svc", "limit": "1.5"}, "limit"),
        (
            {"anchor": ANCHOR, "service": "svc", "window_before_seconds": "abc"},
            "window_before_seconds",
        ),
        (
            {"anchor": ANCHOR, "service": "svc", "window_after_seconds": "PT5M"},
            "window_after_seconds",
        ),
    ],
    ids=[
        "missing_anchor",
        "no_filter",
        "naive_anchor",
        "unparseable_anchor",
        "bad_trace",
        "limit_not_int",
        "limit_float",
        "before_not_int",
        "after_not_int",
    ],
)
def test_invalid_input_returns_invalid_query(
    client: TestClient, fake_conn: MagicMock, params: dict[str, str], locator: str
) -> None:
    detail = _assert_invalid(client.get("/evidence", params=params))
    assert [error["locator"] for error in detail["errors"]] == [locator]
    assert all(set(error) == {"locator", "message"} for error in detail["errors"])
    fake_conn.execute.assert_not_called()


@pytest.mark.parametrize(
    "params",
    [
        {"window_before_seconds": "-1"},
        {"window_after_seconds": "-5"},
        {"window_before_seconds": "86401"},
        {"limit": "1001"},
        {"limit": "0"},
    ],
    ids=["neg_before", "neg_after", "big_window", "limit_above_max", "limit_zero"],
)
def test_out_of_range_returns_invalid_query(
    client: TestClient, fake_conn: MagicMock, params: dict[str, str]
) -> None:
    _assert_invalid(client.get("/evidence", params={"anchor": ANCHOR, "service": "svc", **params}))
    fake_conn.execute.assert_not_called()


def test_huge_window_integer_returns_invalid_query(client: TestClient) -> None:
    params = {"anchor": ANCHOR, "service": "svc", "window_before_seconds": str(10**20)}
    _assert_invalid(client.get("/evidence", params=params))


def test_overflowing_anchor_returns_invalid_query(client: TestClient) -> None:
    params = {"anchor": "9999-12-31T23:59:00Z", "service": "svc", "window_after_seconds": "3600"}
    _assert_invalid(client.get("/evidence", params=params))


def test_error_body_does_not_echo_input(client: TestClient) -> None:
    bad_trace = "SECRETISH-fake-trace-value"
    bad_service = "fake-service-" + "x" * 300
    response = client.get(
        "/evidence", params={"anchor": ANCHOR, "trace_id": bad_trace, "service": bad_service}
    )
    _assert_invalid(response)
    assert bad_trace not in response.text
    assert "fake-service-" not in response.text
    bad_limit = "fake-limit-value"
    response = client.get(
        "/evidence", params={"anchor": ANCHOR, "service": "s", "limit": bad_limit}
    )
    _assert_invalid(response)
    assert bad_limit not in response.text


def test_query_canceled_returns_504(client: TestClient, fake_conn: MagicMock) -> None:
    fake_conn.execute.side_effect = [
        MagicMock(),
        MagicMock(),
        DBAPIError("SELECT", {}, psycopg.errors.QueryCanceled("fake statement timeout")),
    ]
    response = client.get("/evidence", params={"anchor": ANCHOR, "service": "svc"})
    assert response.status_code == 504
    assert response.json()["detail"]["error"] == "query_timeout"


def test_other_db_errors_propagate(client: TestClient, fake_conn: MagicMock) -> None:
    fake_conn.execute.side_effect = [
        MagicMock(),
        MagicMock(),
        OperationalError("SELECT", {}, Exception("fake connection lost")),
    ]
    response = client.get("/evidence", params={"anchor": ANCHOR, "service": "svc"})
    assert response.status_code == 500


def test_no_engine_returns_503() -> None:
    application = create_app(Settings(DATABASE_URL="", CRE_OTEL_EXPORTER="none"))
    with TestClient(application) as test_client:
        response = test_client.get("/evidence", params={"anchor": ANCHOR, "service": "svc"})
    assert response.status_code == 503
