from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from cre.evidence import (
    EVIDENCE_ADAPTER,
    CommitEvidence,
    ConfigChangeEvidence,
    DeploymentEvidence,
    Inference,
    LogEvidence,
    SpanEvidence,
)
from tests.evidence_samples import PARENT_SPAN_ID, RETRIEVED_AT, SPAN_ID, TRACE_ID

TS = "2026-01-01T00:00:05Z"


def _provenance() -> dict[str, Any]:
    return {
        "source": {"source_type": "structured_log", "name": "fake source"},
        "retrieval_method": "structured_log_json",
        "identifiers": {"content_sha256": "ab" * 32},
        "retrieved_at": RETRIEVED_AT.isoformat(),
    }


def _log(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": "log",
        "timestamp": TS,
        "provenance": _provenance(),
        "trace_id": TRACE_ID,
        "span_id": SPAN_ID,
        "observed": {"level": "ERROR", "message": "order_lookup_failed"},
    }
    data.update(overrides)
    return data


def _span(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kind": "span",
        "timestamp": TS,
        "provenance": _provenance(),
        "trace_id": TRACE_ID,
        "span_id": SPAN_ID,
        "observed": {
            "name": "GET /orders/{order_id}",
            "start_time": "2026-01-01T00:00:05Z",
            "end_time": "2026-01-01T00:00:06Z",
            "parent_span_id": PARENT_SPAN_ID,
        },
    }
    data.update(overrides)
    return data


VALID_ITEMS: list[tuple[dict[str, Any], type]] = [
    (_log(), LogEvidence),
    (_span(), SpanEvidence),
    (
        {
            "kind": "deployment",
            "timestamp": TS,
            "provenance": _provenance(),
            "observed": {"version": "0.1.0", "status": "succeeded"},
        },
        DeploymentEvidence,
    ),
    (
        {
            "kind": "commit",
            "timestamp": TS,
            "provenance": _provenance(),
            "observed": {"sha": "deadbeef", "message": "fake commit", "files": ["a.py"]},
        },
        CommitEvidence,
    ),
    (
        {
            "kind": "config_change",
            "timestamp": TS,
            "provenance": _provenance(),
            "observed": {"key": "FAKE_FLAG", "old_value": "off", "new_value": "on"},
        },
        ConfigChangeEvidence,
    ),
]


@pytest.mark.parametrize(("data", "expected"), VALID_ITEMS)
def test_each_kind_validates_to_its_subclass(data: dict[str, Any], expected: type) -> None:
    item = EVIDENCE_ADAPTER.validate_python(data)
    assert isinstance(item, expected)
    assert item.timestamp.tzinfo is not None
    assert item.inferred == []


def test_missing_timestamp_rejected() -> None:
    data = _log()
    del data["timestamp"]
    with pytest.raises(ValidationError):
        EVIDENCE_ADAPTER.validate_python(data)


def test_missing_provenance_rejected() -> None:
    data = _log()
    del data["provenance"]
    with pytest.raises(ValidationError):
        EVIDENCE_ADAPTER.validate_python(data)


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(ValidationError):
        EVIDENCE_ADAPTER.validate_python(_log(timestamp="2026-01-01T00:00:05"))


def test_naive_retrieved_at_rejected() -> None:
    provenance = _provenance()
    provenance["retrieved_at"] = "2026-01-01T00:00:00"
    with pytest.raises(ValidationError):
        EVIDENCE_ADAPTER.validate_python(_log(provenance=provenance))


def test_empty_identifiers_rejected() -> None:
    provenance = _provenance()
    provenance["identifiers"] = {}
    with pytest.raises(ValidationError):
        EVIDENCE_ADAPTER.validate_python(_log(provenance=provenance))


def test_empty_retrieval_method_rejected() -> None:
    provenance = _provenance()
    provenance["retrieval_method"] = ""
    with pytest.raises(ValidationError):
        EVIDENCE_ADAPTER.validate_python(_log(provenance=provenance))


def test_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        EVIDENCE_ADAPTER.validate_python(_log(confidence=0.9))


def test_extra_observed_field_rejected() -> None:
    observed = {"level": "ERROR", "message": "x", "score": 1}
    with pytest.raises(ValidationError):
        EVIDENCE_ADAPTER.validate_python(_log(observed=observed))


def test_unknown_kind_rejected() -> None:
    with pytest.raises(ValidationError):
        EVIDENCE_ADAPTER.validate_python(_log(kind="metric"))


@pytest.mark.parametrize("bad", [TRACE_ID.upper(), TRACE_ID[:-1], "0" * 32, "g" * 32])
def test_malformed_trace_id_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        EVIDENCE_ADAPTER.validate_python(_log(trace_id=bad))


@pytest.mark.parametrize("bad", [SPAN_ID.upper(), SPAN_ID + "0", "0" * 16, "z" * 16])
def test_malformed_span_id_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        EVIDENCE_ADAPTER.validate_python(_log(span_id=bad))


def test_malformed_parent_span_id_rejected() -> None:
    data = _span()
    data["observed"] = {**data["observed"], "parent_span_id": "0" * 16}
    with pytest.raises(ValidationError):
        EVIDENCE_ADAPTER.validate_python(data)


@pytest.mark.parametrize("missing", ["trace_id", "span_id"])
def test_span_requires_trace_and_span_ids(missing: str) -> None:
    data = _span()
    del data[missing]
    with pytest.raises(ValidationError):
        EVIDENCE_ADAPTER.validate_python(data)


def test_log_without_ids_is_allowed() -> None:
    item = EVIDENCE_ADAPTER.validate_python(_log(trace_id=None, span_id=None))
    assert item.trace_id is None and item.span_id is None


def test_span_end_before_start_rejected() -> None:
    data = _span()
    data["observed"] = {**data["observed"], "end_time": "2026-01-01T00:00:04Z"}
    with pytest.raises(ValidationError):
        EVIDENCE_ADAPTER.validate_python(data)


def test_commit_with_non_hex_sha_rejected() -> None:
    data = dict(VALID_ITEMS[3][0])
    data["observed"] = {"sha": "not-a-sha", "message": "fake"}
    with pytest.raises(ValidationError):
        EVIDENCE_ADAPTER.validate_python(data)


def test_inferred_defaults_empty_and_accepts_inference() -> None:
    inference = {"statement": "same trace", "basis": ["trace_id"], "method": "rule:same_trace_id"}
    item = EVIDENCE_ADAPTER.validate_python(_log(inferred=[inference]))
    assert item.inferred == [Inference(**inference)]


def test_inference_with_empty_basis_rejected() -> None:
    with pytest.raises(ValidationError):
        Inference(statement="same trace", basis=[], method="rule:same_trace_id")


def test_models_are_frozen() -> None:
    item = EVIDENCE_ADAPTER.validate_python(_log())
    with pytest.raises(ValidationError):
        item.timestamp = datetime.now(UTC) - timedelta(days=1)  # type: ignore[misc]
    with pytest.raises(ValidationError):
        item.provenance.retrieval_method = "other"  # type: ignore[misc]
