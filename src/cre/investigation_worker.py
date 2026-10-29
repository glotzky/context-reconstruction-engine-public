"""Claim, collect, resume and checkpoint orchestration for durable investigation jobs.

Each step that writes runs in its own transaction together with a lease renewal. Persisted
steps are final: a resumed run never re-runs a collect or checkpoint step. Logs carry only the
event, job id and exception class name, never the request, evidence, prompts or output.
"""

import logging
import time
from typing import Literal
from uuid import UUID

from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError

from cre import evidence_repo, investigation_jobs
from cre.config import Settings
from cre.evidence import Evidence
from cre.investigation_jobs import (
    ClaimedJob,
    CollectGap,
    CollectStepPayload,
    LeaseLostError,
    StepView,
)
from cre.investigator import (
    Clock,
    InvestigationCheckpoint,
    ToolExecutor,
    investigate,
    make_db_executor,
)
from cre.llm import LLMProvider
from cre.retrieval import EvidenceQuery

logger = logging.getLogger("cre.worker")

_COLLECT_SEQ = {"trace": 0, "service": 1}
_INITIAL_STEP = "hypotheses:initial"


def _checkpoint_seq(step_key: str) -> int:
    if step_key == _INITIAL_STEP:
        return 2
    return 2 + int(step_key.removeprefix("round:"))


def _log(event: str, job_id: UUID, **extra: str) -> None:
    logger.info(event, extra={"event": event, "job_id": str(job_id), **extra})


def _collect(
    engine: Engine,
    settings: Settings,
    worker_id: str,
    job: ClaimedJob,
    steps: list[StepView],
    executor: ToolExecutor,
) -> None:
    persisted = {step.step_key for step in steps}
    request = job.request
    queries: list[tuple[Literal["trace", "service"], EvidenceQuery]] = []
    limit = settings.investigation_per_query_limit
    if request.trace_id is not None:
        queries.append(
            (
                "trace",
                EvidenceQuery(trace_id=request.trace_id, anchor=request.timestamp, limit=limit),
            )
        )
    if request.service is not None:
        queries.append(
            (
                "service",
                EvidenceQuery(service=request.service, anchor=request.timestamp, limit=limit),
            )
        )
    for name, query in queries:
        step_key = f"collect:{name}"
        if step_key in persisted:
            continue
        status = "completed"
        try:
            result = executor(query)
        except Exception as exc:
            status = "failed"
            payload = CollectStepPayload(
                query=name,
                limit=limit,
                gap=CollectGap(kind="collector_failed", error_type=type(exc).__name__),
            )
        else:
            payload = CollectStepPayload(
                query=name,
                start=result.start,
                end=result.end,
                limit=result.limit,
                count=result.count,
                truncated=result.truncated,
                evidence_ids=[item.id for item in result.items],
            )
        with engine.begin() as conn:
            investigation_jobs.record_step(
                conn,
                job.id,
                worker_id,
                settings.investigation_lease_seconds,
                step_key=step_key,
                seq=_COLLECT_SEQ[name],
                kind="collect",
                status=status,
                payload=payload.model_dump(mode="json"),
            )


def _latest_checkpoint(steps: list[StepView]) -> InvestigationCheckpoint | None:
    checkpoints = [step for step in steps if step.kind == "checkpoint"]
    if not checkpoints:
        return None
    latest = max(checkpoints, key=lambda step: step.seq)
    return InvestigationCheckpoint.model_validate(latest.payload)


def _evidence_ids(steps: list[StepView], checkpoint: InvestigationCheckpoint | None) -> list[UUID]:
    if checkpoint is not None:
        candidates = list(checkpoint.evidence_ids)
    else:
        candidates = []
        for step in sorted(steps, key=lambda s: s.seq):
            if step.kind == "collect" and step.status == "completed":
                candidates.extend(CollectStepPayload.model_validate(step.payload).evidence_ids)
    return list(dict.fromkeys(candidates))


def process_investigation(
    engine: Engine,
    settings: Settings,
    provider: LLMProvider,
    worker_id: str,
    *,
    executor: ToolExecutor | None = None,
    clock: Clock = time.monotonic,
) -> UUID | None:
    """Claim one investigation job and run it to completion. Returns its id, or None."""
    try:
        with engine.begin() as conn:
            job = investigation_jobs.claim_job(
                conn, worker_id, settings.investigation_lease_seconds
            )
    except DBAPIError:
        raise
    except Exception as exc:
        logger.error(
            "investigation_claim_failed",
            extra={"event": "investigation_claim_failed", "error_type": type(exc).__name__},
        )
        return None
    if job is None:
        return None
    try:
        if job.attempts > settings.investigation_job_max_attempts:
            with engine.begin() as conn:
                investigation_jobs.fail_job(conn, job.id, worker_id, "max_attempts_exceeded")
            _log("investigation_failed", job.id, error="max_attempts_exceeded")
            return job.id
        run_executor = executor if executor is not None else make_db_executor(engine, settings)
        with engine.connect() as conn:
            steps = investigation_jobs.load_steps(conn, job.id)
        _collect(engine, settings, worker_id, job, steps, run_executor)
        with engine.connect() as conn:
            steps = investigation_jobs.load_steps(conn, job.id)
            checkpoint = _latest_checkpoint(steps)
            evidence: list[Evidence] = []
            for evidence_id in _evidence_ids(steps, checkpoint):
                item = evidence_repo.load_evidence(conn, evidence_id)
                if item is not None:
                    evidence.append(item)
        if not evidence:
            with engine.begin() as conn:
                investigation_jobs.fail_job(conn, job.id, worker_id, "no_initial_evidence")
            _log("investigation_failed", job.id, error="no_initial_evidence")
            return job.id

        def on_step(step_key: str, cp: InvestigationCheckpoint) -> None:
            with engine.begin() as conn:
                investigation_jobs.record_step(
                    conn,
                    job.id,
                    worker_id,
                    settings.investigation_lease_seconds,
                    step_key=step_key,
                    seq=_checkpoint_seq(step_key),
                    kind="checkpoint",
                    status="completed",
                    payload=cp.model_dump(mode="json"),
                )

        result = investigate(
            evidence,
            anchor=job.request.timestamp,
            provider=provider,
            executor=run_executor,
            settings=settings,
            clock=clock,
            checkpoint=checkpoint,
            on_step=on_step,
        )
        with engine.begin() as conn:
            investigation_jobs.complete_job(conn, job.id, worker_id, result.model_dump(mode="json"))
        _log("investigation_completed", job.id)
    except LeaseLostError:
        _log("investigation_lease_lost", job.id)
    except DBAPIError:
        raise
    except Exception as exc:
        try:
            with engine.begin() as conn:
                investigation_jobs.fail_job(conn, job.id, worker_id, "internal_error")
        except Exception as fail_exc:
            logger.error(
                "investigation_fail_job_failed",
                extra={
                    "event": "investigation_fail_job_failed",
                    "job_id": str(job.id),
                    "error_type": type(fail_exc).__name__,
                },
            )
        logger.error(
            "investigation_internal_error",
            extra={
                "event": "investigation_internal_error",
                "job_id": str(job.id),
                "error_type": type(exc).__name__,
            },
        )
    return job.id
