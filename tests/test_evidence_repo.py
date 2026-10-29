from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from cre.evidence import EVIDENCE_ADAPTER, Evidence
from cre.evidence_repo import (
    MAX_BATCH_LOAD,
    evidence_from_row,
    evidence_table,
    evidence_to_row,
    idempotency_key,
    load_evidence,
    load_evidence_many,
    store_evidence,
)
from cre.normalizers import normalize_deployment, normalize_log, normalize_span
from cre.redaction import REDACTED
from tests.evidence_samples import (
    DEPLOYMENT_SOURCE,
    FAKE_DB_PASSWORD,
    LOG_SOURCE,
    RETRIEVED_AT,
    SAMPLE_DEPLOYMENT,
    SAMPLE_LOG,
    SAMPLE_SPAN,
    SPAN_ID,
    SPAN_SOURCE,
    TRACE_ID,
)

EXPECTED_COLUMNS = [
    "id",
    "kind",
    "occurred_at",
    "trace_id",
    "span_id",
    "request_id",
    "service",
    "deployment_version",
    "source",
    "retrieval_method",
    "identifiers",
    "retrieved_at",
    "observed",
    "inferred",
    "idempotency_key",
    "stored_at",
]


def _base(kind: str, observed: dict[str, Any], **extra: Any) -> Evidence:
    return EVIDENCE_ADAPTER.validate_python(
        {
            "kind": kind,
            "timestamp": "2026-01-01T00:00:05+02:00",
            "provenance": {
                "source": {
                    "source_type": "fake",
                    "name": "fake source",
                    "uri": "file:///fake.log",
                    "locator": "line:1",
                },
                "retrieval_method": "fake_method",
                "identifiers": {"content_sha256": "cd" * 32, "extra": "x"},
                "retrieved_at": RETRIEVED_AT,
            },
            "inferred": [{"statement": "s", "basis": ["trace_id"], "method": "rule:x"}],
            **extra,
            "observed": observed,
        }
    )


ALL_KINDS: list[Evidence] = [
    _base("log", {"level": "INFO", "message": "m", "attributes": {"a": [1, {"b": None}]}}),
    _base(
        "span",
        {
            "name": "n",
            "start_time": "2026-01-01T00:00:05Z",
            "end_time": "2026-01-01T00:00:06Z",
            "attributes": {"k": "v"},
        },
        trace_id=TRACE_ID,
        span_id=SPAN_ID,
        request_id="demo-1",
        service="svc",
        deployment_version="0.1.0",
    ),
    _base("deployment", {"version": "0.1.0", "status": "succeeded", "commit": "deadbeef"}),
    _base("commit", {"sha": "deadbeef", "message": "m", "files": ["a.py"]}),
    _base("config_change", {"key": "FLAG", "old_value": "a", "new_value": "b"}),
]


def _assert_same(loaded: Evidence, expected: Evidence) -> None:
    assert loaded == expected
    assert loaded.provenance == expected.provenance
    assert loaded.provenance.source == expected.provenance.source
    assert loaded.timestamp == expected.timestamp
    assert loaded.timestamp.tzinfo is not None
    assert loaded.provenance.retrieved_at.tzinfo is not None


@pytest.mark.parametrize("item", ALL_KINDS, ids=lambda i: i.kind)
def test_row_round_trip_each_kind(item: Evidence) -> None:
    row = evidence_to_row(item)
    assert row["occurred_at"].tzinfo is not None
    assert row["retrieved_at"].tzinfo is not None
    _assert_same(evidence_from_row(row), item)


def test_store_evidence_executes_single_insert(fake_conn: MagicMock) -> None:
    item = normalize_log(SAMPLE_LOG, source=LOG_SOURCE, retrieved_at=RETRIEVED_AT)
    fake_conn.execute.return_value.scalar_one_or_none.return_value = item.id
    returned = store_evidence(fake_conn, item)
    assert returned == item.id
    fake_conn.execute.assert_called_once()
    stmt = fake_conn.execute.call_args.args[0]
    assert stmt.is_insert
    assert stmt.table.name == "evidence_items"
    params = stmt.compile().params
    assert params == evidence_to_row(item)
    assert FAKE_DB_PASSWORD not in repr(params)
    fake_conn.commit.assert_not_called()


def test_store_evidence_redacts_unredacted_item(fake_conn: MagicMock) -> None:
    item = _base("log", {"level": "INFO", "message": "password=fake-store-pw"})
    store_evidence(fake_conn, item)
    params = fake_conn.execute.call_args.args[0].compile().params
    assert "fake-store-pw" not in repr(params)
    assert params["observed"]["message"] == f"password={REDACTED}"


@pytest.mark.parametrize(
    "item",
    [
        normalize_log(SAMPLE_LOG, source=LOG_SOURCE, retrieved_at=RETRIEVED_AT),
        normalize_span(SAMPLE_SPAN, source=SPAN_SOURCE, retrieved_at=RETRIEVED_AT),
        normalize_deployment(
            SAMPLE_DEPLOYMENT, source=DEPLOYMENT_SOURCE, retrieved_at=RETRIEVED_AT
        ),
    ],
    ids=lambda i: i.kind,
)
def test_normalize_store_load_round_trip(fake_conn: MagicMock, item: Evidence) -> None:
    fake_conn.execute.return_value.scalar_one_or_none.return_value = item.id
    stored_id = store_evidence(fake_conn, item)
    assert stored_id is not None
    params = fake_conn.execute.call_args.args[0].compile().params
    fake_conn.execute.reset_mock()
    fake_conn.execute.return_value.mappings.return_value.first.return_value = {
        **params,
        "stored_at": RETRIEVED_AT,
    }
    loaded = load_evidence(fake_conn, stored_id)
    assert loaded is not None
    _assert_same(loaded, item)
    assert loaded.provenance.identifiers == item.provenance.identifiers
    assert loaded.provenance.retrieval_method == item.provenance.retrieval_method
    select_stmt = fake_conn.execute.call_args.args[0]
    assert select_stmt.compile().params == {"id_1": stored_id}
    assert FAKE_DB_PASSWORD not in loaded.model_dump_json()


def test_normalize_store_load_round_trip_bare_password(fake_conn: MagicMock) -> None:
    log = {**SAMPLE_LOG, "message": "auth failed password=fake-bare-pw token=abc"}
    item = normalize_log(log, source=LOG_SOURCE, retrieved_at=RETRIEVED_AT)
    assert item.observed.message == f"auth failed password={REDACTED} token={REDACTED}"
    fake_conn.execute.return_value.scalar_one_or_none.return_value = item.id
    stored_id = store_evidence(fake_conn, item)
    assert stored_id is not None
    params = fake_conn.execute.call_args.args[0].compile().params
    assert params == evidence_to_row(item)
    assert params["observed"]["message"] == item.observed.message
    fake_conn.execute.return_value.mappings.return_value.first.return_value = {
        **params,
        "stored_at": RETRIEVED_AT,
    }
    loaded = load_evidence(fake_conn, stored_id)
    assert loaded is not None
    _assert_same(loaded, item)
    assert "fake-bare-pw" not in loaded.model_dump_json()


def test_load_evidence_returns_none_when_missing(fake_conn: MagicMock) -> None:
    fake_conn.execute.return_value.mappings.return_value.first.return_value = None
    assert load_evidence(fake_conn, uuid4()) is None


def test_evidence_from_row_unknown_kind() -> None:
    row = evidence_to_row(ALL_KINDS[0])
    row["kind"] = "metric"
    with pytest.raises(ValidationError):
        evidence_from_row(row)


def test_evidence_from_row_missing_column() -> None:
    row = evidence_to_row(ALL_KINDS[0])
    del row["retrieved_at"]
    with pytest.raises(KeyError):
        evidence_from_row(row)


def test_evidence_table_columns_match_migration() -> None:
    assert evidence_table.c.keys() == EXPECTED_COLUMNS


def test_store_evidence_compiles_on_conflict_do_nothing_returning(fake_conn: MagicMock) -> None:
    item = normalize_log(SAMPLE_LOG, source=LOG_SOURCE, retrieved_at=RETRIEVED_AT)
    store_evidence(fake_conn, item)
    stmt = fake_conn.execute.call_args.args[0]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (idempotency_key) DO NOTHING" in sql
    assert "RETURNING evidence_items.id" in sql


def test_store_evidence_returns_id_when_inserted(fake_conn: MagicMock) -> None:
    item = normalize_log(SAMPLE_LOG, source=LOG_SOURCE, retrieved_at=RETRIEVED_AT)
    fake_conn.execute.return_value.scalar_one_or_none.return_value = item.id
    assert store_evidence(fake_conn, item) == item.id


def test_store_evidence_returns_none_for_duplicate(fake_conn: MagicMock) -> None:
    item = normalize_log(SAMPLE_LOG, source=LOG_SOURCE, retrieved_at=RETRIEVED_AT)
    fake_conn.execute.return_value.scalar_one_or_none.return_value = None
    assert store_evidence(fake_conn, item) is None


def test_evidence_to_row_includes_idempotency_key() -> None:
    item = ALL_KINDS[0]
    assert evidence_to_row(item)["idempotency_key"] == idempotency_key(item)
    assert len(idempotency_key(item)) == 64


def _with_provenance(item: Evidence, **changes: Any) -> Evidence:
    data = item.model_dump(mode="json")
    for path, value in changes.items():
        target = data
        *parents, leaf = path.split("__")
        for part in parents:
            target = target[part]
        target[leaf] = value
    return EVIDENCE_ADAPTER.validate_python(data)


def test_idempotency_key_ignores_id_retrieved_at_and_locator() -> None:
    item = ALL_KINDS[0]
    other = _with_provenance(
        item,
        id=str(uuid4()),
        provenance__retrieved_at="2030-06-01T00:00:00Z",
        provenance__source__locator="line:999",
        provenance__source__uri="file:///other.log",
    )
    assert other.id != item.id
    assert idempotency_key(other) == idempotency_key(item)


@pytest.mark.parametrize(
    "changes",
    [
        {"provenance__identifiers": {"content_sha256": "ef" * 32, "extra": "x"}},
        {"provenance__source__name": "another source"},
    ],
    ids=["identifier", "source_name"],
)
def test_idempotency_key_changes_with_identity_fields(changes: dict[str, Any]) -> None:
    item = ALL_KINDS[0]
    assert idempotency_key(_with_provenance(item, **changes)) != idempotency_key(item)


def test_idempotency_key_changes_with_kind() -> None:
    log, deployment = ALL_KINDS[0], ALL_KINDS[2]
    same_provenance = _with_provenance(
        deployment, provenance=log.provenance.model_dump(mode="json")
    )
    assert idempotency_key(same_provenance) != idempotency_key(log)


def test_load_evidence_many_empty_does_not_execute(fake_conn: MagicMock) -> None:
    assert load_evidence_many(fake_conn, []) == []
    fake_conn.execute.assert_not_called()


def test_load_evidence_many_over_limit_raises(fake_conn: MagicMock) -> None:
    with pytest.raises(ValueError):
        load_evidence_many(fake_conn, [uuid4() for _ in range(MAX_BATCH_LOAD + 1)])
    fake_conn.execute.assert_not_called()


def test_load_evidence_many_maps_rows_and_dedupes(fake_conn: MagicMock) -> None:
    rows = [evidence_to_row(item) for item in ALL_KINDS[:2]]
    fake_conn.execute.return_value.mappings.return_value.all.return_value = rows
    ids = [ALL_KINDS[0].id, ALL_KINDS[1].id, ALL_KINDS[0].id]
    loaded = load_evidence_many(fake_conn, ids)
    assert [item.id for item in loaded] == [ALL_KINDS[0].id, ALL_KINDS[1].id]
    _assert_same(loaded[0], ALL_KINDS[0])
    stmt = fake_conn.execute.call_args.args[0]
    params = stmt.compile(dialect=postgresql.dialect()).params
    assert list(params.values()) == [[ALL_KINDS[0].id, ALL_KINDS[1].id]]
