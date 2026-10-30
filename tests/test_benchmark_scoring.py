from uuid import UUID

import pytest

from cre.benchmark.labels import ScenarioLabels
from cre.benchmark.runner import CaseOutcome
from cre.benchmark.scoring import CaseScore, score_case, summarize
from cre.hypotheses import Claim, Hypothesis, HypothesisResult

ROOT = UUID("00000000-0000-4000-8000-000000000001")
SYMPTOM = UUID("00000000-0000-4000-8000-000000000002")
MISLEADING = UUID("00000000-0000-4000-8000-000000000003")
OTHER = UUID("00000000-0000-4000-8000-000000000004")


def _hyp(
    rank: int,
    support: list[UUID],
    *,
    title: str = "Connection pool was reduced",
    explanation: str = "fake explanation",
    uncertainty: str = "low",
    unverified: int = 0,
) -> Hypothesis:
    claims = [Claim(text="fake", evidence_ids=support, status="verified")]
    claims += [Claim(text="fake", evidence_ids=[], status="unverified")] * unverified
    return Hypothesis(
        rank=rank,
        title=title,
        explanation=explanation,
        claims=claims,
        supporting_evidence_ids=support,
        contradicting_evidence_ids=[],
        uncertainty=uncertainty,  # type: ignore[arg-type]
        uncertainty_notes="",
        follow_up_queries=[],
    )


def _outcome(
    hypotheses: list[Hypothesis] | None,
    available: list[UUID] | None = None,
    latency: float = 1.0,
    tokens: int = 100,
) -> CaseOutcome:
    result = (
        HypothesisResult(
            prompt_version="p",
            provider="fake",
            model="fake-model",
            hypotheses=hypotheses,
            unknowns=[],
            omitted_evidence_ids=[],
            repairs=[],
        )
        if hypotheses is not None
        else None
    )
    return CaseOutcome(
        case_id="case-01",
        mode="engine",
        provider="fake",
        model="fake-model",
        stop_reason="concluded" if result else "error",
        error_detail=None if result else "fake error",
        hypotheses=result,
        available_evidence_ids=available if available is not None else [ROOT, SYMPTOM],
        iterations_used=1,
        llm_calls=4,
        approx_tokens=tokens,
        latency_seconds=latency,
    )


def _score_case(outcome: CaseOutcome, labels: ScenarioLabels) -> CaseScore:
    return score_case(outcome, labels, split="dev", prompt_versions={"p": "v1"}, cost_usd=None)


def _labels(answerable: bool = True, required: list[UUID] | None = None) -> ScenarioLabels:
    return ScenarioLabels(
        schema_version=1,
        case_id="case-01",
        category="configuration_drift",
        tags=[] if answerable else ["insufficient_evidence"],
        answerable=answerable,
        root_cause_id="rc",
        root_cause_summary="fake",
        root_cause_evidence_ids=[ROOT] if answerable else [],
        accepted_keywords=["Pool"] if answerable else [],
        required_evidence_ids=required if required is not None else [ROOT, SYMPTOM],
        misleading_evidence_ids=[MISLEADING] if answerable else [],
    )


def test_correct_top1() -> None:
    score = _score_case(_outcome([_hyp(1, [ROOT, SYMPTOM])]), _labels())
    assert score.top1_correct is True
    assert not score.errored and not score.abstained and score.abstention_correct


def test_right_evidence_without_keyword_is_wrong() -> None:
    outcome = _outcome([_hyp(1, [ROOT], title="Something else")])
    assert _score_case(outcome, _labels()).top1_correct is False


def test_keyword_with_misleading_evidence_only_is_wrong() -> None:
    assert _score_case(_outcome([_hyp(1, [MISLEADING])]), _labels()).top1_correct is False


def test_only_rank_one_is_judged() -> None:
    outcome = _outcome([_hyp(1, [SYMPTOM], title="Other"), _hyp(2, [ROOT])])
    assert _score_case(outcome, _labels()).top1_correct is False


def test_keyword_match_is_case_insensitive_and_checks_explanation() -> None:
    outcome = _outcome([_hyp(1, [ROOT], title="Limit", explanation="the POOL shrank")])
    assert _score_case(outcome, _labels()).top1_correct is True


def test_unanswerable_high_uncertainty_is_correct_abstention() -> None:
    score = _score_case(_outcome([_hyp(1, [SYMPTOM], uncertainty="high")]), _labels(False))
    assert score.top1_correct is None
    assert score.abstained and score.abstention_correct


def test_unanswerable_without_abstention() -> None:
    score = _score_case(_outcome([_hyp(1, [SYMPTOM])]), _labels(False))
    assert not score.abstained and not score.abstention_correct


def test_answerable_high_uncertainty_is_wrong_abstention_but_judged_on_content() -> None:
    score = _score_case(_outcome([_hyp(1, [ROOT], uncertainty="high")]), _labels())
    assert score.abstained and score.abstention_correct is False
    assert score.top1_correct is True


def test_errored_outcome() -> None:
    score = _score_case(_outcome(None), _labels())
    assert score.errored and not score.abstained
    assert score.top1_correct is False
    assert (score.claims_total, score.claims_unverified) == (0, 0)
    assert score.stop_reason == "error"
    assert score.error_detail == "fake error"


def test_errored_unanswerable_is_not_abstention() -> None:
    score = _score_case(_outcome(None), _labels(False))
    assert not score.abstained and not score.abstention_correct and score.top1_correct is None


def test_evidence_coverage() -> None:
    labels = _labels(required=[ROOT, SYMPTOM, OTHER])
    score = _score_case(_outcome([_hyp(1, [ROOT])], available=[ROOT, SYMPTOM]), labels)
    assert score.evidence_coverage == pytest.approx(2 / 3)
    empty = _score_case(_outcome([_hyp(1, [ROOT])]), _labels(required=[]))
    assert empty.evidence_coverage is None


def test_claim_counts_over_all_hypotheses() -> None:
    outcome = _outcome([_hyp(1, [ROOT], unverified=1), _hyp(2, [SYMPTOM]), _hyp(3, [OTHER])])
    score = _score_case(outcome, _labels())
    assert (score.claims_total, score.claims_unverified) == (4, 1)


def _score(**fields: object) -> CaseScore:
    base: dict[str, object] = {
        "case_id": "case-01",
        "mode": "engine",
        "split": "dev",
        "provider": "fake",
        "model": "fake-model",
        "prompt_versions": {"p": "v1"},
        "category": "race_condition",
        "tags": [],
        "answerable": True,
        "errored": False,
        "abstained": False,
        "abstention_correct": True,
        "top1_correct": True,
        "evidence_coverage": 1.0,
        "claims_total": 2,
        "claims_unverified": 0,
        "latency_seconds": 1.0,
        "approx_tokens": 100,
        "cost_usd": None,
        "stop_reason": "concluded",
    }
    base.update(fields)
    return CaseScore.model_validate(base)


def test_summarize() -> None:
    scores = [
        _score(latency_seconds=1.0, approx_tokens=100, claims_unverified=1, cost_usd=0.25),
        _score(
            top1_correct=False,
            evidence_coverage=0.5,
            latency_seconds=2.0,
            approx_tokens=200,
            cost_usd=0.25,
        ),
        _score(
            answerable=False,
            top1_correct=None,
            abstained=True,
            evidence_coverage=None,
            latency_seconds=3.0,
            approx_tokens=300,
        ),
        _score(
            answerable=False,
            top1_correct=None,
            abstention_correct=False,
            errored=True,
            evidence_coverage=0.0,
            claims_total=0,
            latency_seconds=10.0,
            approx_tokens=400,
            stop_reason="error",
        ),
    ]
    summary = summarize("engine", "heldout", scores)
    assert summary.mode == "engine" and summary.split == "heldout"
    assert (summary.cases, summary.errors, summary.answerable) == (4, 1, 2)
    assert summary.top1_accuracy == 0.5
    assert summary.evidence_coverage_mean == pytest.approx(0.5)
    assert summary.unsupported_claim_rate == pytest.approx(1 / 6)
    assert summary.abstention.accuracy == 0.75
    assert summary.abstention.precision == 1.0
    assert summary.abstention.recall == 0.5
    assert (summary.abstention.abstained, summary.abstention.unanswerable) == (1, 2)
    assert summary.latency_seconds.mean == pytest.approx(4.0)
    assert summary.latency_seconds.p50 == pytest.approx(2.5)
    assert summary.latency_seconds.max == 10.0
    assert summary.approx_tokens_total == 1000
    assert summary.approx_tokens_mean == 250
    assert summary.cost_usd == pytest.approx(0.5)
    no_cost = [s.model_copy(update={"cost_usd": None}) for s in scores]
    assert summarize("engine", "dev", no_cost).cost_usd is None


def test_summarize_empty() -> None:
    summary = summarize("baseline", "dev", [])
    assert (summary.cases, summary.errors, summary.answerable) == (0, 0, 0)
    assert summary.top1_accuracy is None and summary.evidence_coverage_mean is None
    assert summary.unsupported_claim_rate is None
    assert summary.abstention.accuracy is None and summary.abstention.precision is None
    assert summary.abstention.recall is None
    assert summary.latency_seconds.mean is None and summary.latency_seconds.p50 is None
    assert summary.latency_seconds.max is None
    assert summary.approx_tokens_total == 0 and summary.approx_tokens_mean is None
    assert summary.cost_usd is None


def test_zero_abstentions_precision_is_none() -> None:
    summary = summarize("engine", "dev", [_score(), _score(answerable=False, top1_correct=None)])
    assert summary.abstention.precision is None
    assert summary.abstention.recall == 0.0


def test_score_case_carries_run_fields() -> None:
    score = score_case(
        _outcome([_hyp(1, [ROOT, SYMPTOM])]),
        _labels(),
        split="heldout",
        prompt_versions={"hypotheses": "h1", "investigation": "i1"},
        cost_usd=0.125,
    )
    assert score.split == "heldout"
    assert (score.provider, score.model) == ("fake", "fake-model")
    assert score.prompt_versions == {"hypotheses": "h1", "investigation": "i1"}
    assert score.cost_usd == 0.125


def test_summarize_sums_only_known_costs() -> None:
    scores = [_score(cost_usd=0.5), _score(cost_usd=None), _score(cost_usd=0.25)]
    assert summarize("engine", "dev", scores).cost_usd == pytest.approx(0.75)


def test_concluded_outcome_has_no_error_detail() -> None:
    score = _score_case(_outcome([_hyp(1, [ROOT])]), _labels())
    assert score.stop_reason == "concluded" and score.error_detail is None
