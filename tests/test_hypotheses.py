import json
import re
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from cre.bundle import BundleLimits, build_bundle
from cre.hypotheses import (
    MAX_OUTPUT_CHARS,
    Claim,
    Hypothesis,
    HypothesisOutputError,
    HypothesisResult,
    generate_hypotheses,
    parse_and_validate,
)
from cre.llm import FAKE_EMPTY_RESPONSE, FakeProvider, LLMProviderError, LLMRequest
from cre.prompts import HYPOTHESIS_PROMPT_VERSION, HYPOTHESIS_SYSTEM_PROMPT
from tests.test_bundle import COMMIT, DEPLOYMENT, INJECTION, LOG, SCENARIO, SPAN, _log

BUNDLE = build_bundle(SCENARIO)
HEADER = re.compile(r"<<<EVIDENCE id=([0-9a-f-]+) kind=")


def _claim(ids: list[str], status: str = "verified", text: str = "a claim") -> dict[str, Any]:
    return {"text": text, "evidence_ids": ids, "status": status}


def _hyp(
    rank: int = 1,
    supporting: list[str] | None = None,
    claims: list[dict[str, Any]] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    supporting = [str(LOG.id)] if supporting is None else supporting
    return {
        "rank": rank,
        "title": f"hypothesis {rank}",
        "explanation": "fake explanation",
        "claims": claims if claims is not None else [_claim(supporting)],
        "supporting_evidence_ids": supporting,
        "contradicting_evidence_ids": [],
        "uncertainty": "medium",
        "uncertainty_notes": "fake notes",
        "follow_up_queries": ["check fake migration"],
        **fields,
    }


def _raw(*hypotheses: dict[str, Any], unknowns: list[str] | None = None) -> str:
    return json.dumps({"hypotheses": list(hypotheses), "unknowns": unknowns or []})


def _parse(raw: str, bundle: Any = BUNDLE) -> HypothesisResult:
    return parse_and_validate(raw, bundle, provider="fake", model="fake-model")


def test_valid_output() -> None:
    raw = _raw(
        _hyp(1, [str(LOG.id), str(SPAN.id)]),
        _hyp(2, [str(DEPLOYMENT.id)], contradicting_evidence_ids=[str(COMMIT.id)]),
        unknowns=["no db metrics"],
    )
    result = _parse(raw)
    assert [h.rank for h in result.hypotheses] == [1, 2]
    assert result.repairs == []
    assert result.prompt_version == HYPOTHESIS_PROMPT_VERSION
    assert (result.provider, result.model) == ("fake", "fake-model")
    assert result.unknowns == ["no db metrics"]
    assert result.omitted_evidence_ids == []
    assert all(c.status == "verified" for h in result.hypotheses for c in h.claims)
    assert result.hypotheses[0].supporting_evidence_ids == [LOG.id, SPAN.id]
    assert result.hypotheses[1].contradicting_evidence_ids == [COMMIT.id]


def test_unverified_claim_kept() -> None:
    claims = [_claim([str(LOG.id)]), _claim([], status="unverified", text="guess")]
    result = _parse(_raw(_hyp(claims=claims)))
    assert result.hypotheses[0].claims[1].status == "unverified"
    assert result.hypotheses[0].claims[1].evidence_ids == []
    assert result.repairs == []


def test_unverified_claim_with_valid_ids_becomes_verified() -> None:
    result = _parse(_raw(_hyp(claims=[_claim([str(LOG.id)], status="unverified")])))
    assert result.hypotheses[0].claims[0].status == "verified"
    assert result.repairs == []


def test_malformed_json_rejected_without_echo() -> None:
    raw = '{"hypotheses": [SECRET_MARKER_TEXT'
    with pytest.raises(HypothesisOutputError) as excinfo:
        _parse(raw)
    assert "SECRET_MARKER_TEXT" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps(
            {
                "hypotheses": [{k: v for k, v in _hyp().items() if k != "uncertainty"}],
                "unknowns": [],
            }
        ),
        _raw(_hyp(extra_field="x")),
        _raw(*[_hyp(i) for i in range(1, 7)]),
    ],
    ids=["missing_uncertainty", "extra_field", "too_many"],
)
def test_schema_violation_rejected(raw: str) -> None:
    with pytest.raises(HypothesisOutputError):
        _parse(raw)


def test_hallucinated_ids_removed_and_claim_downgraded() -> None:
    fake_id = str(uuid4())
    claims = [_claim([fake_id, "not-a-uuid"]), _claim([str(LOG.id), fake_id])]
    result = _parse(_raw(_hyp(supporting=[str(LOG.id), fake_id], claims=claims)))
    hypothesis = result.hypotheses[0]
    assert hypothesis.supporting_evidence_ids == [LOG.id]
    assert hypothesis.claims[0].status == "unverified"
    assert hypothesis.claims[0].evidence_ids == []
    assert hypothesis.claims[1].evidence_ids == [LOG.id]
    kinds = [r.kind for r in result.repairs]
    assert kinds.count("unknown_evidence_id_removed") == 4
    assert kinds.count("claim_downgraded") == 1
    details = " ".join(r.detail for r in result.repairs)
    assert fake_id in details and "not-a-uuid" in details


def test_long_bad_id_detail_is_truncated() -> None:
    bad = "q" * 500
    result = _parse(_raw(_hyp(supporting=[str(LOG.id), bad], claims=[_claim([str(LOG.id)])])))
    assert result.repairs[0].kind == "unknown_evidence_id_removed"
    assert "q" * 64 in result.repairs[0].detail
    assert "q" * 65 not in result.repairs[0].detail


def test_omitted_id_is_treated_as_unknown() -> None:
    bundle = build_bundle(SCENARIO, BundleLimits(max_items=2))
    assert COMMIT.id in bundle.omitted_evidence_ids
    result = _parse(_raw(_hyp(supporting=[str(LOG.id), str(COMMIT.id)])), bundle)
    assert result.hypotheses[0].supporting_evidence_ids == [LOG.id]
    assert "unknown_evidence_id_removed" in {r.kind for r in result.repairs}
    assert result.omitted_evidence_ids == bundle.omitted_evidence_ids
    assert any("2 evidence items were omitted" in u for u in result.unknowns)


def test_uppercase_and_brace_uuid_variants_accepted() -> None:
    variants = [str(LOG.id).upper(), "{" + str(LOG.id) + "}", str(SPAN.id)]
    result = _parse(_raw(_hyp(supporting=variants)))
    assert result.hypotheses[0].supporting_evidence_ids == [LOG.id, SPAN.id]
    assert result.repairs == []


def test_unsupported_hypothesis_dropped() -> None:
    result = _parse(_raw(_hyp(1, [str(uuid4())]), _hyp(2, [str(LOG.id)])))
    assert [h.title for h in result.hypotheses] == ["hypothesis 2"]
    assert result.hypotheses[0].rank == 1
    kinds = [r.kind for r in result.repairs]
    assert "hypothesis_dropped_unsupported" in kinds
    assert "reranked" in kinds


def test_all_hypotheses_dropped_raises() -> None:
    with pytest.raises(HypothesisOutputError):
        _parse(_raw(_hyp(1, [str(uuid4())]), _hyp(2, ["nope"])))


def test_empty_hypotheses_raises() -> None:
    with pytest.raises(HypothesisOutputError):
        _parse(FAKE_EMPTY_RESPONSE)


def test_conflicting_id_removed_from_both() -> None:
    hyp = _hyp(
        supporting=[str(LOG.id), str(SPAN.id)],
        contradicting_evidence_ids=[str(SPAN.id), str(COMMIT.id)],
    )
    result = _parse(_raw(hyp))
    hypothesis = result.hypotheses[0]
    assert hypothesis.supporting_evidence_ids == [LOG.id]
    assert hypothesis.contradicting_evidence_ids == [COMMIT.id]
    assert [r.kind for r in result.repairs] == ["conflicting_evidence_id_removed"]


def test_duplicate_and_gapped_ranks_renumbered() -> None:
    result = _parse(_raw(_hyp(7), _hyp(2), _hyp(2)))
    assert [h.rank for h in result.hypotheses] == [1, 2, 3]
    assert [h.title for h in result.hypotheses] == ["hypothesis 2", "hypothesis 2", "hypothesis 7"]
    assert [r.kind for r in result.repairs] == ["reranked"]


def test_duplicate_ids_deduplicated() -> None:
    result = _parse(_raw(_hyp(supporting=[str(LOG.id), str(LOG.id).upper()])))
    assert result.hypotheses[0].supporting_evidence_ids == [LOG.id]


def test_json_fence_accepted() -> None:
    result = _parse(f"\n```json\n{_raw(_hyp())}\n```\n")
    assert len(result.hypotheses) == 1
    assert len(_parse(f"```\n{_raw(_hyp())}\n```").hypotheses) == 1


def test_oversized_output_rejected() -> None:
    raw = _raw(_hyp()) + " " * MAX_OUTPUT_CHARS
    with pytest.raises(HypothesisOutputError):
        _parse(raw)


def test_generate_sends_prompt_with_limits() -> None:
    provider = FakeProvider([_raw(_hyp())])
    result = generate_hypotheses(BUNDLE, provider, max_output_tokens=123, timeout_seconds=4.5)
    assert len(result.hypotheses) == 1
    assert (result.provider, result.model) == ("fake", "fake-model")
    [request] = provider.requests
    assert request.system == HYPOTHESIS_SYSTEM_PROMPT
    assert request.user == BUNDLE.rendered
    assert request.max_output_tokens == 123
    assert request.timeout_seconds == 4.5


def test_provider_runtime_error_wrapped() -> None:
    def boom(_request: LLMRequest) -> str:
        raise RuntimeError("fake network down")

    with pytest.raises(LLMProviderError) as excinfo:
        generate_hypotheses(BUNDLE, FakeProvider(boom), max_output_tokens=10, timeout_seconds=1)
    assert isinstance(excinfo.value.__cause__, RuntimeError)


def test_provider_error_propagates_unchanged() -> None:
    error = LLMProviderError("fake provider failure")

    def fail(_request: LLMRequest) -> str:
        raise error

    with pytest.raises(LLMProviderError) as excinfo:
        generate_hypotheses(BUNDLE, FakeProvider(fail), max_output_tokens=10, timeout_seconds=1)
    assert excinfo.value is error


def test_empty_bundle_raises_before_provider_call() -> None:
    provider = FakeProvider([_raw(_hyp())])
    with pytest.raises(ValueError):
        generate_hypotheses(build_bundle([]), provider, max_output_tokens=10, timeout_seconds=1)
    assert provider.requests == []


def test_injection_bundle_end_to_end() -> None:
    bundle = build_bundle([_log(10, level="ERROR", message=INJECTION), _log(11, seconds=5)])
    hallucinated = str(uuid4())

    def respond(request: LLMRequest) -> str:
        ids = HEADER.findall(request.user)
        return _raw(_hyp(supporting=[*ids, hallucinated], claims=[_claim(ids)]))

    result = generate_hypotheses(
        bundle, FakeProvider(respond), max_output_tokens=100, timeout_seconds=1
    )
    cited = {i for h in result.hypotheses for i in h.supporting_evidence_ids}
    cited |= {i for h in result.hypotheses for c in h.claims for i in c.evidence_ids}
    assert cited == bundle.evidence_ids
    assert len(result.repairs) == 1
    assert result.repairs[0].kind == "unknown_evidence_id_removed"


def test_result_schemas_enforce_invariants() -> None:
    with pytest.raises(ValidationError):
        Claim(text="x", evidence_ids=[], status="verified")
    with pytest.raises(ValidationError):
        Claim(text="x", evidence_ids=[LOG.id], status="unverified")
    hypothesis_fields: dict[str, Any] = {
        "rank": 1,
        "title": "t",
        "explanation": "e",
        "claims": [],
        "supporting_evidence_ids": [LOG.id],
        "contradicting_evidence_ids": [],
        "uncertainty": "low",
        "uncertainty_notes": "",
        "follow_up_queries": [],
    }
    with pytest.raises(ValidationError):
        Hypothesis(**{**hypothesis_fields, "contradicting_evidence_ids": [LOG.id]})
    with pytest.raises(ValidationError):
        Hypothesis(**{**hypothesis_fields, "supporting_evidence_ids": []})
    first = Hypothesis(**hypothesis_fields)
    third = Hypothesis(**{**hypothesis_fields, "rank": 3})
    with pytest.raises(ValidationError):
        HypothesisResult(
            prompt_version="v",
            provider="fake",
            model="m",
            hypotheses=[first, third],
            unknowns=[],
            omitted_evidence_ids=[],
            repairs=[],
        )
    assert isinstance(UUID(str(first.supporting_evidence_ids[0])), UUID)
