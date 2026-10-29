"""Server-rendered investigation report and evidence pages.

The builder is pure: it turns a stored job and the evidence loaded for it into a view model.
Rendering uses Jinja autoescape only; every string from evidence, results or jobs is untrusted.
Evidence is linked only when its id belongs to the job's result and it was actually loaded.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from jinja2 import Environment, PackageLoader, select_autoescape
from pydantic import JsonValue, ValidationError
from sqlalchemy import Connection

from cre.benchmark import DEFAULT_DATA_DIR
from cre.benchmark.fake import load_fake_script, make_fake_provider
from cre.benchmark.runner import make_memory_executor
from cre.benchmark.scenarios import load_input
from cre.config import Settings
from cre.evidence import Evidence, _Model
from cre.evidence_repo import load_evidence_many
from cre.hypotheses import ClaimStatus, RepairNote, Uncertainty
from cre.investigation_jobs import JobStatus, JobView, get_job
from cre.investigator import InvestigationResult, investigate
from cre.redaction import redact, redact_evidence
from cre.timeline import TimelineEntry, TimelineRelationship, TimelineUnknown, build_timeline

REPORT_MAX_EVIDENCE = 500
CONTENT_MAX_CHARS = 20_000
DEMO_JOB_ID = UUID("00000000-0000-4000-8000-00000000de01")
_DEMO_CASE = "case-01"
_DEMO_CREATED = datetime(2026, 1, 1, 0, 10, 0, tzinfo=UTC)
_DEMO_UPDATED = datetime(2026, 1, 1, 0, 11, 0, tzinfo=UTC)


class ReportSource(Protocol):
    def get_job(self, job_id: UUID) -> JobView | None: ...

    def load_evidence(self, ids: Sequence[UUID]) -> list[Evidence]: ...


class DbReportSource:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def get_job(self, job_id: UUID) -> JobView | None:
        return get_job(self._conn, job_id)

    def load_evidence(self, ids: Sequence[UUID]) -> list[Evidence]:
        return load_evidence_many(self._conn, ids)


class MemoryReportSource:
    def __init__(self, jobs: Mapping[UUID, JobView], evidence: Sequence[Evidence]) -> None:
        self._jobs = dict(jobs)
        self._evidence = {item.id: item for item in evidence}

    def get_job(self, job_id: UUID) -> JobView | None:
        return self._jobs.get(job_id)

    def load_evidence(self, ids: Sequence[UUID]) -> list[Evidence]:
        return [self._evidence[i] for i in dict.fromkeys(ids) if i in self._evidence]


def build_demo_source(settings: Settings) -> MemoryReportSource:
    """Run benchmark case-01 with its scripted fake provider; no database or real model."""
    root = DEFAULT_DATA_DIR
    scenario = load_input(root / "inputs" / f"{_DEMO_CASE}.json", root)
    script = load_fake_script(root / "fake" / f"{_DEMO_CASE}.json", root)
    result = investigate(
        scenario.initial_evidence(),
        anchor=scenario.anchor,
        provider=make_fake_provider(script, "engine", settings.llm_model or "fake-model"),
        executor=make_memory_executor(scenario.evidence, settings),
        settings=settings,
    )
    job = JobView(
        id=DEMO_JOB_ID,
        status="completed",
        attempts=1,
        request=redact({"timestamp": scenario.anchor.isoformat(), "symptom": "fake demo symptom"}),
        error=None,
        result=result.model_dump(mode="json"),
        created_at=_DEMO_CREATED,
        updated_at=_DEMO_UPDATED,
        steps=[],
    )
    return MemoryReportSource({DEMO_JOB_ID: job}, scenario.evidence)


class EvidenceRef(_Model):
    id: UUID
    resolved: bool
    label: str


class ClaimView(_Model):
    text: str
    status: ClaimStatus
    evidence: list[EvidenceRef]


class HypothesisView(_Model):
    rank: int
    title: str
    explanation: str
    claims: list[ClaimView]
    supporting: list[EvidenceRef]
    contradicting: list[EvidenceRef]
    uncertainty: Uncertainty
    uncertainty_notes: str
    follow_up_queries: list[str]


class TimelineEntryView(_Model):
    entry: TimelineEntry
    evidence: list[EvidenceRef]


class ToolCallView(_Model):
    iteration: int
    tool: str
    status: str
    retrieval_method: str
    arguments: dict[str, JsonValue] | None
    count: int
    truncated: bool
    evidence: list[EvidenceRef]


class ReportView(_Model):
    job_id: UUID
    status: JobStatus
    attempts: int
    error: str | None
    created_at: datetime
    updated_at: datetime
    complete: bool
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    stop_reason: str | None = None
    budget_exhausted: str | None = None
    error_detail: str | None = None
    observed: list[TimelineEntryView] = []
    relationships: list[TimelineRelationship] = []
    hypotheses: list[HypothesisView] = []
    unknowns: list[str] = []
    timeline_gaps: list[TimelineUnknown] = []
    missing_evidence_ids: list[UUID] = []
    repairs: list[RepairNote] = []
    next_steps: list[str] = []
    tool_calls: list[ToolCallView] = []


def parse_result(job: JobView) -> InvestigationResult | None:
    """The job's result, or None if the job is not completed or the result is unreadable."""
    if job.status != "completed" or job.result is None:
        return None
    try:
        return InvestigationResult.model_validate(job.result)
    except ValidationError:
        return None


def allowed_evidence_ids(job: JobView) -> frozenset[UUID]:
    result = parse_result(job)
    return frozenset(result.evidence_ids) if result is not None else frozenset()


def report_evidence_ids(job: JobView) -> list[UUID]:
    """The ids to load for a report, capped at ``REPORT_MAX_EVIDENCE``."""
    result = parse_result(job)
    if result is None:
        return []
    return list(dict.fromkeys(result.evidence_ids))[:REPORT_MAX_EVIDENCE]


def load_report(source: ReportSource, job: JobView) -> ReportView:
    ids = report_evidence_ids(job)
    return build_report(job, source.load_evidence(ids) if ids else [])


def _label(item: Evidence) -> str:
    return f"{item.kind} · {item.timestamp.astimezone(UTC).isoformat().replace('+00:00', 'Z')}"


def build_report(job: JobView, evidence: Sequence[Evidence]) -> ReportView:
    base = ReportView(
        job_id=job.id,
        status=job.status,
        attempts=job.attempts,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        complete=False,
    )
    result = parse_result(job)
    if result is None:
        return base
    all_ids = list(dict.fromkeys(result.evidence_ids))
    allowed = frozenset(all_ids[:REPORT_MAX_EVIDENCE])
    by_id = {item.id: item for item in evidence if item.id in allowed}
    loaded = [redact_evidence(by_id[i]) for i in all_ids if i in by_id]
    labels = {item.id: _label(item) for item in loaded}

    def refs(ids: Sequence[UUID]) -> list[EvidenceRef]:
        return [
            EvidenceRef(id=i, resolved=i in labels, label=labels.get(i, "evidence unavailable"))
            for i in ids
        ]

    timeline = build_timeline(loaded)
    chosen = result.hypotheses if result.hypotheses is not None else result.initial_hypotheses
    ranked = sorted(chosen.hypotheses, key=lambda h: h.rank) if chosen is not None else []
    hypotheses = [
        HypothesisView(
            rank=h.rank,
            title=h.title,
            explanation=h.explanation,
            claims=[
                ClaimView(text=c.text, status=c.status, evidence=refs(c.evidence_ids))
                for c in h.claims
            ],
            supporting=refs(h.supporting_evidence_ids),
            contradicting=refs(h.contradicting_evidence_ids),
            uncertainty=h.uncertainty,
            uncertainty_notes=h.uncertainty_notes,
            follow_up_queries=list(h.follow_up_queries),
        )
        for h in ranked
    ]
    next_steps = list(dict.fromkeys(q for h in ranked for q in h.follow_up_queries))
    tool_calls = [
        ToolCallView(
            iteration=call.iteration,
            tool=call.tool,
            status=call.status,
            retrieval_method=call.retrieval_method,
            arguments=call.arguments,
            count=call.count,
            truncated=call.truncated,
            evidence=refs(call.evidence_ids),
        )
        for round_ in result.rounds
        for call in round_.tool_calls
    ]
    return base.model_copy(
        update={
            "complete": True,
            "provider": result.provider,
            "model": result.model,
            "prompt_version": result.prompt_version,
            "stop_reason": result.stop_reason,
            "budget_exhausted": result.budget_exhausted,
            "error_detail": result.error_detail,
            "observed": [
                TimelineEntryView(entry=e, evidence=refs(e.evidence_ids)) for e in timeline.entries
            ],
            "relationships": list(timeline.relationships),
            "hypotheses": hypotheses,
            "unknowns": list(chosen.unknowns) if chosen is not None else [],
            "timeline_gaps": list(timeline.unknowns),
            "missing_evidence_ids": [i for i in all_ids if i not in labels],
            "repairs": list(chosen.repairs) if chosen is not None else [],
            "next_steps": next_steps,
            "tool_calls": tool_calls,
        }
    )


def _json_text(value: object) -> str:
    return json.dumps(value, indent=None, sort_keys=True)


_env = Environment(
    loader=PackageLoader("cre", "templates"),
    autoescape=select_autoescape(["html"]),
)
_env.filters["json_text"] = _json_text


def render_report(view: ReportView, evidence_href: Callable[[UUID], str]) -> str:
    return _env.get_template("report.html").render(view=view, evidence_href=evidence_href)


def render_evidence(job_id: UUID, item: Evidence, report_href: str) -> str:
    redacted = redact_evidence(item)
    content = json.dumps(redacted.observed.model_dump(mode="json"), indent=2, sort_keys=True)
    truncated = len(content) > CONTENT_MAX_CHARS
    return _env.get_template("evidence.html").render(
        job_id=job_id,
        item=redacted,
        content=content[:CONTENT_MAX_CHARS],
        truncated=truncated,
        max_chars=CONTENT_MAX_CHARS,
        report_href=report_href,
    )


def render_not_found() -> str:
    return _env.get_template("not_found.html").render()
