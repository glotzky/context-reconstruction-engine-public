import copy
import json
from datetime import datetime
from typing import Any

import pytest

from cre.normalizers import (
    NormalizationError,
    normalize_commit,
    normalize_deployment,
    normalize_log,
    normalize_span,
)
from cre.redaction import REDACTED
from tests.evidence_samples import (
    COMMIT_SOURCE,
    DEPLOYMENT_SOURCE,
    FAKE_DB_PASSWORD,
    LOG_SOURCE,
    PARENT_SPAN_ID,
    RETRIEVED_AT,
    SAMPLE_COMMIT,
    SAMPLE_DEPLOYMENT,
    SAMPLE_LOG,
    SAMPLE_SPAN,
    SPAN_ID,
    SPAN_SOURCE,
    TRACE_ID,
)


def _log(record: dict[str, Any] | str = SAMPLE_LOG) -> Any:
    return normalize_log(record, source=LOG_SOURCE, retrieved_at=RETRIEVED_AT)


def _span(span: dict[str, Any] = SAMPLE_SPAN) -> Any:
    return normalize_span(span, source=SPAN_SOURCE, retrieved_at=RETRIEVED_AT)


def _deployment(event: dict[str, Any] = SAMPLE_DEPLOYMENT) -> Any:
    return normalize_deployment(event, source=DEPLOYMENT_SOURCE, retrieved_at=RETRIEVED_AT)


def test_log_happy_path_maps_every_field() -> None:
    item = _log()
    assert item.kind == "log"
    assert item.timestamp == datetime.fromisoformat("2026-01-01T00:00:05.123+00:00")
    assert item.service == "cre-lab-api"
    assert item.deployment_version == "0.1.0"
    assert item.request_id == "demo-1"
    assert item.trace_id == TRACE_ID
    assert item.span_id == SPAN_ID
    assert item.observed.level == "ERROR"
    assert item.observed.logger == "cre.api"
    assert item.observed.message == "order_lookup_failed"
    attributes = item.observed.attributes
    assert attributes["commit"] == "deadbeef"
    assert attributes["error_type"] == "ProgrammingError"
    assert 'column "customer_name" does not exist' in attributes["sql_error"]
    assert attributes["order_id"] == 1
    assert "exc_info" in attributes
    assert item.provenance.source == LOG_SOURCE
    assert item.provenance.retrieval_method == "structured_log_json"
    assert item.provenance.retrieved_at == RETRIEVED_AT
    assert set(item.provenance.identifiers) == {"content_sha256", "request_id", "trace_id"}
    assert item.provenance.identifiers["request_id"] == "demo-1"
    assert item.provenance.identifiers["trace_id"] == TRACE_ID


def test_log_json_string_matches_dict() -> None:
    from_dict = _log()
    from_str = _log(json.dumps(SAMPLE_LOG))
    assert from_str.model_dump(exclude={"id"}) == from_dict.model_dump(exclude={"id"})


def test_log_null_ids() -> None:
    record = {**SAMPLE_LOG, "request_id": None, "trace_id": None, "span_id": None}
    item = _log(record)
    assert item.request_id is None and item.trace_id is None and item.span_id is None
    assert list(item.provenance.identifiers) == ["content_sha256"]


def test_log_connection_string_password_redacted() -> None:
    item = _log()
    assert FAKE_DB_PASSWORD not in item.model_dump_json()
    assert (
        f"postgresql+psycopg://lab:{REDACTED}@db:5432/lab" in item.observed.attributes["sql_error"]
    )


def test_log_input_not_mutated() -> None:
    record = copy.deepcopy(SAMPLE_LOG)
    _log(record)
    assert record == SAMPLE_LOG


def test_content_sha256_is_deterministic() -> None:
    first = _log().provenance.identifiers["content_sha256"]
    second = _log().provenance.identifiers["content_sha256"]
    assert first == second
    assert len(first) == 64
    other = _log({**SAMPLE_LOG, "order_id": 2}).provenance.identifiers["content_sha256"]
    assert other != first


def test_log_missing_timestamp() -> None:
    record = {k: v for k, v in SAMPLE_LOG.items() if k != "timestamp"}
    with pytest.raises(NormalizationError, match="timestamp"):
        _log(record)


def test_log_naive_timestamp() -> None:
    with pytest.raises(NormalizationError, match="timestamp"):
        _log({**SAMPLE_LOG, "timestamp": "2026-01-01T00:00:05"})


def test_log_invalid_json() -> None:
    with pytest.raises(NormalizationError, match="invalid JSON"):
        _log("{not json")


def test_log_non_object_json() -> None:
    with pytest.raises(NormalizationError, match="object"):
        _log("[1, 2]")


def test_log_malformed_trace_id_wrapped() -> None:
    with pytest.raises(NormalizationError, match="trace_id"):
        _log({**SAMPLE_LOG, "trace_id": "abc"})


def test_span_happy_path() -> None:
    item = _span()
    assert item.kind == "span"
    assert item.trace_id == TRACE_ID
    assert item.span_id == SPAN_ID
    assert item.timestamp == item.observed.start_time
    assert item.timestamp == datetime.fromisoformat("2026-01-01T00:00:05.100+00:00")
    assert item.service == "cre-lab-api"
    assert item.deployment_version == "0.1.0"
    assert item.request_id == "demo-1"
    assert item.observed.parent_span_id == PARENT_SPAN_ID
    assert item.observed.status_code == "ERROR"
    assert item.observed.status_message == "UndefinedColumn"
    assert item.observed.span_kind == "SERVER"
    attributes = item.observed.attributes
    assert attributes["resource"]["deployment.commit"] == "deadbeef"
    assert attributes["http.status_code"] == 500
    assert attributes["events"][0]["name"] == "exception"
    assert "links" not in attributes
    assert item.provenance.retrieval_method == "otel_sdk_span_json"
    assert set(item.provenance.identifiers) == {"content_sha256", "trace_id", "span_id"}


def test_span_missing_trace_id() -> None:
    span = copy.deepcopy(SAMPLE_SPAN)
    del span["context"]["trace_id"]
    with pytest.raises(NormalizationError, match="trace_id"):
        _span(span)


def test_span_missing_context() -> None:
    span = {k: v for k, v in SAMPLE_SPAN.items() if k != "context"}
    with pytest.raises(NormalizationError, match="context"):
        _span(span)


def test_span_bad_end_time() -> None:
    with pytest.raises(NormalizationError, match="end_time"):
        _span({**SAMPLE_SPAN, "end_time": "not-a-time"})


def test_span_end_before_start() -> None:
    with pytest.raises(NormalizationError):
        _span({**SAMPLE_SPAN, "end_time": "2026-01-01T00:00:04Z"})


def test_deployment_happy_path() -> None:
    item = _deployment()
    assert item.kind == "deployment"
    assert item.timestamp == datetime.fromisoformat("2026-01-01T00:00:00+00:00")
    assert item.service == "cre-lab-api"
    assert item.deployment_version == "0.1.0"
    assert item.observed.version == "0.1.0"
    assert item.observed.status == "succeeded"
    assert item.observed.commit == "deadbeef"
    assert item.observed.environment == "lab"
    assert item.observed.actor == "fake-deployer"
    assert item.provenance.identifiers["deployment_id"] == "deploy-0001"
    assert item.observed.attributes == {"pipeline_run": "fake-run-7"}
    assert item.provenance.retrieval_method == "deployment_record"


def test_deployment_missing_version() -> None:
    event = {k: v for k, v in SAMPLE_DEPLOYMENT.items() if k != "version"}
    with pytest.raises(NormalizationError, match="version"):
        _deployment(event)


def test_no_normalizer_populates_inferred() -> None:
    assert _log().inferred == []
    assert _span().inferred == []
    assert _deployment().inferred == []


def _commit(record: dict[str, Any] = SAMPLE_COMMIT) -> Any:
    return normalize_commit(record, source=COMMIT_SOURCE, retrieved_at=RETRIEVED_AT)


def test_normalize_commit_happy_path() -> None:
    item = _commit()
    sha = SAMPLE_COMMIT["sha"].lower()
    assert item.kind == "commit"
    assert item.observed.sha == sha
    assert item.observed.message == SAMPLE_COMMIT["message"]
    assert item.observed.author == "Fake Developer"
    assert item.observed.files == SAMPLE_COMMIT["files"]
    assert item.timestamp == datetime.fromisoformat("2025-12-31T23:50:00+00:00")
    assert item.service == "cre-lab-api"
    assert item.deployment_version == "0.1.0"
    assert item.provenance.identifiers["commit_sha"] == sha
    assert "content_sha256" in item.provenance.identifiers
    assert item.provenance.retrieval_method == "commit_record"
    assert item.inferred == []


def test_normalize_commit_missing_committed_at() -> None:
    record = {k: v for k, v in SAMPLE_COMMIT.items() if k != "committed_at"}
    with pytest.raises(NormalizationError, match="missing required field: committed_at"):
        _commit(record)


def test_normalize_commit_invalid_sha() -> None:
    with pytest.raises(NormalizationError, match="invalid commit record"):
        _commit({**SAMPLE_COMMIT, "sha": "not-a-sha"})


def test_normalize_commit_redacts_secret_in_message() -> None:
    item = _commit({**SAMPLE_COMMIT, "message": f"fix: password={FAKE_DB_PASSWORD}"})
    assert FAKE_DB_PASSWORD not in item.model_dump_json()
    assert item.observed.message == f"fix: password={REDACTED}"
