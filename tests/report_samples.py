"""Deterministic fake jobs and evidence for report tests (no real data, no test functions)."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from cre.evidence import EVIDENCE_ADAPTER, Evidence
from cre.hypotheses import Claim, Hypothesis, HypothesisResult, RepairNote
from cre.investigation_jobs import JobView
from cre.investigator import InvestigationResult, InvestigationRound, ToolCallRecord
from tests.evidence_samples import FAKE_DSN, LOG_SOURCE, RETRIEVED_AT, TRACE_ID

JOB_ID = UUID("00000000-0000-4000-8000-0000000000a1")
PENDING_ID = UUID("00000000-0000-4000-8000-0000000000a2")
FAILED_ID = UUID("00000000-0000-4000-8000-0000000000a3")
INVALID_ID = UUID("00000000-0000-4000-8000-0000000000a4")
CREATED = datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)

LOG_ID = UUID("00000000-0000-4000-8000-0000000000e1")
DEPLOY_ID = UUID("00000000-0000-4000-8000-0000000000e2")
MISSING_ID = UUID("00000000-0000-4000-8000-0000000000e3")
OUTSIDER_ID = UUID("00000000-0000-4000-8000-0000000000e4")

SCRIPT = "<script>alert(1)</script>"
BAD_KEY = '"><img src=x onerror=alert(1)>'


def make_log(
    evidence_id: UUID, message: str = SCRIPT, attributes: dict[str, Any] | None = None
) -> Evidence:
    return EVIDENCE_ADAPTER.validate_python(
        {
            "id": evidence_id,
            "kind": "log",
            "timestamp": "2026-01-01T00:00:03Z",
            "trace_id": TRACE_ID,
            "service": "fake-svc",
            "provenance": {
                "source": LOG_SOURCE.model_dump(),
                "retrieval_method": "fake_fixture",
                "identifiers": {"line": "42"},
                "retrieved_at": RETRIEVED_AT,
            },
            "observed": {
                "level": "ERROR",
                "message": message,
                "attributes": attributes
                if attributes is not None
                else {BAD_KEY: "fake", "dsn": f"conn {FAKE_DSN}"},
            },
        }
    )


def make_deploy(evidence_id: UUID = DEPLOY_ID) -> Evidence:
    return EVIDENCE_ADAPTER.validate_python(
        {
            "id": evidence_id,
            "kind": "deployment",
            "timestamp": "2026-01-01T00:00:00Z",
            "service": "fake-svc",
            "deployment_version": "0.2.0",
            "provenance": {
                "source": {"source_type": "deployment_event", "name": "fake deploys"},
                "retrieval_method": "fake_fixture",
                "identifiers": {"deployment_id": "deploy-1"},
                "retrieved_at": RETRIEVED_AT,
            },
            "observed": {"version": "0.2.0", "status": "succeeded"},
        }
    )


def make_hypotheses(
    follow_ups: tuple[list[str], list[str]] = (["q1", "q2"], ["q2", "q3"]),
) -> HypothesisResult:
    second = Hypothesis(
        rank=2,
        title="Second fake hypothesis",
        explanation="fake explanation two",
        claims=[Claim(text="cites missing", evidence_ids=[MISSING_ID], status="verified")],
        supporting_evidence_ids=[MISSING_ID],
        contradicting_evidence_ids=[],
        uncertainty="high",
        uncertainty_notes="fake notes two",
        follow_up_queries=follow_ups[1],
    )
    first = Hypothesis(
        rank=1,
        title=f"First {SCRIPT}",
        explanation="fake explanation one",
        claims=[
            Claim(text="verified fake claim", evidence_ids=[LOG_ID], status="verified"),
            Claim(text="unverified fake claim", evidence_ids=[], status="unverified"),
        ],
        supporting_evidence_ids=[LOG_ID],
        contradicting_evidence_ids=[DEPLOY_ID],
        uncertainty="medium",
        uncertainty_notes="fake notes one",
        follow_up_queries=follow_ups[0],
    )
    return HypothesisResult(
        prompt_version="fake-prompt-v1",
        provider="fake",
        model="fake-model",
        hypotheses=[first, second],
        unknowns=["fake unknown question"],
        omitted_evidence_ids=[],
        repairs=[
            RepairNote(kind="unknown_evidence_id_removed", hypothesis_title="x", detail="fake")
        ],
    )


def make_result(
    *,
    hypotheses: HypothesisResult | None = None,
    initial: HypothesisResult | None = None,
    evidence_ids: list[UUID] | None = None,
) -> InvestigationResult:
    call = ToolCallRecord(
        iteration=1,
        tool="fetch_by_trace",
        status="executed",
        arguments={"trace_id": TRACE_ID, BAD_KEY: SCRIPT},
        retrieval_method="fake_retrieval",
        count=1,
        evidence_ids=[LOG_ID],
    )
    return InvestigationResult(
        prompt_version="fake-prompt-v1",
        provider="fake",
        model="fake-model",
        stop_reason="concluded",
        budget_exhausted=None,
        error_detail=None,
        iterations_used=1,
        approx_tokens_used=10,
        initial_hypotheses=initial,
        hypotheses=hypotheses,
        rounds=[
            InvestigationRound(
                iteration=1,
                action="call_tools",
                tool_calls=[call],
                new_evidence_ids=[],
                revised=False,
                adjustments=[],
            )
        ],
        evidence_ids=evidence_ids if evidence_ids is not None else [LOG_ID, DEPLOY_ID, MISSING_ID],
    )


def make_job(
    job_id: UUID = JOB_ID,
    *,
    status: str = "completed",
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> JobView:
    return JobView.model_validate(
        {
            "id": job_id,
            "status": status,
            "attempts": 2,
            "request": {"timestamp": "2026-01-01T00:00:05Z", "symptom": SCRIPT},
            "error": error,
            "result": result,
            "created_at": CREATED,
            "updated_at": CREATED,
            "steps": [],
        }
    )


def completed_job(job_id: UUID = JOB_ID) -> JobView:
    return make_job(
        job_id, result=make_result(hypotheses=make_hypotheses()).model_dump(mode="json")
    )
