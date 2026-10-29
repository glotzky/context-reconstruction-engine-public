"""Structured hypothesis generation over a bounded evidence bundle.

Model output is untrusted: its size is capped, it is parsed into strict schemas, and every
cited evidence ID is checked against the bundle in application code. Invalid citations are
removed and recorded in ``repairs``; malformed output is rejected. Prompts, evidence and raw
output are never logged or echoed in error messages.
"""

import re
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, ValidationError, model_validator

from cre.bundle import EvidenceBundle
from cre.evidence import NonEmptyStr, _Model
from cre.llm import LLMProvider, LLMProviderError, LLMRequest
from cre.prompts import HYPOTHESIS_SYSTEM_PROMPT

MAX_OUTPUT_CHARS = 100_000
_ID_DETAIL_MAX_CHARS = 64
_FENCE = re.compile(r"^```(?:json)?[ \t]*\n?(.*?)\n?[ \t]*```$", re.DOTALL)

ClaimStatus = Literal["verified", "unverified"]
Uncertainty = Literal["low", "medium", "high"]
RepairKind = Literal[
    "unknown_evidence_id_removed",
    "claim_downgraded",
    "conflicting_evidence_id_removed",
    "hypothesis_dropped_unsupported",
    "reranked",
]


class HypothesisOutputError(ValueError):
    """Model output was rejected. The message never contains the raw output."""


class RawClaim(_Model):
    text: Annotated[NonEmptyStr, Field(max_length=1000)]
    evidence_ids: Annotated[list[str], Field(max_length=20)]
    status: ClaimStatus


class RawHypothesis(_Model):
    rank: Annotated[int, Field(ge=1)]
    title: Annotated[NonEmptyStr, Field(max_length=200)]
    explanation: Annotated[str, Field(max_length=2000)]
    claims: Annotated[list[RawClaim], Field(min_length=1, max_length=20)]
    supporting_evidence_ids: Annotated[list[str], Field(max_length=50)]
    contradicting_evidence_ids: Annotated[list[str], Field(max_length=50)]
    uncertainty: Uncertainty
    uncertainty_notes: Annotated[str, Field(max_length=1000)]
    follow_up_queries: Annotated[
        list[Annotated[NonEmptyStr, Field(max_length=500)]], Field(max_length=10)
    ]


class RawHypothesisOutput(_Model):
    hypotheses: Annotated[list[RawHypothesis], Field(max_length=5)]
    unknowns: Annotated[list[str], Field(max_length=20)]


class Claim(_Model):
    text: str
    evidence_ids: list[UUID]
    status: ClaimStatus

    @model_validator(mode="after")
    def _status_matches_evidence(self) -> Self:
        if (self.status == "verified") != bool(self.evidence_ids):
            raise ValueError("a claim is verified exactly when it cites evidence")
        return self


class Hypothesis(_Model):
    rank: int
    title: str
    explanation: str
    claims: list[Claim]
    supporting_evidence_ids: Annotated[list[UUID], Field(min_length=1)]
    contradicting_evidence_ids: list[UUID]
    uncertainty: Uncertainty
    uncertainty_notes: str
    follow_up_queries: list[str]

    @model_validator(mode="after")
    def _disjoint_evidence(self) -> Self:
        if set(self.supporting_evidence_ids) & set(self.contradicting_evidence_ids):
            raise ValueError("supporting and contradicting evidence must be disjoint")
        return self


class RepairNote(_Model):
    kind: RepairKind
    hypothesis_title: str
    detail: str


class HypothesisResult(_Model):
    prompt_version: str
    provider: str
    model: str
    hypotheses: Annotated[list[Hypothesis], Field(min_length=1)]
    unknowns: list[str]
    omitted_evidence_ids: list[UUID]
    repairs: list[RepairNote]

    @model_validator(mode="after")
    def _ranks_contiguous(self) -> Self:
        if [h.rank for h in self.hypotheses] != list(range(1, len(self.hypotheses) + 1)):
            raise ValueError("hypothesis ranks must be exactly 1..n in order")
        return self


def _strip_fence(raw: str) -> str:
    text = raw.strip()
    match = _FENCE.match(text)
    return match.group(1).strip() if match else text


def _valid_ids(
    ids: list[str], allowed: frozenset[UUID], title: str, repairs: list[RepairNote]
) -> list[UUID]:
    result: list[UUID] = []
    for value in ids:
        try:
            parsed = UUID(value)
        except ValueError:
            parsed = None
        if parsed is None or parsed not in allowed:
            repairs.append(
                RepairNote(
                    kind="unknown_evidence_id_removed",
                    hypothesis_title=title,
                    detail=f"removed evidence ID not in bundle: {value[:_ID_DETAIL_MAX_CHARS]}",
                )
            )
        elif parsed not in result:
            result.append(parsed)
    return result


def _repair(
    raw: RawHypothesis, allowed: frozenset[UUID], repairs: list[RepairNote]
) -> Hypothesis | None:
    title = raw.title
    supporting = _valid_ids(raw.supporting_evidence_ids, allowed, title, repairs)
    contradicting = _valid_ids(raw.contradicting_evidence_ids, allowed, title, repairs)
    conflicts = set(supporting) & set(contradicting)
    for evidence_id in sorted(conflicts, key=str):
        repairs.append(
            RepairNote(
                kind="conflicting_evidence_id_removed",
                hypothesis_title=title,
                detail=f"removed {evidence_id} cited as both supporting and contradicting",
            )
        )
    supporting = [i for i in supporting if i not in conflicts]
    contradicting = [i for i in contradicting if i not in conflicts]
    claims: list[Claim] = []
    for claim in raw.claims:
        ids = _valid_ids(claim.evidence_ids, allowed, title, repairs)
        if claim.status == "verified" and not ids:
            repairs.append(
                RepairNote(
                    kind="claim_downgraded",
                    hypothesis_title=title,
                    detail="verified claim has no valid evidence IDs; marked unverified",
                )
            )
        claims.append(
            Claim(text=claim.text, evidence_ids=ids, status="verified" if ids else "unverified")
        )
    if not supporting:
        repairs.append(
            RepairNote(
                kind="hypothesis_dropped_unsupported",
                hypothesis_title=title,
                detail="hypothesis has no valid supporting evidence IDs; dropped",
            )
        )
        return None
    return Hypothesis(
        rank=raw.rank,
        title=title,
        explanation=raw.explanation,
        claims=claims,
        supporting_evidence_ids=supporting,
        contradicting_evidence_ids=contradicting,
        uncertainty=raw.uncertainty,
        uncertainty_notes=raw.uncertainty_notes,
        follow_up_queries=list(raw.follow_up_queries),
    )


def parse_and_validate(
    raw: str, bundle: EvidenceBundle, *, provider: str, model: str
) -> HypothesisResult:
    """Parse model output, check citations against the bundle and repair or reject it."""
    if len(raw) > MAX_OUTPUT_CHARS:
        raise HypothesisOutputError("model output exceeds MAX_OUTPUT_CHARS")
    try:
        output = RawHypothesisOutput.model_validate_json(_strip_fence(raw))
    except ValidationError as exc:
        raise HypothesisOutputError(
            f"model output failed schema validation ({exc.error_count()} errors)"
        ) from None

    allowed = bundle.evidence_ids
    repairs: list[RepairNote] = []
    kept: list[tuple[int, int, Hypothesis]] = []
    for index, raw_hypothesis in enumerate(output.hypotheses):
        hypothesis = _repair(raw_hypothesis, allowed, repairs)
        if hypothesis is not None:
            kept.append((hypothesis.rank, index, hypothesis))
    if not kept:
        raise HypothesisOutputError("model output has no hypothesis with valid supporting evidence")

    kept.sort(key=lambda item: (item[0], item[1]))
    old_ranks = [rank for rank, _, _ in kept]
    new_ranks = list(range(1, len(kept) + 1))
    if old_ranks != new_ranks:
        repairs.append(
            RepairNote(
                kind="reranked",
                hypothesis_title="",
                detail=f"ranks {old_ranks} renumbered to 1..{len(kept)}",
            )
        )
    hypotheses = [
        h.model_copy(update={"rank": n}) for n, (_, _, h) in zip(new_ranks, kept, strict=True)
    ]

    unknowns = list(output.unknowns)
    omitted = len(bundle.omitted_evidence_ids)
    if omitted:
        unknowns.append(
            f"{omitted} evidence items were omitted from the bundle to fit the budget "
            "and could not be considered."
        )
    return HypothesisResult(
        prompt_version=bundle.prompt_version,
        provider=provider,
        model=model,
        hypotheses=hypotheses,
        unknowns=unknowns,
        omitted_evidence_ids=list(bundle.omitted_evidence_ids),
        repairs=repairs,
    )


def generate_hypotheses(
    bundle: EvidenceBundle,
    provider: LLMProvider,
    *,
    max_output_tokens: int,
    timeout_seconds: float,
) -> HypothesisResult:
    """Call the provider once with the bundle and return validated hypotheses."""
    if not bundle.evidence:
        raise ValueError("bundle has no evidence to reason over")
    request = LLMRequest(
        system=HYPOTHESIS_SYSTEM_PROMPT,
        user=bundle.rendered,
        max_output_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
    )
    try:
        raw = provider.complete(request)
    except LLMProviderError:
        raise
    except Exception as exc:
        raise LLMProviderError("LLM provider call failed") from exc
    return parse_and_validate(raw, bundle, provider=provider.name, model=provider.model)
