"""OpenTelemetry tracing setup."""

import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from cre.config import Settings


def format_span_line(span: ReadableSpan) -> str:
    line: str = span.to_json(indent=None)  # type: ignore[arg-type]
    return line + os.linesep


def configure_tracing(settings: Settings) -> TracerProvider:
    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": settings.version,
            "deployment.commit": settings.commit,
        }
    )
    provider = TracerProvider(resource=resource)
    if settings.otel_exporter == "console":
        provider.add_span_processor(
            BatchSpanProcessor(ConsoleSpanExporter(formatter=format_span_line))
        )
    elif settings.otel_exporter == "otlp":
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(provider)
    return provider


def current_trace_ids() -> tuple[str | None, str | None]:
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None, None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
