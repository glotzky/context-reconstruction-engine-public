"""Pure, deterministic benchmark metrics. No LLM judge."""

import statistics
from collections.abc import Sequence

from cre.benchmark.labels import Category, ScenarioLabels, Tag
from cre.benchmark.runner import CaseOutcome, Mode
from cre.evidence import _Model


class CaseScore(_Model):
    case_id: str
    mode: Mode
    category: Category
    tags: list[Tag]
    answerable: bool
    errored: bool
    abstained: bool
    abstention_correct: bool
    top1_correct: bool | None
    evidence_coverage: float | None
    claims_total: int
    claims_unverified: int
    latency_seconds: float
    approx_tokens: int
    stop_reason: str


class AbstentionSummary(_Model):
    accuracy: float | None
    precision: float | None
    recall: float | None
    abstained: int
    unanswerable: int


class LatencySummary(_Model):
    mean: float | None
    p50: float | None
    max: float | None


class ModeSummary(_Model):
    mode: Mode
    cases: int
    errors: int
    answerable: int
    top1_accuracy: float | None
    evidence_coverage_mean: float | None
    unsupported_claim_rate: float | None
    abstention: AbstentionSummary
    latency_seconds: LatencySummary
    approx_tokens_total: int
    approx_tokens_mean: float | None
    cost_usd: float | None


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def score_case(outcome: CaseOutcome, labels: ScenarioLabels) -> CaseScore:
    result = outcome.hypotheses
    errored = result is None
    top = result.hypotheses[0] if result is not None else None
    abstained = top is not None and top.uncertainty == "high"

    top1: bool | None = None
    if labels.answerable:
        top1 = False
        if top is not None:
            cites_cause = bool(
                set(top.supporting_evidence_ids) & set(labels.root_cause_evidence_ids)
            )
            text = f"{top.title} {top.explanation}".casefold()
            mentions_cause = any(k.casefold() in text for k in labels.accepted_keywords)
            top1 = cites_cause and mentions_cause

    required = set(labels.required_evidence_ids)
    coverage = (
        len(required & set(outcome.available_evidence_ids)) / len(required) if required else None
    )
    claims = [c for h in result.hypotheses for c in h.claims] if result is not None else []
    return CaseScore(
        case_id=outcome.case_id,
        mode=outcome.mode,
        category=labels.category,
        tags=list(labels.tags),
        answerable=labels.answerable,
        errored=errored,
        abstained=abstained,
        abstention_correct=abstained == (not labels.answerable),
        top1_correct=top1,
        evidence_coverage=coverage,
        claims_total=len(claims),
        claims_unverified=sum(1 for c in claims if c.status == "unverified"),
        latency_seconds=outcome.latency_seconds,
        approx_tokens=outcome.approx_tokens,
        stop_reason=outcome.stop_reason,
    )


def summarize(
    mode: Mode, scores: Sequence[CaseScore], usd_per_1k_tokens: float | None
) -> ModeSummary:
    cases = len(scores)
    answerable = [s for s in scores if s.answerable]
    coverages = [s.evidence_coverage for s in scores if s.evidence_coverage is not None]
    claims_total = sum(s.claims_total for s in scores)
    abstained = sum(1 for s in scores if s.abstained)
    unanswerable = sum(1 for s in scores if not s.answerable)
    true_abstentions = sum(1 for s in scores if s.abstained and not s.answerable)
    latencies = [s.latency_seconds for s in scores]
    tokens_total = sum(s.approx_tokens for s in scores)
    return ModeSummary(
        mode=mode,
        cases=cases,
        errors=sum(1 for s in scores if s.errored),
        answerable=len(answerable),
        top1_accuracy=_ratio(sum(1 for s in answerable if s.top1_correct), len(answerable)),
        evidence_coverage_mean=statistics.fmean(coverages) if coverages else None,
        unsupported_claim_rate=_ratio(sum(s.claims_unverified for s in scores), claims_total),
        abstention=AbstentionSummary(
            accuracy=_ratio(sum(1 for s in scores if s.abstention_correct), cases),
            precision=_ratio(true_abstentions, abstained),
            recall=_ratio(true_abstentions, unanswerable),
            abstained=abstained,
            unanswerable=unanswerable,
        ),
        latency_seconds=LatencySummary(
            mean=statistics.fmean(latencies) if latencies else None,
            p50=statistics.median(latencies) if latencies else None,
            max=max(latencies) if latencies else None,
        ),
        approx_tokens_total=tokens_total,
        approx_tokens_mean=tokens_total / cases if cases else None,
        cost_usd=tokens_total / 1000 * usd_per_1k_tokens if usd_per_1k_tokens is not None else None,
    )
