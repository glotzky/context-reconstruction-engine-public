import threading
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from sqlalchemy.exc import DBAPIError

from cre.worker import CLAIM_SQL, MARK_DONE_SQL, MARK_FAILED_SQL, process_one, run
from tests.conftest import LogCapture


def _conn_with_row(row: tuple[int, str] | None) -> MagicMock:
    conn = MagicMock(name="connection")
    conn.execute.return_value.first.return_value = row
    return conn


def test_process_one_noop_marks_done(log_capture: LogCapture) -> None:
    conn = _conn_with_row((1, "noop"))
    assert process_one(conn) == 1
    assert conn.execute.call_args_list[0].args[0] is CLAIM_SQL
    update = conn.execute.call_args_list[1]
    assert update.args[0] is MARK_DONE_SQL
    assert "status = 'done'" in str(update.args[0])
    assert update.args[1] == {"job_id": 1}
    lines = [r for r in log_capture.records() if r["message"] == "job_processed"]
    assert lines[0]["job_id"] == 1
    assert lines[0]["kind"] == "noop"
    assert lines[0]["status"] == "done"


def test_process_one_unknown_kind_marks_failed() -> None:
    conn = _conn_with_row((5, "mystery"))
    assert process_one(conn) == 5
    update = conn.execute.call_args_list[1]
    assert update.args[0] is MARK_FAILED_SQL
    assert "status = 'failed'" in str(update.args[0])
    assert update.args[1] == {"job_id": 5, "last_error": "unknown job kind"}


def test_process_one_no_pending_returns_none() -> None:
    conn = _conn_with_row(None)
    assert process_one(conn) is None
    assert conn.execute.call_count == 1


def _engine_yielding(*conns: MagicMock) -> MagicMock:
    engine = MagicMock(name="engine")
    engine.begin.return_value.__enter__.side_effect = list(conns)
    return engine


def test_run_counts_jobs_and_waits_only_when_idle() -> None:
    engine = _engine_yielding(_conn_with_row((1, "noop")), _conn_with_row(None))
    stop = MagicMock(spec=threading.Event)
    stop.is_set.return_value = False
    assert run(engine, 0.5, stop, max_iterations=2) == 1
    stop.wait.assert_called_once_with(0.5)


def test_run_does_not_wait_when_busy() -> None:
    engine = _engine_yielding(_conn_with_row((1, "noop")), _conn_with_row((2, "noop")))
    stop = MagicMock(spec=threading.Event)
    stop.is_set.return_value = False
    assert run(engine, 0.5, stop, max_iterations=2) == 2
    stop.wait.assert_not_called()


def test_run_logs_db_error_and_continues(log_capture: LogCapture) -> None:
    engine = MagicMock(name="engine")
    engine.begin.return_value.__enter__.side_effect = [
        DBAPIError("SELECT 1", {}, Exception("connection refused")),
        _conn_with_row((3, "noop")),
    ]
    stop = MagicMock(spec=threading.Event)
    stop.is_set.return_value = False
    assert run(engine, 0.1, stop, max_iterations=2) == 1
    stop.wait.assert_called_once_with(0.1)
    failures = [r for r in log_capture.records() if r["message"] == "worker_iteration_failed"]
    assert len(failures) == 1
    assert failures[0]["level"] == "ERROR"
    assert failures[0]["error_type"] == "DBAPIError"


def test_run_returns_immediately_when_stopped() -> None:
    engine = MagicMock(name="engine")
    stop = threading.Event()
    stop.set()
    assert run(engine, 0.1, stop) == 0
    engine.begin.assert_not_called()


@pytest.mark.parametrize("poll", [0.01])
def test_run_idle_wait_is_interruptible(poll: float) -> None:
    engine = _engine_yielding(*[_conn_with_row(None) for _ in range(3)])
    stop = threading.Event()
    original_wait = stop.wait

    def _wait_then_stop(timeout: float | None = None) -> bool:
        stop.set()
        return original_wait(timeout)

    stop.wait = _wait_then_stop  # type: ignore[method-assign]
    assert run(engine, poll, stop) == 0
    assert engine.begin.call_count == 1


def test_run_counts_investigation_and_skips_noop_claim() -> None:
    engine = MagicMock(name="engine")
    process_investigation = MagicMock(return_value=UUID(int=1))
    stop = MagicMock(spec=threading.Event)
    stop.is_set.return_value = False
    assert (
        run(engine, 0.5, stop, max_iterations=2, process_investigation=process_investigation) == 2
    )
    assert process_investigation.call_count == 2
    process_investigation.assert_called_with(engine)
    engine.begin.assert_not_called()
    stop.wait.assert_not_called()


def test_run_falls_through_to_noop_when_no_investigation() -> None:
    engine = _engine_yielding(_conn_with_row((7, "noop")), _conn_with_row(None))
    process_investigation = MagicMock(return_value=None)
    stop = MagicMock(spec=threading.Event)
    stop.is_set.return_value = False
    assert (
        run(engine, 0.5, stop, max_iterations=2, process_investigation=process_investigation) == 1
    )
    assert process_investigation.call_count == 2
    assert engine.begin.call_count == 2
    stop.wait.assert_called_once_with(0.5)


def test_run_logs_db_error_from_investigation_and_waits(log_capture: LogCapture) -> None:
    engine = MagicMock(name="engine")
    process_investigation = MagicMock(
        side_effect=DBAPIError("SELECT 1", {}, Exception("connection refused"))
    )
    stop = MagicMock(spec=threading.Event)
    stop.is_set.return_value = False
    assert (
        run(engine, 0.2, stop, max_iterations=1, process_investigation=process_investigation) == 0
    )
    stop.wait.assert_called_once_with(0.2)
    engine.begin.assert_not_called()
    failures = [r for r in log_capture.records() if r["message"] == "worker_iteration_failed"]
    assert len(failures) == 1 and failures[0]["error_type"] == "DBAPIError"
