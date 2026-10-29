import json
import threading
from collections import Counter
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from cre import evidence_repo, investigation_jobs
from cre.config import Settings
from cre.evidence import Evidence
from cre.investigation_jobs import (
    ClaimedJob,
    InvestigationRequest,
    JobError,
    LeaseLostError,
    StepView,
)
from cre.investigation_worker import process_investigation
from cre.llm import FakeProvider, LLMRequest
from cre.prompts import HYPOTHESIS_SYSTEM_PROMPT
from cre.retrieval import EvidenceQuery
from cre.worker import run
from tests.conftest import LogCapture
from tests.evidence_samples import TRACE_ID
from tests.test_bundle import DEPLOYMENT, LOG
from tests.test_investigator import (
    ANCHOR,
    CONCLUDE,
    INITIAL,
    NEW,
    TRACE_CALL,
    FakeExecutor,
    _revision,
    _tools,
)

SETTINGS = Settings()
CREATED = datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)
JOB_ID = UUID(int=900)


class Crash(BaseException):
    """Simulates the worker process dying mid-run."""


class FakeStore:
    """In-memory store emulating leases, attempts and unique step keys."""

    def __init__(self, request: InvestigationRequest, *, attempts: int = 0) -> None:
        self.now = 0.0
        self.job: dict[str, Any] = {
            "id": JOB_ID,
            "status": "pending",
            "request": request.model_dump(mode="json"),
            "attempts": attempts,
            "locked_by": None,
            "lease_expires_at": None,
            "result": None,
            "error": None,
        }
        self.steps: list[dict[str, Any]] = []
        self.evidence: dict[UUID, Evidence] = {e.id: e for e in (LOG, DEPLOYMENT, NEW)}
        self.record_results: list[bool] = []
        self.completions = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(investigation_jobs, "claim_job", self.claim_job)
        monkeypatch.setattr(investigation_jobs, "load_steps", self.load_steps)
        monkeypatch.setattr(investigation_jobs, "record_step", self.record_step)
        monkeypatch.setattr(investigation_jobs, "complete_job", self.complete_job)
        monkeypatch.setattr(investigation_jobs, "fail_job", self.fail_job)
        monkeypatch.setattr(evidence_repo, "load_evidence", self.load_evidence)

    def expire_lease(self) -> None:
        self.now = (self.job["lease_expires_at"] or 0.0) + 1

    def claim_job(self, _conn: Any, worker_id: str, lease_seconds: float) -> ClaimedJob | None:
        job = self.job
        expired = job["status"] == "running" and job["lease_expires_at"] < self.now
        if job["status"] != "pending" and not expired:
            return None
        job.update(
            status="running",
            locked_by=worker_id,
            attempts=job["attempts"] + 1,
            lease_expires_at=self.now + lease_seconds,
        )
        return ClaimedJob(
            id=job["id"],
            request=InvestigationRequest.model_validate(job["request"]),
            attempts=job["attempts"],
        )

    def _guard(self, worker_id: str) -> None:
        if self.job["locked_by"] != worker_id or self.job["status"] != "running":
            raise LeaseLostError

    def load_steps(self, _conn: Any, _job_id: UUID) -> list[StepView]:
        return [StepView(**step) for step in sorted(self.steps, key=lambda s: s["seq"])]

    def record_step(
        self,
        _conn: Any,
        _job_id: UUID,
        worker_id: str,
        lease_seconds: float,
        *,
        step_key: str,
        seq: int,
        kind: str,
        status: str,
        payload: dict[str, Any],
    ) -> bool:
        self._guard(worker_id)
        self.job["lease_expires_at"] = self.now + lease_seconds
        if any(step["step_key"] == step_key for step in self.steps):
            self.record_results.append(False)
            return False
        self.steps.append(
            {
                "step_key": step_key,
                "seq": seq,
                "kind": kind,
                "status": status,
                "payload": json.loads(json.dumps(payload)),
                "created_at": CREATED,
            }
        )
        self.record_results.append(True)
        return True

    def complete_job(self, _conn: Any, _job_id: UUID, worker_id: str, result: Any) -> None:
        self._guard(worker_id)
        self.completions += 1
        self.job.update(status="completed", result=json.loads(json.dumps(result)), locked_by=None)

    def fail_job(self, _conn: Any, _job_id: UUID, worker_id: str, error: JobError) -> None:
        self._guard(worker_id)
        self.job.update(status="failed", error=error, locked_by=None)

    def load_evidence(self, _conn: Any, evidence_id: UUID) -> Evidence | None:
        return self.evidence.get(evidence_id)

    def step(self, key: str) -> dict[str, Any]:
        return next(step for step in self.steps if step["step_key"] == key)


def _is_collect(query: EvidenceQuery) -> bool:
    return query.window_before is None


def _default_items(query: EvidenceQuery) -> list[Evidence]:
    if _is_collect(query):
        return [LOG] if query.trace_id is not None else [DEPLOYMENT]
    return [NEW]


def _request(**fields: Any) -> InvestigationRequest:
    return InvestigationRequest.model_validate(
        {"timestamp": ANCHOR.isoformat(), "trace_id": TRACE_ID, **fields}
    )


def _process(
    provider: FakeProvider, executor: FakeExecutor, worker_id: str = "worker-a"
) -> UUID | None:
    return process_investigation(
        MagicMock(name="engine"), SETTINGS, provider, worker_id, executor=executor
    )


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStore:
    fake = FakeStore(_request())
    fake.install(monkeypatch)
    return fake


def test_happy_path_completes_with_checkpoints(store: FakeStore) -> None:
    provider = FakeProvider([INITIAL, _tools(TRACE_CALL), _revision(), CONCLUDE])
    assert _process(provider, FakeExecutor(_default_items)) == JOB_ID
    assert store.job["status"] == "completed" and store.job["locked_by"] is None
    assert [s["step_key"] for s in store.steps] == [
        "collect:trace",
        "hypotheses:initial",
        "round:1",
    ]
    assert [s["seq"] for s in store.steps] == [0, 2, 3]
    collect = store.step("collect:trace")
    assert collect["status"] == "completed" and collect["kind"] == "collect"
    assert collect["payload"]["evidence_ids"] == [str(LOG.id)]
    assert collect["payload"]["limit"] == SETTINGS.investigation_per_query_limit
    result = store.job["result"]
    assert result["stop_reason"] == "concluded"
    assert result["evidence_ids"] == [str(LOG.id), str(NEW.id)]
    round_cp = store.step("round:1")["payload"]
    assert round_cp["evidence_ids"] == [str(LOG.id), str(NEW.id)]
    assert len(round_cp["rounds"]) == 1


def test_crash_resumes_without_repeating_steps(store: FakeStore) -> None:
    responses = [INITIAL, _tools(TRACE_CALL), _revision()]

    def crashing(_request: LLMRequest) -> str:
        if responses:
            return responses.pop(0)
        raise Crash

    executor = FakeExecutor(_default_items)
    with pytest.raises(Crash):
        _process(FakeProvider(crashing), executor, "worker-a")
    assert store.job["status"] == "running"
    assert [s["step_key"] for s in store.steps][-1] == "round:1"
    checkpoint_tokens = store.step("round:1")["payload"]["approx_tokens_used"]
    queries_before = len(executor.queries)

    assert _process(FakeProvider([CONCLUDE]), executor, "worker-b") is None
    store.expire_lease()
    resumed = FakeProvider([CONCLUDE])
    assert _process(resumed, executor, "worker-b") == JOB_ID

    assert store.job["status"] == "completed" and store.job["attempts"] == 2
    assert len(resumed.requests) == 1
    assert resumed.requests[0].system != HYPOTHESIS_SYSTEM_PROMPT
    assert len(executor.queries) == queries_before
    keys = Counter(s["step_key"] for s in store.steps)
    assert all(count == 1 for count in keys.values())
    result = store.job["result"]
    assert result["iterations_used"] == 2
    assert result["approx_tokens_used"] > checkpoint_tokens
    assert [r["iteration"] for r in result["rounds"]] == [1, 2]


def test_retried_step_write_is_noop(store: FakeStore, monkeypatch: pytest.MonkeyPatch) -> None:
    original = store.record_step

    def twice(*args: Any, **kwargs: Any) -> bool:
        first = original(*args, **kwargs)
        assert original(*args, **kwargs) is False
        return first

    monkeypatch.setattr(investigation_jobs, "record_step", twice)
    provider = FakeProvider([INITIAL, _tools(TRACE_CALL), _revision(), CONCLUDE])
    assert _process(provider, FakeExecutor(_default_items)) == JOB_ID
    assert store.job["status"] == "completed"
    assert store.record_results == [True, False] * 3
    assert len(store.steps) == 3


def test_collector_failure_recorded_as_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore(_request(service="cre-lab-api"))
    store.install(monkeypatch)

    def items(query: EvidenceQuery) -> list[Evidence]:
        if query.trace_id is not None:
            raise TimeoutError("fake secret detail")
        return [LOG]

    provider = FakeProvider([INITIAL, CONCLUDE])
    assert _process(provider, FakeExecutor(items)) == JOB_ID
    trace = store.step("collect:trace")
    assert trace["status"] == "failed"
    assert trace["payload"]["gap"] == {"kind": "collector_failed", "error_type": "TimeoutError"}
    assert trace["payload"]["evidence_ids"] == []
    assert "fake secret detail" not in json.dumps(store.steps, default=str)
    service = store.step("collect:service")
    assert service["status"] == "completed" and service["seq"] == 1
    assert store.job["status"] == "completed"
    assert store.job["result"]["stop_reason"] == "concluded"


def test_all_collectors_fail_without_provider_call(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore(_request(service="cre-lab-api"))
    store.install(monkeypatch)
    provider = FakeProvider([INITIAL])
    assert _process(provider, FakeExecutor(RuntimeError("down"))) == JOB_ID
    assert store.job["status"] == "failed" and store.job["error"] == "no_initial_evidence"
    assert provider.requests == []
    assert {s["status"] for s in store.steps} == {"failed"}


def test_missing_evidence_ids_are_skipped(store: FakeStore) -> None:
    store.evidence.pop(LOG.id)
    provider = FakeProvider([INITIAL])
    assert _process(provider, FakeExecutor(_default_items)) == JOB_ID
    assert store.job["error"] == "no_initial_evidence"
    assert provider.requests == []


def test_max_attempts_exceeded_after_reclaim(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore(_request(), attempts=SETTINGS.investigation_job_max_attempts)
    store.job.update(status="running", locked_by="dead-worker", lease_expires_at=-1.0)
    store.install(monkeypatch)
    provider = FakeProvider([INITIAL])
    executor = FakeExecutor(_default_items)
    assert _process(provider, executor) == JOB_ID
    assert store.job["status"] == "failed" and store.job["error"] == "max_attempts_exceeded"
    assert store.job["attempts"] == SETTINGS.investigation_job_max_attempts + 1
    assert provider.requests == [] and executor.queries == [] and store.steps == []


def test_lease_lost_stops_without_completion(store: FakeStore, log_capture: LogCapture) -> None:
    def items(query: EvidenceQuery) -> list[Evidence]:
        store.job["locked_by"] = "worker-other"
        return _default_items(query)

    provider = FakeProvider([INITIAL])
    assert _process(provider, FakeExecutor(items)) == JOB_ID
    assert store.completions == 0 and store.steps == []
    assert store.job["status"] == "running" and store.job["locked_by"] == "worker-other"
    assert provider.requests == []
    events = [r["event"] for r in log_capture.records() if "event" in r]
    assert "investigation_lease_lost" in events


def test_unexpected_exception_fails_job_without_message(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    def boom(_conn: Any, _evidence_id: UUID) -> Evidence | None:
        raise RuntimeError("fake internal detail password=fake-pw")

    monkeypatch.setattr(evidence_repo, "load_evidence", boom)
    assert _process(FakeProvider([INITIAL]), FakeExecutor(_default_items)) == JOB_ID
    assert store.job["status"] == "failed" and store.job["error"] == "internal_error"
    records = [r for r in log_capture.records() if r.get("event") == "investigation_internal_error"]
    assert len(records) == 1
    assert records[0]["job_id"] == str(JOB_ID) and records[0]["error_type"] == "RuntimeError"
    assert "fake internal detail" not in json.dumps(log_capture.records())


def test_claim_validation_error_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    def bad_claim(_conn: Any, _worker_id: str, _lease: float) -> ClaimedJob | None:
        InvestigationRequest.model_validate({"timestamp": "fake-secret-ts"})
        return None

    monkeypatch.setattr(investigation_jobs, "claim_job", bad_claim)
    provider = FakeProvider([INITIAL])
    engine = MagicMock(name="engine")
    engine.begin.return_value.__enter__.return_value.execute.return_value.first.return_value = None
    assert process_investigation(engine, SETTINGS, provider, "worker-a") is None
    stop = MagicMock(spec=threading.Event)
    stop.is_set.return_value = False
    assert (
        run(
            engine,
            0.1,
            stop,
            max_iterations=1,
            process_investigation=lambda e: process_investigation(
                e, SETTINGS, provider, "worker-a"
            ),
        )
        == 0
    )
    assert provider.requests == []
    records = [r for r in log_capture.records() if r.get("event") == "investigation_claim_failed"]
    assert records and records[0]["error_type"] == "ValidationError"
    assert "fake-secret-ts" not in log_capture.stream.getvalue()


def test_fail_job_error_in_internal_error_path_is_logged(
    store: FakeStore, monkeypatch: pytest.MonkeyPatch, log_capture: LogCapture
) -> None:
    def boom(_conn: Any, _evidence_id: UUID) -> Evidence | None:
        raise RuntimeError("fake internal detail")

    def lost(*_args: Any) -> None:
        raise LeaseLostError

    monkeypatch.setattr(evidence_repo, "load_evidence", boom)
    monkeypatch.setattr(investigation_jobs, "fail_job", lost)
    assert _process(FakeProvider([INITIAL]), FakeExecutor(_default_items)) == JOB_ID
    records = [
        r for r in log_capture.records() if r.get("event") == "investigation_fail_job_failed"
    ]
    assert len(records) == 1 and records[0]["error_type"] == "LeaseLostError"


def test_no_pending_job_returns_none(store: FakeStore) -> None:
    store.job["status"] = "completed"
    provider = FakeProvider([INITIAL])
    assert _process(provider, FakeExecutor(_default_items)) is None
    assert provider.requests == []
