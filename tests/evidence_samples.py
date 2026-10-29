"""Deterministic fake sample inputs for evidence tests (no real data, no test functions)."""

from datetime import UTC, datetime
from typing import Any

from cre.evidence import SourceRef

RETRIEVED_AT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
SPAN_ID = "b7ad6b7169203331"
PARENT_SPAN_ID = "00f067aa0ba902b7"
FAKE_DB_PASSWORD = "lab-password"
FAKE_DSN = f"postgresql+psycopg://lab:{FAKE_DB_PASSWORD}@db:5432/lab"

LOG_SOURCE = SourceRef(
    source_type="structured_log", name="cre-lab-api stdout", uri=None, locator="line:42"
)
SPAN_SOURCE = SourceRef(source_type="otel_span", name="cre-lab-api console exporter")
DEPLOYMENT_SOURCE = SourceRef(source_type="deployment_event", name="fake deploy history")
COMMIT_SOURCE = SourceRef(source_type="commit_record", name="fake commit history")

SAMPLE_LOG: dict[str, Any] = {
    "timestamp": "2026-01-01T00:00:05.123Z",
    "level": "ERROR",
    "logger": "cre.api",
    "message": "order_lookup_failed",
    "service": "cre-lab-api",
    "version": "0.1.0",
    "commit": "deadbeef",
    "request_id": "demo-1",
    "trace_id": TRACE_ID,
    "span_id": SPAN_ID,
    "order_id": 1,
    "error_type": "ProgrammingError",
    "sql_error": (
        '(psycopg.errors.UndefinedColumn) column "customer_name" does not exist '
        f"(connection: {FAKE_DSN})"
    ),
    "exc_info": "Traceback (most recent call last):\n  ...\nUndefinedColumn",
}

SAMPLE_SPAN: dict[str, Any] = {
    "name": "GET /orders/{order_id}",
    "context": {
        "trace_id": "0x" + TRACE_ID.upper(),
        "span_id": "0x" + SPAN_ID,
        "trace_state": "[]",
    },
    "kind": "SpanKind.SERVER",
    "parent_id": "0x" + PARENT_SPAN_ID,
    "start_time": "2026-01-01T00:00:05.100000Z",
    "end_time": "2026-01-01T00:00:05.150000Z",
    "status": {"status_code": "ERROR", "description": "UndefinedColumn"},
    "attributes": {
        "http.method": "GET",
        "http.route": "/orders/{order_id}",
        "http.status_code": 500,
        "request_id": "demo-1",
    },
    "events": [
        {
            "name": "exception",
            "timestamp": "2026-01-01T00:00:05.140000Z",
            "attributes": {"exception.type": "UndefinedColumn"},
        }
    ],
    "links": [],
    "resource": {
        "attributes": {
            "service.name": "cre-lab-api",
            "service.version": "0.1.0",
            "deployment.commit": "deadbeef",
        },
        "schema_url": "",
    },
}

SAMPLE_DEPLOYMENT: dict[str, Any] = {
    "deployed_at": "2026-01-01T00:00:00Z",
    "service": "cre-lab-api",
    "version": "0.1.0",
    "status": "succeeded",
    "commit": "deadbeef",
    "environment": "lab",
    "actor": "fake-deployer",
    "deployment_id": "deploy-0001",
    "pipeline_run": "fake-run-7",
}

SAMPLE_COMMIT: dict[str, Any] = {
    "sha": "DEADBEEF00112233445566778899AABBCCDDEEFF",
    "committed_at": "2025-12-31T23:50:00Z",
    "author": "Fake Developer",
    "message": "Rename customer_name column",
    "files": ["migrations/versions/0002_rename_customer_name.py"],
    "service": "cre-lab-api",
    "version": "0.1.0",
    "reviewer": "fake-reviewer",
}

OTLP_CHILD_SPAN_ID = "1a2b3c4d5e6f7081"

SAMPLE_OTLP: dict[str, Any] = {
    "resourceSpans": [
        {
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "cre-lab-api"}},
                    {"key": "service.version", "value": {"stringValue": "0.1.0"}},
                ]
            },
            "scopeSpans": [
                {
                    "scope": {"name": "opentelemetry.instrumentation.fastapi", "version": "0.50b0"},
                    "spans": [
                        {
                            "traceId": TRACE_ID,
                            "spanId": SPAN_ID,
                            "parentSpanId": "",
                            "name": "GET /orders/{order_id}",
                            "kind": 2,
                            "startTimeUnixNano": "1767225605100000000",
                            "endTimeUnixNano": "1767225605150000999",
                            "status": {"code": 2, "message": "UndefinedColumn"},
                            "attributes": [
                                {"key": "http.method", "value": {"stringValue": "GET"}},
                                {"key": "http.status_code", "value": {"intValue": "500"}},
                                {"key": "db.password", "value": {"stringValue": FAKE_DB_PASSWORD}},
                            ],
                            "events": [
                                {
                                    "name": "exception",
                                    "timeUnixNano": "1767225605140000000",
                                    "attributes": [
                                        {
                                            "key": "exception.type",
                                            "value": {"stringValue": "UndefinedColumn"},
                                        }
                                    ],
                                }
                            ],
                            "droppedAttributesCount": 0,
                        },
                        {
                            "traceId": TRACE_ID,
                            "spanId": OTLP_CHILD_SPAN_ID,
                            "parentSpanId": SPAN_ID,
                            "name": "SELECT orders",
                            "kind": "SPAN_KIND_CLIENT",
                            "startTimeUnixNano": 1767225605110000000,
                            "endTimeUnixNano": 1767225605120000000,
                            "status": {"code": "STATUS_CODE_ERROR"},
                            "attributes": [
                                {"key": "db.system", "value": {"stringValue": "postgresql"}}
                            ],
                        },
                    ],
                }
            ],
        }
    ]
}

GIT_LOG_OUTPUT = (
    "\x1eaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\x1f2026-01-01T00:00:00+00:00"
    "\x1fFake Developer\x1fRename customer_name column\n\n"
    "migrations/versions/0002_rename_customer_name.py\nsrc/cre/orders.py\n"
    "\x1ebbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\x1f2025-12-31T12:00:00-05:00"
    "\x1fOther Fake Dev\x1fAdd orders endpoint\n\nsrc/cre/api.py\n"
)
