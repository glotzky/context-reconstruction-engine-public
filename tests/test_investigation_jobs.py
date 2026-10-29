from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from cre.investigation_jobs import (
    CLAIM_JOB_SQL,
    FAIL_CLAIMED_SQL,
    RENEW_LEASE_SQL,
    ClaimedJob,
    InvestigationRequest,
    LeaseLostError,
    claim_job,
    complete_job,
    fail_job,
    get_job,
    record_step,
    submit_job,
)
from tests.conftest import LogCapture
from tests.evidence_samples import TRACE_ID

JOB_ID = UUID(int=77)
CREATED = datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)
TIMESTAMP = "2026-01-01T00:00:05Z"


def _compiled(stmt: Any) -> Any:
    return stmt.compile(dialect=postgresql.dialect())


def test_request_valid_with_timestamp_and_trace_id() -> None:
    request = InvestigationRequest.model_validate({"timestamp": TIMESTAMP, "trace_id": TRACE_ID})
    assert request.trace_id == TRACE_ID
    assert request.timestamp.tzinfo is not None
    assert request.service is None and request.symptom is None and request.request_id is None


def test_request_valid_with_service_only() -> None:
    request = InvestigationRequest.model_validate({"timestamp": TIMESTAMP, "service": "fake-svc"})
    assert request.service == "fake-svc"


@pytest.mark.parametrize(
    "payload",
    [
        {"timestamp": "2026-01-01T00:00:05", "trace_id": TRACE_ID},
        {"trace_id": TRACE_ID},
        {"timestamp": TIMESTAMP},
        {"timestamp": TIMESTAMP, "trace_id": "not-a-trace"},
        {"timestamp": TIMESTAMP, "trace_id": "0" * 32},
        {"timestamp": TIMESTAMP, "trace_id": TRACE_ID, "extra": "x"},
        {"timestamp": TIMESTAMP, "trace_id": TRACE_ID, "symptom": "x" * 2001},
        {"timestamp": TIMESTAMP, "service": "s" * 201},
        {"timestamp": TIMESTAMP, "trace_id": TRACE_ID, "request_id": ""},
    ],
)
def test_request_rejects_invalid(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        InvestigationRequest.model_validate(payload)


def test_submit_job_stores_redacted_pending_request() -> None:
    conn = MagicMock(name="connection")
    request = InvestigationRequest.model_validate(
        {
            "timestamp": TIMESTAMP,
            "trace_id": TRACE_ID,
            "symptom": "login failed password=fake-pw-123",
        }
    )
    job_id = submit_job(conn, request)
    assert isinstance(job_id, UUID)
    params = _compiled(conn.execute.call_args.args[0]).params
    assert params["id"] == job_id
    assert params["status"] == "pending"
    assert "fake-pw-123" not in params["request"]["symptom"]
    assert "[REDACTED]" in params["request"]["symptom"]
    assert params["request"]["trace_id"] == TRACE_ID
    conn.commit.assert_not_called()


@pytest.mark.parametrize(
    "fields",
    [
        {"trace_id": TRACE_ID, "symptom": "pwd=a " * 333},
        {"service": "token=x," * 25},
    ],
)
def test_submit_job_rejects_request_invalid_after_redaction(fields: dict[str, Any]) -> None:
    conn = MagicMock(name="connection")
    request = InvestigationRequest.model_validate({"timestamp": TIMESTAMP, **fields})
    with pytest.raises(ValidationError):
        submit_job(conn, request)
    conn.execute.assert_not_called()


def test_claim_sql_uses_skip_locked_and_lease() -> None:
    sql = str(CLAIM_JOB_SQL)
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "lease_expires_at < now()" in sql
    assert "attempts = attempts + 1" in sql
    assert "locked_by = :worker_id" in str(RENEW_LEASE_SQL)


def test_claim_job_returns_claimed_job() -> None:
    conn = MagicMock(name="connection")
    conn.execute.return_value.first.return_value = (
        JOB_ID,
        {"timestamp": TIMESTAMP, "trace_id": TRACE_ID},
        2,
    )
    claimed = claim_job(conn, "worker-a", 30)
    assert claimed == ClaimedJob(
        id=JOB_ID,
        request=InvestigationRequest.model_validate({"timestamp": TIMESTAMP, "trace_id": TRACE_ID}),
        attempts=2,
    )
    assert conn.execute.call_args.args[0] is CLAIM_JOB_SQL
    assert conn.execute.call_args.args[1] == {"worker_id": "worker-a", "lease_seconds": 30}


def test_claim_job_fails_invalid_row_and_claims_next(log_capture: LogCapture) -> None:
    conn = MagicMock(name="connection")
    bad_id = UUID(int=78)
    conn.execute.return_value.first.side_effect = [
        (bad_id, {"timestamp": TIMESTAMP, "symptom": "fake-secret-symptom " * 200}, 1),
        (JOB_ID, {"timestamp": TIMESTAMP, "trace_id": TRACE_ID}, 1),
    ]
    claimed = claim_job(conn, "worker-a", 30)
    assert claimed is not None and claimed.id == JOB_ID
    calls = conn.execute.call_args_list
    assert [c.args[0] for c in calls] == [CLAIM_JOB_SQL, FAIL_CLAIMED_SQL, CLAIM_JOB_SQL]
    assert calls[1].args[1] == {"job_id": bad_id, "worker_id": "worker-a"}
    sql = str(FAIL_CLAIMED_SQL)
    assert "status = 'failed'" in sql and "error = 'internal_error'" in sql
    assert "locked_by = :worker_id" in sql
    records = [
        r for r in log_capture.records() if r.get("event") == "investigation_invalid_request"
    ]
    assert len(records) == 1 and records[0]["job_id"] == str(bad_id)
    assert "fake-secret-symptom" not in log_capture.stream.getvalue()


def test_claim_job_invalid_only_row_returns_none() -> None:
    conn = MagicMock(name="connection")
    conn.execute.return_value.first.side_effect = [(JOB_ID, {"timestamp": "not-a-time"}, 1), None]
    assert claim_job(conn, "worker-a", 30) is None
    assert conn.execute.call_args_list[1].args[0] is FAIL_CLAIMED_SQL


def test_claim_job_returns_none_without_row() -> None:
    conn = MagicMock(name="connection")
    conn.execute.return_value.first.return_value = None
    assert claim_job(conn, "worker-a", 30) is None


def _record(conn: MagicMock) -> bool:
    return record_step(
        conn,
        JOB_ID,
        "worker-a",
        30,
        step_key="collect:trace",
        seq=0,
        kind="collect",
        status="completed",
        payload={"query": "trace"},
    )


def _conn_for_step(renewed: Any, inserted: Any) -> MagicMock:
    conn = MagicMock(name="connection")
    renew = MagicMock(name="renew")
    renew.first.return_value = renewed
    insert = MagicMock(name="insert")
    insert.scalar_one_or_none.return_value = inserted
    conn.execute.side_effect = [renew, insert]
    return conn


def test_record_step_inserts_after_renewing_lease() -> None:
    conn = _conn_for_step((JOB_ID,), 1)
    assert _record(conn) is True
    first, second = conn.execute.call_args_list
    assert first.args[0] is RENEW_LEASE_SQL
    assert first.args[1] == {"job_id": JOB_ID, "worker_id": "worker-a", "lease_seconds": 30}
    params = _compiled(second.args[0]).params
    assert params["step_key"] == "collect:trace" and params["payload"] == {"query": "trace"}


def test_record_step_conflict_returns_false() -> None:
    conn = _conn_for_step((JOB_ID,), None)
    assert _record(conn) is False
    insert_sql = str(_compiled(conn.execute.call_args_list[1].args[0]))
    assert "ON CONFLICT (job_id, step_key) DO NOTHING" in insert_sql


def test_record_step_lease_lost_raises_without_insert() -> None:
    conn = _conn_for_step(None, 1)
    with pytest.raises(LeaseLostError):
        _record(conn)
    assert conn.execute.call_count == 1


@pytest.mark.parametrize(
    "finish",
    [
        lambda conn: complete_job(conn, JOB_ID, "worker-a", {"stop_reason": "concluded"}),
        lambda conn: fail_job(conn, JOB_ID, "worker-a", "internal_error"),
    ],
)
def test_finish_raises_when_lease_lost(finish: Any) -> None:
    conn = MagicMock(name="connection")
    conn.execute.return_value.rowcount = 0
    with pytest.raises(LeaseLostError):
        finish(conn)


def test_complete_job_sets_result_and_releases_lease() -> None:
    conn = MagicMock(name="connection")
    conn.execute.return_value.rowcount = 1
    complete_job(conn, JOB_ID, "worker-a", {"stop_reason": "concluded"})
    compiled = _compiled(conn.execute.call_args.args[0])
    sql = str(compiled)
    assert "investigation_jobs.locked_by =" in sql and "investigation_jobs.status =" in sql
    params = compiled.params
    assert params["status"] == "completed"
    assert params["result"] == {"stop_reason": "concluded"}
    assert params["locked_by"] is None and params["lease_expires_at"] is None
    assert params["locked_by_1"] == "worker-a" and params["status_1"] == "running"


def test_fail_job_sets_error_code() -> None:
    conn = MagicMock(name="connection")
    conn.execute.return_value.rowcount = 1
    fail_job(conn, JOB_ID, "worker-a", "no_initial_evidence")
    params = _compiled(conn.execute.call_args.args[0]).params
    assert params["status"] == "failed" and params["error"] == "no_initial_evidence"


def test_get_job_missing_returns_none() -> None:
    conn = MagicMock(name="connection")
    conn.execute.return_value.mappings.return_value.first.return_value = None
    assert get_job(conn, JOB_ID) is None


def _step_row(key: str, seq: int) -> dict[str, Any]:
    return {
        "step_key": key,
        "seq": seq,
        "kind": "checkpoint",
        "status": "completed",
        "payload": {"k": key},
        "created_at": CREATED,
    }


def test_get_job_returns_view_with_steps_ordered_by_seq() -> None:
    conn = MagicMock(name="connection")
    job = MagicMock(name="job_result")
    job.mappings.return_value.first.return_value = {
        "id": JOB_ID,
        "status": "running",
        "attempts": 1,
        "request": {"timestamp": TIMESTAMP, "trace_id": TRACE_ID},
        "error": None,
        "result": None,
        "created_at": CREATED,
        "updated_at": CREATED,
    }
    steps = MagicMock(name="steps_result")
    steps.mappings.return_value.all.return_value = [
        _step_row("hypotheses:initial", 2),
        _step_row("round:1", 3),
    ]
    conn.execute.side_effect = [job, steps]
    view = get_job(conn, JOB_ID)
    assert view is not None
    assert view.id == JOB_ID and view.status == "running" and view.attempts == 1
    assert [s.step_key for s in view.steps] == ["hypotheses:initial", "round:1"]
    steps_sql = str(_compiled(conn.execute.call_args_list[1].args[0]))
    assert "ORDER BY investigation_steps.seq, investigation_steps.id" in steps_sql
