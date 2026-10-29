from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import DBAPIError

from cre import lab
from cre.evidence import ConfigChangeEvidence, DeploymentEvidence
from cre.evidence_repo import idempotency_key

HEALTHY_COLUMNS = ["customer_name"]
APPLIED_COLUMNS = ["customer_full_name"]


def _result(values: Sequence[str]) -> MagicMock:
    result = MagicMock(name="result")
    result.scalars.return_value.all.return_value = list(values)
    return result


def _probe(conn: MagicMock, columns: list[str], stale: Sequence[str] = ()) -> None:
    """Answer the schema-rename probe with ``columns`` and the stale-search-path probe with
    ``stale`` (healthy by default); every other statement gets an empty result."""

    def execute(statement: Any, *args: Any, **kwargs: Any) -> MagicMock:
        sql = str(statement)
        if "information_schema.columns" in sql:
            return _result(columns)
        if "pg_namespace" in sql:
            return _result(stale)
        return _result([])

    conn.execute.side_effect = execute


def _stale(conn: MagicMock, markers: Sequence[str]) -> None:
    _probe(conn, HEALTHY_COLUMNS, markers)


def _executed_sql(conn: MagicMock) -> list[str]:
    return [str(call.args[0]) for call in conn.execute.call_args_list]


def _alters(conn: MagicMock) -> list[str]:
    return [sql for sql in _executed_sql(conn) if sql.startswith("ALTER")]


def test_failures_registry_contains_both_failures() -> None:
    assert set(lab.FAILURES) == {"schema-rename", "stale-search-path"}
    with pytest.raises(TypeError):
        lab.FAILURES["other"] = lab.FAILURES["schema-rename"]  # type: ignore[index]


def test_apply_renames_column_when_healthy(fake_conn: MagicMock) -> None:
    _probe(fake_conn, ["customer_name"])
    assert lab.apply(fake_conn, "schema-rename") is True
    alters = _alters(fake_conn)
    assert len(alters) == 1
    assert "RENAME COLUMN customer_name TO customer_full_name" in alters[0]


def test_apply_is_noop_when_already_applied(fake_conn: MagicMock) -> None:
    _probe(fake_conn, ["customer_full_name"])
    assert lab.apply(fake_conn, "schema-rename") is False
    assert fake_conn.execute.call_count == 1
    assert "information_schema.columns" in _executed_sql(fake_conn)[0]


def test_revert_restores_column_when_applied(fake_conn: MagicMock) -> None:
    _probe(fake_conn, ["customer_full_name"])
    assert lab.revert(fake_conn, "schema-rename") is True
    alters = _alters(fake_conn)
    assert len(alters) == 1
    assert "RENAME COLUMN customer_full_name TO customer_name" in alters[0]


def test_revert_when_nothing_applied_is_noop(fake_conn: MagicMock) -> None:
    _probe(fake_conn, ["customer_name"])
    assert lab.revert(fake_conn, "schema-rename") is False
    assert _alters(fake_conn) == []


@pytest.mark.parametrize("name", ["nope", "", "Schema-Rename"])
def test_unknown_name_rejected_before_db_access(fake_conn: MagicMock, name: str) -> None:
    with pytest.raises(lab.UnknownFailureError, match="schema-rename"):
        lab.apply(fake_conn, name)
    with pytest.raises(lab.UnknownFailureError):
        lab.revert(fake_conn, name)
    fake_conn.execute.assert_not_called()


def test_get_failure_returns_registered_failure() -> None:
    assert lab.get_failure("schema-rename").name == "schema-rename"


@pytest.mark.parametrize("columns", [[], ["customer_name", "customer_full_name"]])
def test_unexpected_schema_raises_lab_state_error(fake_conn: MagicMock, columns: list[str]) -> None:
    _probe(fake_conn, columns)
    with pytest.raises(lab.LabStateError, match="alembic upgrade head"):
        lab.apply(fake_conn, "schema-rename")
    with pytest.raises(lab.LabStateError):
        lab.revert(fake_conn, "schema-rename")
    with pytest.raises(lab.LabStateError):
        lab.reset(fake_conn)
    assert _alters(fake_conn) == []


def test_reset_reverts_applied_failures(fake_conn: MagicMock) -> None:
    _probe(fake_conn, ["customer_full_name"])
    assert lab.reset(fake_conn) == ["schema-rename"]
    healthy = MagicMock(name="connection")
    _probe(healthy, ["customer_name"])
    assert lab.reset(healthy) == []
    assert _alters(healthy) == []


def _engine(conn: MagicMock) -> MagicMock:
    engine = MagicMock(name="engine")
    engine.begin.return_value.__enter__.return_value = conn
    return engine


def test_main_list_needs_no_engine(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lab, "create_db_engine", MagicMock(side_effect=AssertionError))
    assert lab.main(["list"]) == 0
    assert "schema-rename" in capsys.readouterr().out


def test_main_apply_unknown_name_exits_2_without_engine(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lab, "create_db_engine", MagicMock(side_effect=AssertionError))
    engine = MagicMock(name="engine")
    assert lab.main(["apply", "nope"], engine=engine) == 2
    assert engine.mock_calls == []
    assert "unknown failure" in capsys.readouterr().err


def test_main_bad_usage_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    engine = MagicMock(name="engine")
    assert lab.main([], engine=engine) == 2
    assert lab.main(["apply"], engine=engine) == 2
    assert engine.mock_calls == []


def test_main_apply_uses_injected_engine(
    fake_conn: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    _probe(fake_conn, ["customer_name"])
    engine = _engine(fake_conn)
    assert lab.main(["apply", "schema-rename"], engine=engine) == 0
    assert "applied schema-rename" in capsys.readouterr().out
    engine.begin.assert_called_once()
    fake_conn.commit.assert_not_called()


@pytest.mark.parametrize(
    ("argv", "columns", "expected"),
    [
        (["apply", "schema-rename"], ["customer_full_name"], "schema-rename already applied"),
        (["revert", "schema-rename"], ["customer_full_name"], "reverted schema-rename"),
        (["revert", "schema-rename"], ["customer_name"], "schema-rename not applied"),
        (["reset"], ["customer_full_name"], "reset: reverted ['schema-rename']"),
        (["reset"], ["customer_name"], "reset: reverted []"),
    ],
)
def test_main_outcome_lines(
    fake_conn: MagicMock,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    columns: list[str],
    expected: str,
) -> None:
    _probe(fake_conn, columns)
    assert lab.main(argv, engine=_engine(fake_conn)) == 0
    assert capsys.readouterr().out.strip() == expected


def test_main_lab_state_error_exits_1(
    fake_conn: MagicMock, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake:fake-password@localhost/fake")
    _probe(fake_conn, [])
    assert lab.main(["apply", "schema-rename"], engine=_engine(fake_conn)) == 1
    err = capsys.readouterr().err
    assert "LabStateError" in err
    assert "postgresql://" not in err


def test_main_dbapi_error_exits_1_without_url(
    fake_conn: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    from sqlalchemy.exc import OperationalError

    engine = MagicMock(name="engine")
    engine.begin.side_effect = OperationalError(
        "connect", {}, Exception("postgresql://fake:fake-password@localhost/fake")
    )
    assert lab.main(["reset"], engine=engine) == 1
    err = capsys.readouterr().err
    assert "OperationalError" in err
    assert "postgresql://" not in err


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    store = MagicMock(name="store_evidence")
    monkeypatch.setattr(lab, "store_evidence", store)
    return store


def test_stale_apply_when_healthy(fake_conn: MagicMock, stored: MagicMock) -> None:
    _stale(fake_conn, [])
    assert lab.apply(fake_conn, "stale-search-path") is True
    ddl = [sql for sql in _executed_sql(fake_conn) if not sql.startswith("SELECT")]
    assert ddl[0] == "CREATE SCHEMA lab_stale"
    assert ddl[1] == "CREATE TABLE lab_stale.orders AS SELECT * FROM public.orders"
    assert ddl[2] == (
        "ALTER TABLE lab_stale.orders RENAME COLUMN customer_name TO customer_full_name"
    )
    assert ddl[3].startswith("DO $$")
    assert "IN DATABASE %I SET search_path TO lab_stale, public" in ddl[3]
    assert "current_database()" in ddl[3]
    assert len(ddl) == 4
    stored.assert_called_once()
    conn_arg, item = stored.call_args.args
    assert conn_arg is fake_conn
    assert isinstance(item, ConfigChangeEvidence)
    assert item.observed.key == "search_path"
    assert item.observed.old_value == "public"
    assert item.observed.new_value == "lab_stale, public"
    assert item.observed.changed_by == "cre-lab"


def test_stale_apply_noop_when_applied(fake_conn: MagicMock, stored: MagicMock) -> None:
    _stale(fake_conn, ["schema", "setting"])
    assert lab.apply(fake_conn, "stale-search-path") is False
    assert fake_conn.execute.call_count == 1
    assert "pg_namespace" in _executed_sql(fake_conn)[0]
    stored.assert_not_called()


def _stale_revert_ddl(conn: MagicMock) -> list[str]:
    return [sql for sql in _executed_sql(conn) if not sql.startswith("SELECT")]


def test_stale_revert_when_applied(fake_conn: MagicMock, stored: MagicMock) -> None:
    _stale(fake_conn, ["schema", "setting"])
    assert lab.revert(fake_conn, "stale-search-path") is True
    ddl = _stale_revert_ddl(fake_conn)
    assert len(ddl) == 2
    assert ddl[0].startswith("DO $$")
    assert "IN DATABASE %I RESET search_path" in ddl[0]
    assert ddl[1] == "DROP SCHEMA IF EXISTS lab_stale CASCADE"
    stored.assert_called_once()


@pytest.mark.parametrize("markers", [["schema"], ["setting"]])
def test_stale_revert_partial_state_cleans_up(
    fake_conn: MagicMock, stored: MagicMock, markers: list[str]
) -> None:
    _stale(fake_conn, markers)
    assert lab.revert(fake_conn, "stale-search-path") is True
    ddl = _stale_revert_ddl(fake_conn)
    assert len(ddl) == 2
    assert "RESET search_path" in ddl[0]
    assert ddl[1] == "DROP SCHEMA IF EXISTS lab_stale CASCADE"


def test_stale_revert_noop_when_healthy(fake_conn: MagicMock) -> None:
    _stale(fake_conn, [])
    assert lab.revert(fake_conn, "stale-search-path") is False
    assert _stale_revert_ddl(fake_conn) == []


def test_stale_probe_unknown_marker_raises(fake_conn: MagicMock, stored: MagicMock) -> None:
    _stale(fake_conn, ["bogus"])
    with pytest.raises(lab.LabStateError):
        lab.apply(fake_conn, "stale-search-path")
    with pytest.raises(lab.LabStateError):
        lab.revert(fake_conn, "stale-search-path")
    with pytest.raises(lab.LabStateError):
        lab.reset(fake_conn)
    assert _stale_revert_ddl(fake_conn) == []
    stored.assert_not_called()


def test_apply_refuses_when_other_failure_applied(fake_conn: MagicMock, stored: MagicMock) -> None:
    fake_conn.execute.side_effect = [_result([]), _result(APPLIED_COLUMNS)]
    with pytest.raises(lab.LabStateError, match="schema-rename is applied; run reset first"):
        lab.apply(fake_conn, "stale-search-path")
    executed = _executed_sql(fake_conn)
    assert len(executed) == 2
    assert not any(sql.startswith(("CREATE", "ALTER", "DO")) for sql in executed)
    stored.assert_not_called()


def test_apply_schema_rename_refuses_when_stale_applied(fake_conn: MagicMock) -> None:
    _probe(fake_conn, HEALTHY_COLUMNS, ["schema", "setting"])
    with pytest.raises(lab.LabStateError, match="stale-search-path is applied"):
        lab.apply(fake_conn, "schema-rename")
    assert _alters(fake_conn) == []


def test_schema_rename_targets_public_schema(fake_conn: MagicMock) -> None:
    assert "table_schema = 'public'" in str(lab._ORDERS_COLUMNS_PROBE)
    failure = lab.get_failure("schema-rename")
    statements = [str(sql) for sql in failure.apply_sql + failure.revert_sql]
    assert len(statements) == 2
    assert all(sql.startswith("ALTER TABLE public.orders ") for sql in statements)


def test_build_search_path_change_fields() -> None:
    changed_at = datetime(2026, 2, 21, 9, 19, tzinfo=UTC)
    item = lab.build_search_path_change(changed_at)
    assert item.timestamp == changed_at
    assert item.provenance.source.source_type == "config_change"
    assert item.provenance.source.name == "cre.lab"
    assert (item.provenance.source.locator or "").startswith("stale-search-path")
    assert item.provenance.retrieval_method == "lab_trigger"
    assert item.provenance.retrieved_at == changed_at
    assert item.observed.key == lab.SEARCH_PATH_KEY
    assert item.observed.new_value == lab.STALE_SEARCH_PATH


def test_build_search_path_change_rejects_naive_timestamp() -> None:
    with pytest.raises(lab.NormalizationError):
        lab.build_search_path_change(datetime(2026, 2, 21, 9, 19))


def test_main_normalization_error_exits_1(
    fake_conn: MagicMock, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _stale(fake_conn, [])
    monkeypatch.setattr(
        lab, "build_search_path_change", MagicMock(side_effect=lab.NormalizationError("bad"))
    )
    assert lab.main(["apply", "stale-search-path"], engine=_engine(fake_conn)) == 1
    assert "error: NormalizationError: bad" in capsys.readouterr().err


def test_stale_apply_store_failure_propagates(
    fake_conn: MagicMock, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _stale(fake_conn, [])
    error = DBAPIError("INSERT", {}, Exception("fake failure"))
    monkeypatch.setattr(lab, "store_evidence", MagicMock(side_effect=error))
    with pytest.raises(DBAPIError):
        lab.apply(fake_conn, "stale-search-path")
    assert lab.main(["apply", "stale-search-path"], engine=_engine(fake_conn)) == 1
    assert "database operation failed" in capsys.readouterr().err


def test_reset_reverts_in_registry_order(fake_conn: MagicMock) -> None:
    _probe(fake_conn, APPLIED_COLUMNS, ["schema", "setting"])
    assert lab.reset(fake_conn) == ["schema-rename", "stale-search-path"]


def test_main_list_shows_both(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lab, "create_db_engine", MagicMock(side_effect=AssertionError))
    assert lab.main(["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split(":")[0] for line in lines] == ["schema-rename", "stale-search-path"]


def test_apply_schema_rename_records_deployment_event(
    fake_conn: MagicMock, stored: MagicMock
) -> None:
    _probe(fake_conn, HEALTHY_COLUMNS)
    assert lab.apply(fake_conn, "schema-rename") is True
    stored.assert_called_once()
    conn_arg, item = stored.call_args.args
    assert conn_arg is fake_conn
    assert isinstance(item, DeploymentEvidence)
    assert item.kind == "deployment"
    assert item.observed.status == "applied"
    assert item.observed.environment == "lab"
    assert item.observed.actor == "cre-lab"
    assert item.observed.attributes["change_type"] == "schema_migration"
    assert item.observed.attributes["applied_by"] == "cre.lab"
    assert item.observed.attributes["ddl"] == lab.SCHEMA_RENAME_APPLY_SQL
    assert item.provenance.retrieval_method == "lab_trigger"
    assert item.provenance.source.name == "cre.lab"
    assert item.provenance.source.source_type == "deployment_event"
    assert item.provenance.identifiers["deployment_id"].startswith("lab:schema-rename:apply:")


def test_revert_schema_rename_records_reverted_event(
    fake_conn: MagicMock, stored: MagicMock
) -> None:
    _probe(fake_conn, APPLIED_COLUMNS)
    assert lab.revert(fake_conn, "schema-rename") is True
    stored.assert_called_once()
    _, item = stored.call_args.args
    assert isinstance(item, DeploymentEvidence)
    assert item.observed.status == "reverted"
    assert item.observed.attributes["ddl"] == lab.SCHEMA_RENAME_REVERT_SQL
    assert item.provenance.retrieval_method == "lab_reset"
    assert item.provenance.source.name == "cre.lab"
    assert item.provenance.identifiers["deployment_id"].startswith("lab:schema-rename:revert:")


def test_revert_stale_search_path_records_reverse_config_change(
    fake_conn: MagicMock, stored: MagicMock
) -> None:
    _stale(fake_conn, ["schema", "setting"])
    assert lab.revert(fake_conn, "stale-search-path") is True
    stored.assert_called_once()
    conn_arg, item = stored.call_args.args
    assert conn_arg is fake_conn
    assert isinstance(item, ConfigChangeEvidence)
    assert item.observed.key == lab.SEARCH_PATH_KEY
    assert item.observed.old_value == "lab_stale, public"
    assert item.observed.new_value == "public"
    assert item.provenance.retrieval_method == "lab_reset"
    assert item.provenance.source.name == "cre.lab"


def test_reset_records_event_only_for_reverted_failure(
    fake_conn: MagicMock, stored: MagicMock
) -> None:
    _probe(fake_conn, HEALTHY_COLUMNS, ["schema", "setting"])
    assert lab.reset(fake_conn) == ["stale-search-path"]
    stored.assert_called_once()
    _, item = stored.call_args.args
    assert isinstance(item, ConfigChangeEvidence)
    assert item.provenance.retrieval_method == "lab_reset"


def test_noop_apply_revert_reset_record_nothing(fake_conn: MagicMock, stored: MagicMock) -> None:
    _probe(fake_conn, APPLIED_COLUMNS)
    assert lab.apply(fake_conn, "schema-rename") is False
    _stale(fake_conn, ["schema", "setting"])
    assert lab.apply(fake_conn, "stale-search-path") is False
    _probe(fake_conn, HEALTHY_COLUMNS)
    assert lab.revert(fake_conn, "schema-rename") is False
    assert lab.revert(fake_conn, "stale-search-path") is False
    assert lab.reset(fake_conn) == []
    stored.assert_not_called()


@pytest.mark.parametrize(
    "build", [lab.build_schema_rename_event, lab.build_search_path_change], ids=["rename", "stale"]
)
def test_trigger_and_reset_events_have_distinct_idempotency_keys(build: Any) -> None:
    triggered = build(datetime(2026, 2, 21, 9, 19, tzinfo=UTC))
    reset = build(datetime(2026, 2, 21, 9, 25, tzinfo=UTC), reverted=True)
    retriggered = build(datetime(2026, 2, 21, 9, 30, tzinfo=UTC))
    keys = {idempotency_key(item) for item in (triggered, reset, retriggered)}
    assert len(keys) == 3
    assert (
        triggered.provenance.identifiers["content_sha256"]
        != reset.provenance.identifiers["content_sha256"]
    )


def test_schema_rename_event_does_not_claim_alembic() -> None:
    for reverted in (False, True):
        item = lab.build_schema_rename_event(
            datetime(2026, 2, 21, 9, 19, tzinfo=UTC), reverted=reverted
        )
        assert "alembic" not in item.observed.model_dump_json().lower()


def test_build_schema_rename_event_rejects_naive_timestamp() -> None:
    with pytest.raises(lab.NormalizationError):
        lab.build_schema_rename_event(datetime(2026, 2, 21, 9, 19))


def test_main_reset_normalization_error_exits_1(
    fake_conn: MagicMock, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _probe(fake_conn, APPLIED_COLUMNS)
    monkeypatch.setattr(
        lab, "build_schema_rename_event", MagicMock(side_effect=lab.NormalizationError("bad"))
    )
    assert lab.main(["reset"], engine=_engine(fake_conn)) == 1
    assert "error: NormalizationError: bad" in capsys.readouterr().err
