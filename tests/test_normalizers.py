import copy
import json
from datetime import datetime
from typing import Any

import pytest

from cre.evidence import SourceRef
from cre.normalizers import (
    NormalizationError,
    normalize_commit,
    normalize_config_change,
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


CONFIG_SOURCE = SourceRef(source_type="config_change", name="fake config history")
SAMPLE_CONFIG_CHANGE: dict[str, Any] = {
    "changed_at": "2026-01-01T00:10:00Z",
    "service": "cre-lab-api",
    "version": "0.1.0",
    "actor": "alice@example.com",
    "change_id": "fake-change-1",
    "changes": [
        {"key": "db.pool_size", "old_value": "5", "new_value": "50"},
        {"key": "feature.flag", "old_value": "off", "new_value": "on"},
    ],
}


def _config(record: dict[str, Any] | str = SAMPLE_CONFIG_CHANGE) -> Any:
    return normalize_config_change(record, source=CONFIG_SOURCE, retrieved_at=RETRIEVED_AT)


def _one_change(change: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {**SAMPLE_CONFIG_CHANGE, "changes": [change], **fields}


def test_normalize_config_change_happy_path() -> None:
    items = _config()
    assert len(items) == 2
    expected = [("db.pool_size", "5", "50"), ("feature.flag", "off", "on")]
    for index, (item, (key, old, new)) in enumerate(zip(items, expected, strict=True)):
        assert item.kind == "config_change"
        assert item.timestamp == datetime.fromisoformat("2026-01-01T00:10:00+00:00")
        assert item.service == "cre-lab-api"
        assert item.deployment_version == "0.1.0"
        assert item.observed.key == key
        assert item.observed.old_value == old
        assert item.observed.new_value == new
        assert item.observed.changed_by == "alice@example.com"
        assert item.provenance.retrieval_method == "config_change_record"
        assert item.provenance.retrieved_at == RETRIEVED_AT
        assert item.provenance.source.source_type == "config_change"
        assert item.provenance.source.name == "fake config history"
        assert item.provenance.source.locator == f"changes[{index}]"
        identifiers = item.provenance.identifiers
        assert set(identifiers) == {"content_sha256", "config_key", "change_id"}
        assert identifiers["config_key"] == key
        assert identifiers["change_id"] == "fake-change-1"
        assert item.inferred == []


def test_normalize_config_change_without_change_id_and_with_locator() -> None:
    record = {k: v for k, v in SAMPLE_CONFIG_CHANGE.items() if k != "change_id"}
    source = CONFIG_SOURCE.model_copy(update={"locator": "[3]"})
    items = normalize_config_change(record, source=source, retrieved_at=RETRIEVED_AT)
    assert [i.provenance.source.locator for i in items] == ["[3].changes[0]", "[3].changes[1]"]
    assert all("change_id" not in i.provenance.identifiers for i in items)


def test_normalize_config_change_accepts_json_string_input() -> None:
    from_string = _config(json.dumps(SAMPLE_CONFIG_CHANGE))
    from_dict = _config(SAMPLE_CONFIG_CHANGE)
    assert len(from_string) == len(from_dict) == 2
    for a, b in zip(from_string, from_dict, strict=True):
        assert a.model_dump(exclude={"id"}) == b.model_dump(exclude={"id"})


def test_normalize_config_change_added_and_removed_keys() -> None:
    record = {
        **SAMPLE_CONFIG_CHANGE,
        "changes": [
            {"key": "feature.new", "new_value": "on"},
            {"key": "feature.old", "old_value": "on", "new_value": None},
        ],
    }
    added, removed = _config(record)
    assert (added.observed.old_value, added.observed.new_value) == (None, "on")
    assert (removed.observed.old_value, removed.observed.new_value) == ("on", None)


def test_normalize_config_change_non_string_values_serialized() -> None:
    record = {
        **SAMPLE_CONFIG_CHANGE,
        "changes": [
            {"key": "db.pool_size", "old_value": 5, "new_value": 50},
            {"key": "feature.enabled", "old_value": False, "new_value": True},
            {"key": "retry", "old_value": {"max": 3, "backoff": 1.5}, "new_value": [1, "a"]},
        ],
    }
    pool, flag, retry = _config(record)
    assert (pool.observed.old_value, pool.observed.new_value) == ("5", "50")
    assert (flag.observed.old_value, flag.observed.new_value) == ("false", "true")
    assert retry.observed.old_value == '{"backoff":1.5,"max":3}'
    assert retry.observed.new_value == '[1,"a"]'


@pytest.mark.parametrize("key", ["DB_PASSWORD", "STRIPE_API_KEY", "SERVICE_TOKEN", "DATABASE_URL"])
def test_normalize_config_change_redacts_sensitive_key_values(key: str) -> None:
    (item,) = _config(_one_change({"key": key, "old_value": "hunter2", "new_value": "hunter3"}))
    assert item.observed.key == key
    assert item.observed.old_value == REDACTED
    assert item.observed.new_value == REDACTED
    dumped = item.model_dump_json()
    assert "hunter2" not in dumped and "hunter3" not in dumped


@pytest.mark.parametrize(
    ("old", "new", "secrets"),
    [
        ("postgres://app:hunter2@db.example/x", "postgres://app:hunter3@db.example/x", []),
        ("token=abc123", "token=def456", ["abc123", "def456"]),
        ("sk-fakefakefakefake1234", "sk-fakefakefakefake5678", []),
        ({"password": "hunter2", "pool": 5}, {"password": "hunter3", "pool": 6}, []),
    ],
)
def test_normalize_config_change_redacts_secrets_inside_values(
    old: Any, new: Any, secrets: list[str]
) -> None:
    (item,) = _config(_one_change({"key": "app.settings", "old_value": old, "new_value": new}))
    dumped = item.model_dump_json()
    for secret in ["hunter2", "hunter3", "sk-fakefakefakefake", *secrets]:
        assert secret not in dumped
    assert REDACTED in item.observed.old_value
    assert REDACTED in item.observed.new_value
    if isinstance(old, dict):
        assert item.observed.old_value == '{"password":"[REDACTED]","pool":5}'


def test_normalize_config_change_redacts_secret_in_key_name() -> None:
    secret_key = "ghp_" + "a1" * 12
    (item,) = _config(_one_change({"key": secret_key, "old_value": "x", "new_value": "y"}))
    assert item.observed.key == REDACTED
    assert item.provenance.identifiers["config_key"] == REDACTED
    assert secret_key not in item.model_dump_json()


def test_normalize_config_change_redacted_secrets_still_count_as_changed() -> None:
    (item,) = _config(
        _one_change({"key": "API_TOKEN", "old_value": "hunter2", "new_value": "hunter3"})
    )
    assert item.observed.old_value == REDACTED
    assert item.observed.new_value == REDACTED


def test_normalize_config_change_identifiers_stable_and_distinct() -> None:
    first = _config()
    second = _config()
    assert [i.provenance.identifiers for i in first] == [i.provenance.identifiers for i in second]
    hashes = {i.provenance.identifiers["content_sha256"] for i in first}
    assert len(hashes) == 2


_VALID_CHANGE = {"key": "db.pool_size", "old_value": "5", "new_value": "50"}


@pytest.mark.parametrize(
    "record",
    [
        "not json",
        "[1, 2]",
        {k: v for k, v in SAMPLE_CONFIG_CHANGE.items() if k != "changes"},
        {**SAMPLE_CONFIG_CHANGE, "changes": []},
        {**SAMPLE_CONFIG_CHANGE, "changes": _VALID_CHANGE},
        {**SAMPLE_CONFIG_CHANGE, "changes": ["db.pool_size"]},
        _one_change({"old_value": "5", "new_value": "50"}),
        _one_change({"key": "", "old_value": "5", "new_value": "50"}),
        _one_change({"key": "k" * 201, "old_value": "5", "new_value": "50"}),
        _one_change({"key": "db.pool_size", "old_value": None, "new_value": None}),
        _one_change({"key": "db.pool_size"}),
        _one_change({"key": "db.pool_size", "old_value": "5", "new_value": "5"}),
        {**SAMPLE_CONFIG_CHANGE, "changes": [_VALID_CHANGE, _VALID_CHANGE]},
        {**SAMPLE_CONFIG_CHANGE, "changed_at": "2026-01-01T00:10:00"},
        {k: v for k, v in SAMPLE_CONFIG_CHANGE.items() if k != "changed_at"},
        {k: v for k, v in SAMPLE_CONFIG_CHANGE.items() if k != "service"},
        {k: v for k, v in SAMPLE_CONFIG_CHANGE.items() if k != "version"},
        {k: v for k, v in SAMPLE_CONFIG_CHANGE.items() if k != "actor"},
        {**SAMPLE_CONFIG_CHANGE, "actor": 42},
        {**SAMPLE_CONFIG_CHANGE, "environment": "lab"},
        _one_change({**_VALID_CHANGE, "note": "extra"}),
        {
            **SAMPLE_CONFIG_CHANGE,
            "changes": [{"key": f"k{i}", "new_value": "v"} for i in range(501)],
        },
    ],
    ids=[
        "invalid_json",
        "non_object",
        "missing_changes",
        "empty_changes",
        "changes_not_list",
        "change_not_object",
        "missing_key",
        "empty_key",
        "overlong_key",
        "both_null",
        "both_absent",
        "old_equals_new",
        "duplicate_keys",
        "naive_changed_at",
        "missing_changed_at",
        "missing_service",
        "missing_version",
        "missing_actor",
        "non_string_actor",
        "unknown_record_field",
        "unknown_change_field",
        "too_many_changes",
    ],
)
def test_normalize_config_change_rejects_malformed(record: Any) -> None:
    with pytest.raises(NormalizationError):
        _config(record)


def test_normalize_config_change_accepts_max_changes() -> None:
    record = {
        **SAMPLE_CONFIG_CHANGE,
        "changes": [{"key": f"k{i}", "new_value": "v"} for i in range(500)],
    }
    assert len(_config(record)) == 500


def test_normalize_config_change_error_message_has_no_values() -> None:
    value = "fake-unique-value-hunter2"
    with pytest.raises(NormalizationError) as excinfo:
        _config(_one_change({"key": "db.pool_size", "old_value": value, "new_value": value}))
    message = str(excinfo.value)
    assert value not in message
    assert "old_value and new_value are equal" in message
    assert message.startswith("invalid config change record:")
