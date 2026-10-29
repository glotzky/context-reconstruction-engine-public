import re
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

import cre.api
from cre.api import create_app
from cre.config import Settings
from cre.orders import ORDER_QUERY, Order, fetch_order
from tests.conftest import LogCapture

SAMPLE = {"id": 1, "customer_name": "Test Customer One", "status": "paid", "total_cents": 1999}


class FakeUndefinedColumn(Exception):
    pass


def test_get_order_returns_order(client: TestClient, fake_conn: MagicMock) -> None:
    fake_conn.execute.return_value.mappings.return_value.first.return_value = SAMPLE
    response = client.get("/orders/1")
    assert response.status_code == 200
    assert response.json() == SAMPLE


def test_unknown_order_returns_404(client: TestClient, fake_conn: MagicMock) -> None:
    fake_conn.execute.return_value.mappings.return_value.first.return_value = None
    response = client.get("/orders/999")
    assert response.status_code == 404
    assert response.json() == {"detail": "order not found"}


@pytest.mark.parametrize("bad_id", ["abc", "0", "-1"])
def test_invalid_order_id_returns_422(client: TestClient, bad_id: str) -> None:
    assert client.get(f"/orders/{bad_id}").status_code == 422


def test_db_error_returns_500_and_logs(
    client: TestClient, log_capture: LogCapture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(conn: Any, order_id: int) -> Order | None:
        raise ProgrammingError(
            "SELECT id, customer_name FROM orders",
            {},
            FakeUndefinedColumn('column "customer_name" does not exist'),
        )

    monkeypatch.setattr(cre.api, "fetch_order", _raise)
    response = client.get("/orders/1", headers={"X-Request-ID": "demo-1"})

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "demo-1"
    body = response.json()
    assert body == {
        "error": "database_error",
        "error_type": "FakeUndefinedColumn",
        "request_id": "demo-1",
    }
    assert "SELECT" not in response.text
    assert "fake_password" not in response.text

    failures = [r for r in log_capture.records() if r["message"] == "order_lookup_failed"]
    assert len(failures) == 1
    line = failures[0]
    assert line["level"] == "ERROR"
    assert line["request_id"] == "demo-1"
    assert re.fullmatch(r"[0-9a-f]{32}", line["trace_id"])
    assert "customer_name" in line["sql_error"]
    assert line["order_id"] == 1
    assert line["error_type"] == "ProgrammingError"
    assert line["event"] == "order_lookup_failed"


def test_no_database_returns_503() -> None:
    app = create_app(Settings(DATABASE_URL=""))
    with TestClient(app) as client:
        response = client.get("/orders/1")
    assert response.status_code == 503
    assert response.json() == {"detail": "database not configured"}


def test_fetch_order_maps_row() -> None:
    conn = MagicMock()
    conn.execute.return_value.mappings.return_value.first.return_value = SAMPLE
    order = fetch_order(conn, 1)
    conn.execute.assert_called_once_with(ORDER_QUERY, {"order_id": 1})
    assert order == Order(**SAMPLE)


def test_fetch_order_returns_none_when_missing() -> None:
    conn = MagicMock()
    conn.execute.return_value.mappings.return_value.first.return_value = None
    assert fetch_order(conn, 42) is None


def test_order_query_still_references_customer_name() -> None:
    assert "customer_name" in str(ORDER_QUERY)
