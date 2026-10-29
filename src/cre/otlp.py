"""Flatten an OTLP/JSON trace export into SDK-shaped span dicts accepted by ``normalize_span``.

Only the JSON encoding is supported (not protobuf). Assumptions, following the OTLP spec but not
verified against a live exporter: ``traceId``/``spanId`` are hex strings, ``*UnixNano`` times are
decimal strings or ints, and enums arrive as ints or ``SPAN_KIND_*`` / ``STATUS_CODE_*`` strings.

Structural problems with the whole payload raise ``OtlpError``. Problems inside a single span
become ``FlatSpan(error=...)`` and never raise.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import JsonValue

MAX_ANY_VALUE_DEPTH = 16
_SPAN_KINDS = ("UNSPECIFIED", "INTERNAL", "SERVER", "CLIENT", "PRODUCER", "CONSUMER")
_STATUS_CODES = ("UNSET", "OK", "ERROR")
_ANY_VALUE_KEYS = frozenset(
    {
        "stringValue",
        "boolValue",
        "intValue",
        "doubleValue",
        "bytesValue",
        "arrayValue",
        "kvlistValue",
    }
)
_DIGITS = re.compile(r"^[0-9]+$")
_INT = re.compile(r"^-?[0-9]+$")
_MAPPED_SPAN_KEYS = frozenset(
    {
        "traceId",
        "spanId",
        "parentSpanId",
        "name",
        "kind",
        "startTimeUnixNano",
        "endTimeUnixNano",
        "status",
        "attributes",
        "events",
        "links",
        "flags",
    }
)


class OtlpError(ValueError):
    """Structural problem with an OTLP/JSON payload; the message includes the JSON path."""


@dataclass(frozen=True)
class FlatSpan:
    locator: str
    span: dict[str, Any] | None
    error: str | None


def decode_any_value(value: Any, *, path: str, depth: int = 0) -> JsonValue:
    if depth > MAX_ANY_VALUE_DEPTH:
        raise OtlpError(f"{path}: AnyValue nesting exceeds {MAX_ANY_VALUE_DEPTH} levels")
    if not isinstance(value, Mapping):
        raise OtlpError(f"{path}: AnyValue must be an object")
    if not value:
        return None
    if len(value) > 1:
        raise OtlpError(f"{path}: AnyValue must have exactly one value key")
    key, inner = next(iter(value.items()))
    if key == "stringValue" and isinstance(inner, str):
        return inner
    if key == "boolValue" and isinstance(inner, bool):
        return inner
    if key == "intValue":
        if isinstance(inner, int) and not isinstance(inner, bool):
            return inner
        if isinstance(inner, str) and _INT.match(inner):
            return int(inner)
        raise OtlpError(f"{path}.intValue: must be an integer")
    if key == "doubleValue" and isinstance(inner, int | float) and not isinstance(inner, bool):
        return float(inner)
    if key == "bytesValue" and isinstance(inner, str):
        return inner
    if key == "arrayValue" and isinstance(inner, Mapping):
        values = _optional_list(inner, "values", f"{path}.arrayValue")
        return [
            decode_any_value(item, path=f"{path}.arrayValue.values[{i}]", depth=depth + 1)
            for i, item in enumerate(values)
        ]
    if key == "kvlistValue" and isinstance(inner, Mapping):
        values = _optional_list(inner, "values", f"{path}.kvlistValue")
        return _decode_key_values(values, path=f"{path}.kvlistValue.values", depth=depth + 1)
    if key in _ANY_VALUE_KEYS:
        raise OtlpError(f"{path}.{key}: invalid value type")
    raise OtlpError(f"{path}: unknown AnyValue key")


def decode_attributes(items: Any, *, path: str) -> dict[str, JsonValue]:
    if items is None:
        return {}
    if not isinstance(items, list):
        raise OtlpError(f"{path}: must be a list")
    return _decode_key_values(items, path=path, depth=0)


def _decode_key_values(items: list[Any], *, path: str, depth: int) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for i, item in enumerate(items):
        item_path = f"{path}[{i}]"
        if not isinstance(item, Mapping) or not isinstance(item.get("key"), str):
            raise OtlpError(f"{item_path}: must be an object with a string key")
        result[item["key"]] = decode_any_value(
            item.get("value", {}), path=f"{item_path}.value", depth=depth
        )
    return result


def _optional_list(container: Mapping[str, Any], key: str, path: str) -> list[Any]:
    value = container.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise OtlpError(f"{path}.{key}: must be a list")
    return value


def _nanos_to_iso(value: Any, *, path: str) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        nanos = value
    elif isinstance(value, str) and _DIGITS.match(value):
        nanos = int(value)
    else:
        raise OtlpError(f"{path}: must be a decimal integer")
    seconds, remainder = divmod(nanos, 1_000_000_000)
    try:
        moment = datetime.fromtimestamp(seconds, UTC).replace(microsecond=remainder // 1000)
    except (OverflowError, OSError, ValueError) as exc:
        raise OtlpError(f"{path}: out of range") from exc
    return moment.isoformat(timespec="microseconds")


def _enum(value: Any, names: tuple[str, ...], prefix: str, *, path: str) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < len(names):
        return names[value]
    if isinstance(value, str) and value.startswith(prefix) and value[len(prefix) :] in names:
        return value[len(prefix) :]
    raise OtlpError(f"{path}: unknown value")


def _present(value: Any) -> bool:
    return value is not None and value != ""


def _map_span(
    raw: Any, resource_attributes: dict[str, JsonValue], scope: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise OtlpError("span must be an object")
    context: dict[str, Any] = {}
    if _present(raw.get("traceId")):
        context["trace_id"] = raw["traceId"]
    if _present(raw.get("spanId")):
        context["span_id"] = raw["spanId"]
    span: dict[str, Any] = {
        "context": context,
        "parent_id": raw["parentSpanId"] if _present(raw.get("parentSpanId")) else None,
    }
    if _present(raw.get("name")):
        span["name"] = raw["name"]
    if raw.get("kind") is not None:
        span["kind"] = _enum(raw["kind"], _SPAN_KINDS, "SPAN_KIND_", path="kind")
    for source_key, target_key in (
        ("startTimeUnixNano", "start_time"),
        ("endTimeUnixNano", "end_time"),
    ):
        if _present(raw.get(source_key)):
            span[target_key] = _nanos_to_iso(raw[source_key], path=source_key)
    status = raw.get("status")
    if status is not None:
        if not isinstance(status, Mapping):
            raise OtlpError("status: must be an object")
        mapped_status: dict[str, Any] = {}
        if status.get("code") is not None:
            mapped_status["status_code"] = _enum(
                status["code"], _STATUS_CODES, "STATUS_CODE_", path="status.code"
            )
        if status.get("message") is not None:
            mapped_status["description"] = status["message"]
        span["status"] = mapped_status
    span["attributes"] = decode_attributes(raw.get("attributes"), path="attributes")
    events = []
    for i, event in enumerate(_optional_list(raw, "events", "span")):
        if not isinstance(event, Mapping):
            raise OtlpError(f"events[{i}]: must be an object")
        time = event.get("timeUnixNano")
        events.append(
            {
                "name": event.get("name"),
                "timestamp": (
                    _nanos_to_iso(time, path=f"events[{i}].timeUnixNano")
                    if _present(time)
                    else None
                ),
                "attributes": decode_attributes(
                    event.get("attributes"), path=f"events[{i}].attributes"
                ),
            }
        )
    span["events"] = events
    links = []
    for i, link in enumerate(_optional_list(raw, "links", "span")):
        if not isinstance(link, Mapping):
            raise OtlpError(f"links[{i}]: must be an object")
        links.append(
            {
                "trace_id": link.get("traceId"),
                "span_id": link.get("spanId"),
                "attributes": decode_attributes(
                    link.get("attributes"), path=f"links[{i}].attributes"
                ),
            }
        )
    span["links"] = links
    span["resource"] = {"attributes": dict(resource_attributes)}
    span["instrumentation_scope"] = dict(scope)
    for key, value in raw.items():
        if key in _MAPPED_SPAN_KEYS or (key.startswith("dropped") and key.endswith("Count")):
            continue
        span.setdefault(key, value)
    return span


def flatten_otlp_spans(payload: Mapping[str, Any]) -> list[FlatSpan]:
    resource_spans = payload.get("resourceSpans")
    if not isinstance(resource_spans, list):
        raise OtlpError("resourceSpans: required list")
    flat: list[FlatSpan] = []
    for r_index, resource_span in enumerate(resource_spans):
        r_path = f"resourceSpans[{r_index}]"
        if not isinstance(resource_span, Mapping):
            raise OtlpError(f"{r_path}: must be an object")
        resource = resource_span.get("resource") or {}
        if not isinstance(resource, Mapping):
            raise OtlpError(f"{r_path}.resource: must be an object")
        resource_attributes = decode_attributes(
            resource.get("attributes"), path=f"{r_path}.resource.attributes"
        )
        for s_index, scope_span in enumerate(_optional_list(resource_span, "scopeSpans", r_path)):
            s_path = f"{r_path}.scopeSpans[{s_index}]"
            if not isinstance(scope_span, Mapping):
                raise OtlpError(f"{s_path}: must be an object")
            scope = scope_span.get("scope") or {}
            if not isinstance(scope, Mapping):
                raise OtlpError(f"{s_path}.scope: must be an object")
            scope_info = {"name": scope.get("name"), "version": scope.get("version")}
            for p_index, raw in enumerate(_optional_list(scope_span, "spans", s_path)):
                locator = f"{s_path}.spans[{p_index}]"
                try:
                    span = _map_span(raw, resource_attributes, scope_info)
                except OtlpError as exc:
                    flat.append(FlatSpan(locator=locator, span=None, error=str(exc)))
                else:
                    flat.append(FlatSpan(locator=locator, span=span, error=None))
    return flat
