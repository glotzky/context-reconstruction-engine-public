"""Human-run only: seeds fake slice 1 evidence in Compose PostgreSQL and retrieves it."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from cre.api import create_app
from cre.config import Settings
from cre.evidence import EVIDENCE_ADAPTER, Evidence
from cre.evidence_repo import store_evidence
from cre.retrieval import EvidenceQuery, retrieve_evidence
from tests.evidence_samples import RETRIEVED_AT, SPAN_ID, TRACE_ID

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL is not set"),
]

ROOT = Path(__file__).resolve().parents[2]
T = datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC)
SERVICE = "cre-lab-api"


def _item(kind: str, at: datetime, name: str, observed: dict[str, Any], **extra: Any) -> Evidence:
    return EVIDENCE_ADAPTER.validate_python(
        {
            "kind": kind,
            "timestamp": at,
            "service": SERVICE,
            "provenance": {
                "source": {
                    "source_type": "fake",
                    "name": "fake slice 1 seed",
                    "uri": "file:///fake-seed.json",
                    "locator": name,
                },
                "retrieval_method": "fake_seed",
                "identifiers": {"seed": name},
                "retrieved_at": RETRIEVED_AT,
            },
            "observed": observed,
            **extra,
        }
    )


DEPLOYMENT = _item(
    "deployment",
    T - timedelta(seconds=120),
    "deployment",
    {"version": "0.1.0", "status": "succeeded", "commit": "deadbeef"},
)
CONFIG_CHANGE = _item(
    "config_change",
    T - timedelta(seconds=60),
    "config_change",
    {
        "key": "alembic_revision",
        "old_value": "0001_baseline",
        "new_value": "0002_rename_customer_name",
    },
)
ERROR_LOG = _item(
    "log",
    T,
    "error_log",
    {"level": "ERROR", "message": "order_lookup_failed"},
    trace_id=TRACE_ID,
    span_id=SPAN_ID,
)
SPAN = _item(
    "span",
    T,
    "span",
    {"name": "GET /orders/{order_id}", "start_time": T, "end_time": T + timedelta(seconds=1)},
    trace_id=TRACE_ID,
    span_id=SPAN_ID,
)
OTHER_SERVICE_LOG = _item("log", T, "other_service", {"level": "INFO", "message": "m"}).model_copy(
    update={"service": "fake-other-service"}
)
LATE_LOG = _item("log", T + timedelta(hours=2), "late", {"level": "INFO", "message": "late"})


def _ordered(*items: Evidence) -> list[Evidence]:
    return sorted(items, key=lambda item: (item.timestamp, str(item.id)))


def test_retrieves_seeded_slice1_evidence() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    engine = create_engine(os.environ["DATABASE_URL"])
    settings = Settings()
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "0004_evidence_idempotency_key")
        with engine.begin() as conn:
            for item in (DEPLOYMENT, CONFIG_CHANGE, ERROR_LOG, SPAN, OTHER_SERVICE_LOG, LATE_LOG):
                assert store_evidence(conn, item) is not None

        window = timedelta(seconds=300)
        service_query = EvidenceQuery(
            service=SERVICE, anchor=T, window_before=window, window_after=window
        )
        expected = _ordered(DEPLOYMENT, CONFIG_CHANGE, ERROR_LOG, SPAN)
        with engine.connect() as conn:
            result = retrieve_evidence(conn, service_query, settings)
        assert [item.id for item in result.items] == [item.id for item in expected]
        assert [item.provenance for item in result.items] == [i.provenance for i in expected]
        assert result.items == expected
        assert result.start == T - window and result.end == T + window
        assert result.count == 4 and result.truncated is False

        with engine.connect() as conn:
            traced = retrieve_evidence(conn, EvidenceQuery(trace_id=TRACE_ID, anchor=T), settings)
        assert [item.id for item in traced.items] == [i.id for i in _ordered(ERROR_LOG, SPAN)]

        limited_query = service_query.model_copy(update={"limit": 2})
        with engine.connect() as conn:
            limited = retrieve_evidence(conn, limited_query, settings)
        assert limited.count == 2 and limited.truncated is True
        assert [item.id for item in limited.items] == [item.id for item in expected[:2]]

        with TestClient(create_app(Settings())) as client:
            response = client.get(
                "/evidence",
                params={
                    "service": SERVICE,
                    "anchor": "2026-01-01T00:00:05Z",
                    "window_before_seconds": "300",
                    "window_after_seconds": "300",
                },
            )
        assert response.status_code == 200
        assert [item["id"] for item in response.json()["items"]] == [
            str(item.id) for item in expected
        ]
    finally:
        engine.dispose()
        command.downgrade(config, "base")
