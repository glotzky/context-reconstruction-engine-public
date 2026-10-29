"""Human-run only: lab failure -> shipped logs and spans -> investigation -> report."""

import io
import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import Engine, text

import cre.api
from cre import lab
from cre.api import create_app
from cre.collectors import collect_stdout_lines
from cre.config import Settings
from cre.db import create_db_engine
from cre.ingest import store_items
from cre.investigation_worker import process_investigation
from cre.llm import FakeProvider, LLMRequest
from cre.logging_setup import JsonFormatter
from cre.prompts import HYPOTHESIS_SYSTEM_PROMPT
from cre.telemetry import configure_tracing, format_span_line

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL is not set"),
]

ROOT = Path(__file__).resolve().parents[2]
CONCLUDE = '{"action": "conclude", "tool_calls": [], "reason": "fake done"}'
FAILURES = ["schema-rename", "stale-search-path"]
LAB_KIND = {"schema-rename": "deployment", "stale-search-path": "config_change"}
HYPOTHESIS_TITLE = "Lab change precedes the missing customer_name column error"


def _config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return config


def _reset_on_fresh_engine() -> None:
    engine = create_db_engine(Settings().database_url)
    try:
        with engine.begin() as conn:
            lab.reset(conn)
    finally:
        engine.dispose()


def _apply(engine: Engine, name: str) -> None:
    with engine.begin() as conn:
        assert lab.apply(conn, name) is True


@pytest.fixture
def span_exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()

    def _configure(settings: Settings) -> TracerProvider:
        provider = configure_tracing(settings)
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        return provider

    monkeypatch.setattr(cre.api, "configure_tracing", _configure)
    return exporter


@contextmanager
def _capture_logs() -> Iterator[io.StringIO]:
    settings = Settings()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(settings.service_name, settings.version, settings.commit))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield stream
    finally:
        root.removeHandler(handler)


def _lab_event_id(engine: Engine, name: str) -> UUID:
    with engine.connect() as conn:
        found: UUID = conn.execute(
            text(
                "SELECT id FROM evidence_items WHERE kind = :kind "
                "AND source->>'name' = 'cre.lab' AND retrieval_method = 'lab_trigger'"
            ),
            {"kind": LAB_KIND[name]},
        ).scalar_one()
    return found


def _error_log(engine: Engine, request_id: str) -> tuple[UUID, str]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT id, trace_id FROM evidence_items WHERE kind = 'log' "
                "AND request_id = :request_id AND observed->>'level' = 'ERROR'"
            ),
            {"request_id": request_id},
        ).one()
    return row.id, row.trace_id


def _hypothesis(lab_event_id: UUID, error_log_id: UUID) -> str:
    ids = [str(lab_event_id), str(error_log_id)]
    return json.dumps(
        {
            "hypotheses": [
                {
                    "rank": 1,
                    "title": HYPOTHESIS_TITLE,
                    "explanation": "The lab change was applied shortly before the error log.",
                    "claims": [
                        {
                            "text": "A lab change was applied before the failing request.",
                            "evidence_ids": ids,
                            "status": "verified",
                        }
                    ],
                    "supporting_evidence_ids": ids,
                    "contradicting_evidence_ids": [],
                    "uncertainty": "medium",
                    "uncertainty_notes": "Timing is a lead; the schema state was not inspected.",
                    "follow_up_queries": ["inspect the orders table columns"],
                }
            ],
            "unknowns": [],
        }
    )


@pytest.mark.parametrize("name", FAILURES)
def test_trigger_to_report(name: str, span_exporter: InMemorySpanExporter) -> None:
    config = _config()
    settings = Settings()
    engine = create_db_engine(settings.database_url)
    request_id = f"e2e-test-{name}"
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        with TestClient(create_app(settings), raise_server_exceptions=False) as client:
            _apply(engine, name)
            if name == "stale-search-path":
                client.app.state.engine.dispose()  # type: ignore[attr-defined]
            with _capture_logs() as captured_logs:
                response = client.get("/orders/1", headers={"X-Request-ID": request_id})
            assert response.status_code == 500

            lines = captured_logs.getvalue().splitlines()
            lines += [format_span_line(span) for span in span_exporter.get_finished_spans()]
            collected = collect_stdout_lines(
                lines, source_name="api", retrieved_at=datetime.now(UTC)
            )
            with engine.begin() as conn:
                summary = store_items(
                    conn, [item.evidence for item in collected if item.evidence is not None]
                )
            assert summary.accepted > 0

            error_log_id, trace_id = _error_log(engine, request_id)
            lab_event_id = _lab_event_id(engine, name)
            submitted = client.post(
                "/investigations",
                json={
                    "timestamp": datetime.now(UTC).isoformat(),
                    "trace_id": trace_id,
                    "service": settings.service_name,
                    "request_id": request_id,
                    "symptom": f"GET /orders/1 returned 500 ({name})",
                },
            )
            assert submitted.status_code == 202
            job_id = UUID(submitted.json()["id"])

            def respond(request: LLMRequest) -> str:
                if request.system == HYPOTHESIS_SYSTEM_PROMPT:
                    return _hypothesis(lab_event_id, error_log_id)
                return CONCLUDE

            provider = FakeProvider(respond)
            assert process_investigation(engine, settings, provider, "e2e-worker") == job_id
            with engine.connect() as conn:
                status = conn.execute(
                    text("SELECT status FROM investigation_jobs WHERE id = :id"), {"id": job_id}
                ).scalar_one()
            assert status == "completed"

            report = client.get(f"/investigations/{job_id}/report")
            assert report.status_code == 200
            html = report.text
            # The report page does not render the trace ID itself; its linked evidence page does.
            error_page = client.get(f"/investigations/{job_id}/evidence/{error_log_id}")
            assert error_page.status_code == 200
        assert trace_id in error_page.text
        assert f"/evidence/{lab_event_id}" in html
        assert f"/evidence/{error_log_id}" in html
        assert HYPOTHESIS_TITLE in html
        if name == "stale-search-path":
            assert "Lead (timing only), not causation" in html
        else:
            # The deployment event shares deployment_version with the logs, so the timeline
            # links it explicitly (as a correlation) instead of as a timing-only lead.
            assert "shared deployment_version" in html
        assert "not a determination of cause" in html
        assert "caused by" not in html.lower()
    finally:
        engine.dispose()
        _reset_on_fresh_engine()
        command.downgrade(config, "base")


@pytest.mark.parametrize("name", FAILURES)
def test_reset_records_event(name: str) -> None:
    config = _config()
    engine = create_db_engine(Settings().database_url)
    try:
        command.downgrade(config, "base")
        command.upgrade(config, "head")
        _apply(engine, name)
        with engine.begin() as conn:
            assert lab.reset(conn) == [name]
        with engine.connect() as conn:
            rows: list[Any] = conn.execute(
                text(
                    "SELECT retrieval_method FROM evidence_items "
                    "WHERE kind = :kind AND source->>'name' = 'cre.lab' ORDER BY occurred_at"
                ),
                {"kind": LAB_KIND[name]},
            ).all()
        assert [row.retrieval_method for row in rows] == ["lab_trigger", "lab_reset"]
    finally:
        engine.dispose()
        _reset_on_fresh_engine()
        command.downgrade(config, "base")
