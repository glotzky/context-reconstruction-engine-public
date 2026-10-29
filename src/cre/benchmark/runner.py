"""Run one scenario through the engine or the single-call baseline, fully in memory.

The runners receive investigator input only, never ground-truth labels. Nothing here logs or
prints prompts, evidence or model output. This module must not import labels.
"""

import math
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from cre.benchmark.scenarios import ScenarioInput
from cre.bundle import BundleBudgetError, BundleLimits, build_bundle
from cre.config import Settings
from cre.evidence import EventKind, Evidence, _Model
from cre.hypotheses import HypothesisOutputError, HypothesisResult, generate_hypotheses
from cre.investigator import ToolExecutor, investigate
from cre.llm import LLMProvider, LLMProviderError, LLMRequest
from cre.retrieval import EvidenceQuery, EvidenceQueryResult, resolve_query

Mode = Literal["engine", "baseline"]


class TokenMeter:
    """Wraps a provider; counts calls and proxy tokens ``ceil(chars / 4)``."""

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner
        self.name = inner.name
        self.model = inner.model
        self.calls = 0
        self.approx_tokens = 0

    def complete(self, request: LLMRequest) -> str:
        self.calls += 1
        chars = len(request.system) + len(request.user)
        try:
            response = self._inner.complete(request)
        except BaseException:
            self.approx_tokens += math.ceil(chars / 4)
            raise
        self.approx_tokens += math.ceil((chars + len(response)) / 4)
        return response


def _select(
    evidence: Sequence[Evidence],
    *,
    start: datetime,
    end: datetime,
    trace_id: str | None = None,
    service: str | None = None,
    kinds: frozenset[EventKind] | None = None,
) -> list[Evidence]:
    matched = [
        item
        for item in evidence
        if (trace_id is None or item.trace_id == trace_id)
        and (service is None or item.service == service)
        and (kinds is None or item.kind in kinds)
        and start <= item.timestamp.astimezone(UTC) <= end
    ]
    return sorted(matched, key=lambda item: (item.timestamp.astimezone(UTC), item.id))


def make_memory_executor(evidence: Sequence[Evidence], settings: Settings) -> ToolExecutor:
    """Return an executor with ``retrieve_evidence`` semantics over an in-memory list."""
    items = list(evidence)

    def execute(query: EvidenceQuery) -> EvidenceQueryResult:
        resolved = resolve_query(query, settings)
        matched = _select(
            items,
            start=resolved.start,
            end=resolved.end,
            trace_id=query.trace_id,
            service=query.service,
            kinds=query.kinds,
        )[: resolved.limit + 1]
        kept = matched[: resolved.limit]
        return EvidenceQueryResult(
            trace_id=query.trace_id,
            service=query.service,
            anchor=query.anchor.astimezone(UTC),
            start=resolved.start,
            end=resolved.end,
            limit=resolved.limit,
            count=len(kept),
            truncated=len(matched) > resolved.limit,
            items=kept,
        )

    return execute


class CaseOutcome(_Model):
    case_id: str
    mode: Mode
    provider: str
    model: str
    stop_reason: str
    error_detail: str | None
    hypotheses: HypothesisResult | None
    available_evidence_ids: list[UUID]
    iterations_used: int
    llm_calls: int
    approx_tokens: int
    latency_seconds: float


def run_engine_case(
    scenario: ScenarioInput,
    provider: LLMProvider,
    settings: Settings,
    *,
    clock: Callable[[], float] = time.perf_counter,
) -> CaseOutcome:
    meter = TokenMeter(provider)
    started = clock()
    result = investigate(
        scenario.initial_evidence(),
        anchor=scenario.anchor,
        provider=meter,
        executor=make_memory_executor(scenario.evidence, settings),
        settings=settings,
    )
    latency = clock() - started
    return CaseOutcome(
        case_id=scenario.case_id,
        mode="engine",
        provider=meter.name,
        model=meter.model,
        stop_reason=result.stop_reason,
        error_detail=result.error_detail,
        hypotheses=result.hypotheses,
        available_evidence_ids=list(result.evidence_ids),
        iterations_used=result.iterations_used,
        llm_calls=meter.calls,
        approx_tokens=meter.approx_tokens,
        latency_seconds=latency,
    )


def baseline_evidence(
    scenario: ScenarioInput, settings: Settings, window_seconds: int
) -> list[Evidence]:
    """Initial evidence first, then every item in ``anchor ± window_seconds`` (deduplicated)."""
    if window_seconds < 0 or window_seconds > settings.retrieval_max_window_seconds:
        raise ValueError("baseline window is out of range")
    anchor = scenario.anchor.astimezone(UTC)
    window = timedelta(seconds=window_seconds)
    windowed = _select(scenario.evidence, start=anchor - window, end=anchor + window)
    result: list[Evidence] = []
    seen: set[UUID] = set()
    for item in [*scenario.initial_evidence(), *windowed[: settings.retrieval_max_limit]]:
        if item.id not in seen:
            seen.add(item.id)
            result.append(item)
    return result


def run_baseline_case(
    scenario: ScenarioInput,
    provider: LLMProvider,
    settings: Settings,
    *,
    window_seconds: int,
    clock: Callable[[], float] = time.perf_counter,
) -> CaseOutcome:
    meter = TokenMeter(provider)
    evidence = baseline_evidence(scenario, settings, window_seconds)
    limits = BundleLimits(
        max_items=settings.llm_bundle_max_items,
        max_chars=settings.llm_bundle_max_chars,
        max_item_chars=settings.llm_bundle_max_item_chars,
    )
    hypotheses: HypothesisResult | None = None
    available: list[UUID] = []
    error_detail: str | None = None
    started = clock()
    try:
        bundle = build_bundle(evidence, limits)
        available = [item.id for item in bundle.evidence]
        hypotheses = generate_hypotheses(
            bundle,
            meter,
            max_output_tokens=settings.llm_max_output_tokens,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    except BundleBudgetError:
        error_detail = "evidence bundle over budget"
    except (LLMProviderError, HypothesisOutputError):
        error_detail = "baseline hypothesis generation failed"
    latency = clock() - started
    return CaseOutcome(
        case_id=scenario.case_id,
        mode="baseline",
        provider=meter.name,
        model=meter.model,
        stop_reason="error" if error_detail else "concluded",
        error_detail=error_detail,
        hypotheses=hypotheses,
        available_evidence_ids=available,
        iterations_used=0,
        llm_calls=meter.calls,
        approx_tokens=meter.approx_tokens,
        latency_seconds=latency,
    )
