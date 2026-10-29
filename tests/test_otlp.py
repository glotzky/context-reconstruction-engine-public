import copy
from datetime import UTC, datetime
from typing import Any

import pytest

from cre.evidence import SourceRef
from cre.normalizers import normalize_span
from cre.otlp import OtlpError, decode_any_value, decode_attributes, flatten_otlp_spans
from tests.evidence_samples import (
    FAKE_DB_PASSWORD,
    OTLP_CHILD_SPAN_ID,
    RETRIEVED_AT,
    SAMPLE_OTLP,
    SPAN_ID,
    TRACE_ID,
)

SOURCE = SourceRef(source_type="otel_span", name="fake otlp")


def _raw_span(**overrides: Any) -> dict[str, Any]:
    span = copy.deepcopy(SAMPLE_OTLP["resourceSpans"][0]["scopeSpans"][0]["spans"][0])
    span.update(overrides)
    return span


def _payload(*spans: dict[str, Any]) -> dict[str, Any]:
    return {"resourceSpans": [{"scopeSpans": [{"spans": list(spans)}]}]}


def _single(span: dict[str, Any]) -> Any:
    [flat] = flatten_otlp_spans(_payload(span))
    return flat


def test_happy_path_maps_to_sdk_shape_accepted_by_normalize_span() -> None:
    flat = flatten_otlp_spans(SAMPLE_OTLP)
    assert [f.locator for f in flat] == [
        "resourceSpans[0].scopeSpans[0].spans[0]",
        "resourceSpans[0].scopeSpans[0].spans[1]",
    ]
    assert all(f.error is None for f in flat)
    parent, child = (f.span for f in flat)
    assert parent is not None and child is not None
    assert parent["context"] == {"trace_id": TRACE_ID, "span_id": SPAN_ID}
    assert parent["resource"] == {
        "attributes": {"service.name": "cre-lab-api", "service.version": "0.1.0"}
    }
    assert parent["instrumentation_scope"] == {
        "name": "opentelemetry.instrumentation.fastapi",
        "version": "0.50b0",
    }

    item = normalize_span(parent, source=SOURCE, retrieved_at=RETRIEVED_AT)
    assert item.observed.span_kind == "SERVER"
    assert item.observed.status_code == "ERROR"
    assert item.observed.status_message == "UndefinedColumn"
    assert item.observed.parent_span_id is None
    assert item.observed.start_time == datetime(2026, 1, 1, 0, 0, 5, 100000, tzinfo=UTC)
    assert item.observed.end_time == datetime(2026, 1, 1, 0, 0, 5, 150000, tzinfo=UTC)
    assert item.service == "cre-lab-api"
    assert item.deployment_version == "0.1.0"
    assert item.observed.attributes["http.status_code"] == 500
    assert FAKE_DB_PASSWORD not in item.model_dump_json()

    child_item = normalize_span(child, source=SOURCE, retrieved_at=RETRIEVED_AT)
    assert child_item.span_id == OTLP_CHILD_SPAN_ID
    assert child_item.observed.parent_span_id == SPAN_ID
    assert child_item.observed.span_kind == "CLIENT"
    assert child_item.observed.status_code == "ERROR"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"stringValue": "x"}, "x"),
        ({"boolValue": True}, True),
        ({"intValue": "42"}, 42),
        ({"intValue": -7}, -7),
        ({"doubleValue": 1.5}, 1.5),
        ({"doubleValue": 2}, 2.0),
        ({"arrayValue": {"values": [{"stringValue": "a"}, {"intValue": "1"}]}}, ["a", 1]),
        ({"arrayValue": {}}, []),
        (
            {"kvlistValue": {"values": [{"key": "k", "value": {"boolValue": False}}]}},
            {"k": False},
        ),
        ({"bytesValue": "AAEC"}, "AAEC"),
        ({}, None),
    ],
)
def test_decode_any_value_variants(value: dict[str, Any], expected: Any) -> None:
    assert decode_any_value(value, path="v") == expected


def _nested(depth: int) -> dict[str, Any]:
    value: dict[str, Any] = {"stringValue": "leaf"}
    for _ in range(depth):
        value = {"arrayValue": {"values": [value]}}
    return value


@pytest.mark.parametrize(
    "value",
    [
        {"stringValue": "a", "intValue": "1"},
        {"mysteryValue": "a"},
        {"intValue": "12x"},
        {"intValue": 1.5},
        {"stringValue": 3},
        "not an object",
        _nested(17),
    ],
    ids=["multiple_keys", "unknown_key", "bad_int", "float_int", "wrong_type", "scalar", "deep"],
)
def test_decode_any_value_errors(value: Any) -> None:
    with pytest.raises(OtlpError, match=r"^v"):
        decode_any_value(value, path="v")


def test_decode_any_value_allows_depth_16() -> None:
    assert decode_any_value(_nested(16), path="v") is not None


def test_decode_attributes_rejects_bad_items() -> None:
    assert decode_attributes(None, path="a") == {}
    with pytest.raises(OtlpError, match="a: must be a list"):
        decode_attributes({"key": "x"}, path="a")
    with pytest.raises(OtlpError, match=r"a\[0\]"):
        decode_attributes([{"value": {"stringValue": "x"}}], path="a")


@pytest.mark.parametrize(
    ("kind", "status", "expected_kind", "expected_status"),
    [
        (1, 1, "INTERNAL", "OK"),
        (5, 0, "CONSUMER", "UNSET"),
        ("SPAN_KIND_PRODUCER", "STATUS_CODE_OK", "PRODUCER", "OK"),
        (0, "STATUS_CODE_UNSET", "UNSPECIFIED", "UNSET"),
    ],
)
def test_kind_and_status_accept_int_and_enum_strings(
    kind: Any, status: Any, expected_kind: str, expected_status: str
) -> None:
    flat = _single(_raw_span(kind=kind, status={"code": status}))
    assert flat.error is None
    assert flat.span["kind"] == expected_kind
    assert flat.span["status"]["status_code"] == expected_status


@pytest.mark.parametrize("kind", [6, -1, "SERVER", "SPAN_KIND_BOGUS", True])
def test_unknown_kind_is_per_span_error(kind: Any) -> None:
    flat = _single(_raw_span(kind=kind))
    assert flat.span is None
    assert flat.error is not None and flat.error.startswith("kind:")


def test_unknown_status_code_is_per_span_error() -> None:
    flat = _single(_raw_span(status={"code": 9}))
    assert flat.error is not None and flat.error.startswith("status.code:")


def test_nanoseconds_truncate_to_microseconds() -> None:
    flat = _single(_raw_span(startTimeUnixNano="1767225605000001999", endTimeUnixNano=0))
    assert flat.span["start_time"] == "2026-01-01T00:00:05.000001+00:00"
    assert flat.span["end_time"] == "1970-01-01T00:00:00.000000+00:00"


@pytest.mark.parametrize("value", ["12a", "-5", -5, 1.5, True])
def test_bad_nanoseconds_is_per_span_error_with_path(value: Any) -> None:
    flat = _single(_raw_span(startTimeUnixNano=value))
    assert flat.span is None
    assert flat.error == "startTimeUnixNano: must be a decimal integer"


def test_bad_attribute_is_per_span_error_and_other_spans_survive() -> None:
    bad = _raw_span(attributes=[{"key": "n", "value": {"intValue": "x"}}])
    flats = flatten_otlp_spans(_payload(bad, _raw_span()))
    assert flats[0].error == "attributes[0].value.intValue: must be an integer"
    assert flats[1].error is None


def test_empty_parent_span_id_gives_none() -> None:
    assert _single(_raw_span(parentSpanId="")).span["parent_id"] is None
    span = _raw_span()
    del span["parentSpanId"]
    assert _single(span).span["parent_id"] is None


def test_missing_fields_are_left_absent() -> None:
    flat = _single(_raw_span(traceId="", name="", startTimeUnixNano=""))
    assert flat.error is None
    assert "trace_id" not in flat.span["context"]
    assert "name" not in flat.span
    assert "start_time" not in flat.span


def test_missing_scope_spans_and_spans_give_empty_list() -> None:
    assert flatten_otlp_spans({"resourceSpans": [{}]}) == []
    assert flatten_otlp_spans({"resourceSpans": [{"scopeSpans": [{}]}]}) == []
    assert flatten_otlp_spans({"resourceSpans": []}) == []


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "resourceSpans: required list"),
        ({"resourceSpans": {}}, "resourceSpans: required list"),
        ({"resourceSpans": [{"scopeSpans": {}}]}, "resourceSpans[0].scopeSpans: must be a list"),
        (
            {"resourceSpans": [{"scopeSpans": [{"spans": "x"}]}]},
            r"resourceSpans\[0\].scopeSpans\[0\].spans: must be a list",
        ),
    ],
)
def test_structural_errors_raise_with_path(payload: dict[str, Any], message: str) -> None:
    with pytest.raises(OtlpError, match=message.replace("[0]", r"\[0\]")):
        flatten_otlp_spans(payload)


def test_events_links_dropped_counts_and_passthrough() -> None:
    span = _raw_span(
        links=[
            {
                "traceId": TRACE_ID,
                "spanId": OTLP_CHILD_SPAN_ID,
                "attributes": [{"key": "link.kind", "value": {"stringValue": "follows"}}],
                "droppedAttributesCount": 1,
            }
        ],
        droppedEventsCount=3,
        droppedLinksCount=0,
        flags=256,
        traceState="vendor=fake",
    )
    flat = _single(span)
    assert flat.span["events"] == [
        {
            "name": "exception",
            "timestamp": "2026-01-01T00:00:05.140000+00:00",
            "attributes": {"exception.type": "UndefinedColumn"},
        }
    ]
    assert flat.span["links"] == [
        {
            "trace_id": TRACE_ID,
            "span_id": OTLP_CHILD_SPAN_ID,
            "attributes": {"link.kind": "follows"},
        }
    ]
    assert not any(key.startswith("dropped") for key in flat.span)
    assert "flags" not in flat.span
    assert flat.span["traceState"] == "vendor=fake"
