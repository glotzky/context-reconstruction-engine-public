import logging
import re

import pytest
from fastapi.testclient import TestClient

from cre.request_id import REQUEST_ID_HEADER, normalize_request_id, request_id_var
from tests.conftest import LogCapture

HEX32 = re.compile(r"^[0-9a-f]{32}$")


def test_valid_request_id_is_echoed(client: TestClient) -> None:
    response = client.get("/health", headers={REQUEST_ID_HEADER: "demo-1.A_b"})
    assert response.headers[REQUEST_ID_HEADER] == "demo-1.A_b"


def test_missing_request_id_is_generated(client: TestClient) -> None:
    response = client.get("/health")
    assert HEX32.fullmatch(response.headers[REQUEST_ID_HEADER])


@pytest.mark.parametrize(
    "bad_value",
    ["bad id!", "a" * 129, "", "semi;colon", "new\tline"],
)
def test_invalid_request_id_is_replaced(client: TestClient, bad_value: str) -> None:
    response = client.get("/health", headers={REQUEST_ID_HEADER: bad_value})
    echoed = response.headers[REQUEST_ID_HEADER]
    assert echoed != bad_value
    assert HEX32.fullmatch(echoed)


def test_normalize_request_id_passthrough() -> None:
    assert normalize_request_id("abc-123") == "abc-123"
    assert normalize_request_id("a" * 128) == "a" * 128


def test_normalize_request_id_generates_for_none() -> None:
    generated = normalize_request_id(None)
    assert HEX32.fullmatch(generated)
    assert generated != normalize_request_id(None)


def test_request_id_var_reset_after_request(client: TestClient) -> None:
    client.get("/health", headers={REQUEST_ID_HEADER: "demo-2"})
    assert request_id_var.get() is None


def test_logs_during_request_carry_request_id(client: TestClient, log_capture: LogCapture) -> None:
    @client.app.get("/_log_probe")  # type: ignore[attr-defined]
    def probe() -> dict[str, str]:
        logging.getLogger("cre.test").info("probe_one")
        logging.getLogger("cre.test").info("probe_two")
        return {"ok": "yes"}

    response = client.get("/_log_probe", headers={REQUEST_ID_HEADER: "trace-me-42"})
    assert response.status_code == 200
    probes = [r for r in log_capture.records() if r["message"].startswith("probe_")]
    assert len(probes) == 2
    assert {r["request_id"] for r in probes} == {response.headers[REQUEST_ID_HEADER]}
