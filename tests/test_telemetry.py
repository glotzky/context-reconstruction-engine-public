from opentelemetry.sdk.trace import TracerProvider

from cre.config import Settings
from cre.telemetry import configure_tracing, current_trace_ids


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
