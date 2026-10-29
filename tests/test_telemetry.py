import os
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import cre.telemetry
from cre.config import Settings
from cre.evidence import SourceRef
from cre.normalizers import normalize_span
from cre.telemetry import configure_tracing, current_trace_ids, format_span_line


def test_configure_tracing_sets_resource_attributes() -> None:
    settings = Settings(
        CRE_SERVICE_NAME="cre-test", CRE_VERSION="9.9.9-test", CRE_COMMIT="deadbeef"
    )
    provider = configure_tracing(settings)
    assert isinstance(provider, TracerProvider)
    attrs = provider.resource.attributes
    assert attrs["service.name"] == "cre-test"
    assert attrs["service.version"] == "9.9.9-test"
    assert attrs["deployment.commit"] == "deadbeef"


def test_none_exporter_adds_no_span_processor() -> None:
    provider = configure_tracing(Settings(CRE_OTEL_EXPORTER="none"))
    assert provider._active_span_processor._span_processors == ()


def test_console_exporter_adds_span_processor() -> None:
    provider = configure_tracing(Settings(CRE_OTEL_EXPORTER="console"))
    try:
        assert len(provider._active_span_processor._span_processors) == 1
    finally:
        provider.shutdown()


def test_current_trace_ids_without_span() -> None:
    assert current_trace_ids() == (None, None)


def test_current_trace_ids_inside_span() -> None:
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("s") as span:
        trace_id, span_id = current_trace_ids()
        ctx = span.get_span_context()
    assert trace_id == format(ctx.trace_id, "032x")
    assert span_id == format(ctx.span_id, "016x")


def test_format_span_line_is_single_line_json_normalizable() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("test").start_as_current_span("GET /orders/{order_id}"):
        pass
    [span] = exporter.get_finished_spans()
    line = format_span_line(span)
    assert line.endswith(os.linesep)
    assert "\n" not in line.rstrip(os.linesep)
    item = normalize_span(
        line.strip(),
        source=SourceRef(source_type="otel_span", name="api"),
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert item.trace_id == format(span.context.trace_id, "032x")
    assert item.observed.name == "GET /orders/{order_id}"


def test_console_exporter_uses_single_line_formatter(monkeypatch: pytest.MonkeyPatch) -> None:
    exporter_cls = MagicMock(name="ConsoleSpanExporter")
    monkeypatch.setattr(cre.telemetry, "ConsoleSpanExporter", exporter_cls)
    provider = configure_tracing(Settings(CRE_OTEL_EXPORTER="console"))
    try:
        exporter_cls.assert_called_once_with(formatter=format_span_line)
    finally:
        provider.shutdown()
