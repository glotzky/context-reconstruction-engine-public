from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

from cre.config import Settings
from cre.evidence import Evidence
from cre.evidence_repo import evidence_to_row
from cre.normalizers import normalize_deployment, normalize_log, normalize_span
from cre.retrieval import (
    EvidenceQuery,
    RetrievalQueryError,
    build_select,
    resolve_query,
    retrieve_evidence,
)
from tests.evidence_samples import (
    DEPLOYMENT_SOURCE,
    LOG_SOURCE,
    RETRIEVED_AT,
    SAMPLE_DEPLOYMENT,
    SAMPLE_LOG,
    SAMPLE_SPAN,
    SPAN_SOURCE,
    TRACE_ID,
)

ANCHOR = datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC)
ORDER_BY = "ORDER BY evidence_items.occurred_at ASC, evidence_items.id ASC"

LOG = normalize_log(SAMPLE_LOG, source=LOG_SOURCE, retrieved_at=RETRIEVED_AT)
SPAN = normalize_span(SAMPLE_SPAN, source=SPAN_SOURCE, retrieved_at=RETRIEVED_AT)
DEPLOYMENT = normalize_deployment(
    SAMPLE_DEPLOYMENT, source=DEPLOYMENT_SOURCE, retrieved_at=RETRIEVED_AT
)


@pytest.fixture
def settings() -> Settings:
    return Settings()


def _row(item: Evidence) -> dict[str, Any]:
    return {**evidence_to_row(item), "stored_at": RETRIEVED_AT}


def _db_returns(conn: MagicMock, items: list[Evidence]) -> None:
    result = MagicMock(name="select_result")
    result.mappings.return_value.all.return_value = [_row(item) for item in items]
    conn.execute.side_effect = [MagicMock(name="set_ro"), MagicMock(name="set_timeout"), result]


def _select_sql(conn: MagicMock) -> tuple[str, dict[str, Any]]:
    compiled = conn.execute.call_args_list[2].args[0].compile(dialect=postgresql.dialect())
    return str(compiled), dict(compiled.params)


def _flat(sql: str) -> str:
    return " ".join(sql.split())


def _query(**fields: Any) -> EvidenceQuery:
    return EvidenceQuery.model_validate({"anchor": ANCHOR, **fields})


def test_trace_lookup_filters_trace_and_bounds(fake_conn: MagicMock, settings: Settings) -> None:
    _db_returns(fake_conn, [LOG, SPAN])
    result = retrieve_evidence(fake_conn, _query(trace_id=TRACE_ID), settings)
    sql, params = _select_sql(fake_conn)
    assert "evidence_items.trace_id = %(trace_id_1)s" in sql
    assert "evidence_items.service" not in sql.split("FROM", 1)[1]
    assert "evidence_items.occurred_at >= %(occurred_at_1)s" in sql
    assert "evidence_items.occurred_at <= %(occurred_at_2)s" in sql
    assert params["trace_id_1"] == TRACE_ID
    assert result.items == [LOG, SPAN]
    for got, expected in zip(result.items, [LOG, SPAN], strict=True):
        assert got.provenance == expected.provenance
        assert got.provenance.source.locator == expected.provenance.source.locator
    assert result.trace_id == TRACE_ID and result.service is None
    assert result.count == 2 and result.truncated is False


def test_service_window_lookup(fake_conn: MagicMock, settings: Settings) -> None:
    _db_returns(fake_conn, [DEPLOYMENT, LOG])
    query = _query(
        service="cre-lab-api",
        window_before=timedelta(seconds=300),
        window_after=timedelta(seconds=60),
    )
    result = retrieve_evidence(fake_conn, query, settings)
    sql, params = _select_sql(fake_conn)
    assert "evidence_items.service = %(service_1)s" in sql
    assert "evidence_items.trace_id =" not in sql
    assert params["service_1"] == "cre-lab-api"
    assert params["occurred_at_1"] == ANCHOR - timedelta(seconds=300)
    assert params["occurred_at_2"] == ANCHOR + timedelta(seconds=60)
    assert result.start == ANCHOR - timedelta(seconds=300)
    assert result.end == ANCHOR + timedelta(seconds=60)
    assert result.start.utcoffset() == timedelta(0)
    assert result.items == [DEPLOYMENT, LOG]


def test_trace_and_service_use_and(fake_conn: MagicMock, settings: Settings) -> None:
    _db_returns(fake_conn, [LOG])
    retrieve_evidence(fake_conn, _query(trace_id=TRACE_ID, service="cre-lab-api"), settings)
    sql, _ = _select_sql(fake_conn)
    assert "evidence_items.trace_id = %(trace_id_1)s AND evidence_items.service = " in sql


def test_empty_result(fake_conn: MagicMock, settings: Settings) -> None:
    _db_returns(fake_conn, [])
    result = retrieve_evidence(fake_conn, _query(service="cre-lab-api"), settings)
    assert result.items == []
    assert result.count == 0
    assert result.truncated is False
    assert result.start == ANCHOR - timedelta(seconds=900)
    assert result.end == ANCHOR + timedelta(seconds=900)
    assert result.limit == 200


def test_limit_truncation(fake_conn: MagicMock, settings: Settings) -> None:
    _db_returns(fake_conn, [DEPLOYMENT, LOG, SPAN])
    result = retrieve_evidence(fake_conn, _query(service="cre-lab-api", limit=2), settings)
    assert result.items == [DEPLOYMENT, LOG]
    assert result.count == 2 and result.truncated is True
    sql, params = _select_sql(fake_conn)
    assert "LIMIT %(param_1)s" in sql
    assert params["param_1"] == 3


def test_exact_limit_not_truncated(fake_conn: MagicMock, settings: Settings) -> None:
    _db_returns(fake_conn, [DEPLOYMENT, LOG])
    result = retrieve_evidence(fake_conn, _query(service="cre-lab-api", limit=2), settings)
    assert result.count == 2 and result.truncated is False


def test_ordering_ties_keep_db_order(fake_conn: MagicMock, settings: Settings) -> None:
    tied_span = SPAN.model_copy(update={"timestamp": LOG.timestamp})
    first, second = sorted([LOG, tied_span], key=lambda item: str(item.id), reverse=True)
    _db_returns(fake_conn, [first, second])
    result = retrieve_evidence(fake_conn, _query(trace_id=TRACE_ID), settings)
    sql, _ = _select_sql(fake_conn)
    assert _flat(sql).endswith(f"{ORDER_BY} LIMIT %(param_1)s")
    assert [item.id for item in result.items] == [first.id, second.id]


def test_build_select_order_by_is_last_before_limit() -> None:
    query = _query(service="svc")
    stmt = build_select(query, resolve_query(query, Settings()))
    sql = _flat(str(stmt.compile(dialect=postgresql.dialect())))
    assert sql.startswith("SELECT ")
    assert sql.endswith(f"{ORDER_BY} LIMIT %(param_1)s")
    assert stmt.is_select and not stmt.is_dml


def test_defaults_from_settings() -> None:
    settings = Settings(CRE_RETRIEVAL_DEFAULT_WINDOW_SECONDS=30, CRE_RETRIEVAL_DEFAULT_LIMIT=7)
    resolved = resolve_query(_query(service="svc"), settings)
    assert resolved.start == ANCHOR - timedelta(seconds=30)
    assert resolved.end == ANCHOR + timedelta(seconds=30)
    assert resolved.limit == 7


@pytest.mark.parametrize(
    "fields",
    [
        {"window_before": timedelta(seconds=-1)},
        {"window_after": timedelta(seconds=-1)},
        {"window_before": timedelta(seconds=86_401)},
        {"window_after": timedelta(seconds=86_401)},
        {"limit": 0},
        {"limit": 1001},
    ],
    ids=["neg_before", "neg_after", "big_before", "big_after", "limit_0", "limit_max_plus_1"],
)
def test_invalid_window_or_limit_raises_without_db(
    fake_conn: MagicMock, settings: Settings, fields: dict[str, Any]
) -> None:
    with pytest.raises(RetrievalQueryError):
        retrieve_evidence(fake_conn, _query(service="svc", **fields), settings)
    fake_conn.execute.assert_not_called()


def test_max_window_and_limit_are_allowed(settings: Settings) -> None:
    query = _query(service="svc", window_before=timedelta(seconds=86_400), limit=1000)
    assert resolve_query(query, settings).limit == 1000


def test_zero_window(settings: Settings) -> None:
    query = _query(service="svc", window_before=timedelta(0), window_after=timedelta(0))
    resolved = resolve_query(query, settings)
    assert resolved.start == resolved.end == ANCHOR


def test_overflow_raises(settings: Settings) -> None:
    near_max = datetime.max.replace(tzinfo=UTC) - timedelta(seconds=1)
    query = EvidenceQuery(anchor=near_max, service="svc", window_after=timedelta(seconds=60))
    with pytest.raises(RetrievalQueryError):
        resolve_query(query, settings)


@pytest.mark.parametrize(
    "fields",
    [
        {"anchor": ANCHOR},
        {"anchor": datetime(2026, 1, 1), "service": "svc"},
        {"anchor": ANCHOR, "trace_id": "not-a-trace"},
        {"anchor": ANCHOR, "trace_id": "0" * 32},
        {"anchor": ANCHOR, "service": ""},
        {"anchor": ANCHOR, "service": "s" * 201},
        {"service": "svc"},
        {"anchor": ANCHOR, "service": "svc", "extra": 1},
    ],
    ids=[
        "no_filter",
        "naive",
        "bad_trace",
        "zero_trace",
        "empty_service",
        "long",
        "no_anchor",
        "extra",
    ],
)
def test_evidence_query_validation(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        EvidenceQuery.model_validate(fields)


def test_non_utc_anchor_normalized(fake_conn: MagicMock, settings: Settings) -> None:
    _db_returns(fake_conn, [])
    anchor = datetime.fromisoformat("2026-01-01T02:00:05+02:00")
    result = retrieve_evidence(
        fake_conn, EvidenceQuery(anchor=anchor, service="svc", window_before=timedelta(0)), settings
    )
    assert result.anchor == ANCHOR
    assert result.anchor.utcoffset() == timedelta(0)
    assert result.start.utcoffset() == timedelta(0)
    assert result.end.utcoffset() == timedelta(0)
    assert result.start == ANCHOR


def test_read_only_and_statement_timeout_first(fake_conn: MagicMock, settings: Settings) -> None:
    _db_returns(fake_conn, [])
    retrieve_evidence(fake_conn, _query(service="svc"), settings)
    calls = fake_conn.execute.call_args_list
    assert len(calls) == 3
    assert str(calls[0].args[0]) == "SET TRANSACTION READ ONLY"
    assert "set_config('statement_timeout', :ms, true)" in str(calls[1].args[0])
    assert calls[1].args[1] == {"ms": "5000"}
    assert calls[2].args[0].is_select
    fake_conn.commit.assert_not_called()


def test_statement_timeout_from_settings(fake_conn: MagicMock) -> None:
    _db_returns(fake_conn, [])
    retrieve_evidence(
        fake_conn, _query(service="svc"), Settings(CRE_RETRIEVAL_STATEMENT_TIMEOUT_MS=250)
    )
    assert fake_conn.execute.call_args_list[1].args[1] == {"ms": "250"}


def test_db_failure_propagates(fake_conn: MagicMock, settings: Settings) -> None:
    fake_conn.execute.side_effect = [
        MagicMock(),
        MagicMock(),
        OperationalError("SELECT", {}, Exception("fake connection lost")),
    ]
    with pytest.raises(OperationalError):
        retrieve_evidence(fake_conn, _query(service="svc"), settings)


def test_kinds_filter_in_sql(fake_conn: MagicMock, settings: Settings) -> None:
    _db_returns(fake_conn, [DEPLOYMENT])
    query = _query(service="svc", kinds=frozenset({"deployment", "config_change"}))
    retrieve_evidence(fake_conn, query, settings)
    sql, params = _select_sql(fake_conn)
    assert "evidence_items.kind IN (__[POSTCOMPILE_kind_1])" in _flat(sql)
    assert params["kind_1"] == ["config_change", "deployment"]


def test_no_kinds_sql_unchanged(fake_conn: MagicMock, settings: Settings) -> None:
    _db_returns(fake_conn, [])
    query = _query(service="svc")
    assert query.kinds is None
    retrieve_evidence(fake_conn, query, settings)
    sql, params = _select_sql(fake_conn)
    assert "evidence_items.kind" not in sql.split("FROM", 1)[1]
    assert not any(name.startswith("kind") for name in params)


@pytest.mark.parametrize("kinds", [frozenset(), frozenset({"metric"})], ids=["empty", "unknown"])
def test_empty_kinds_rejected(kinds: frozenset[str]) -> None:
    with pytest.raises(ValidationError):
        _query(service="svc", kinds=kinds)
