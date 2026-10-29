"""Upper-bound cost estimate and a per-call spend guard for real-provider runs.

Uses a conservative ``ceil(chars / 3)`` token proxy. Never logs requests or responses.
Must not import labels.
"""

import math
from collections.abc import Callable, Sequence

from cre.benchmark.runner import Mode, baseline_evidence
from cre.benchmark.scenarios import ScenarioInput
from cre.config import Settings
from cre.evidence import _Model
from cre.llm import LLMProvider, LLMProviderError, LLMRequest, compute_cost

CHARS_PER_TOKEN_ESTIMATE = 3
PROMPT_OVERHEAD_CHARS = 20_000
SPEND_CAP_MESSAGE = "benchmark spend cap reached"
OVER_CAP_WARNING = (
    "estimate exceeds --max-usd; the run will stop at the cap and keep partial results"
)


class CaseEstimate(_Model):
    case_id: str
    mode: Mode
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


class RunEstimate(_Model):
    cases: list[CaseEstimate]
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    max_usd: float


def calls_per_case(mode: Mode, settings: Settings) -> int:
    return 1 + 2 * settings.investigation_max_iterations if mode == "engine" else 1


def _tokens(chars: int) -> int:
    return math.ceil(chars / CHARS_PER_TOKEN_ESTIMATE)


def estimate_case(
    scenario: ScenarioInput, mode: Mode, settings: Settings, *, baseline_window_seconds: int
) -> CaseEstimate:
    evidence = (
        scenario.evidence
        if mode == "engine"
        else baseline_evidence(scenario, settings, baseline_window_seconds)
    )
    evidence_chars = sum(len(item.model_dump_json()) for item in evidence)
    per_call_input = _tokens(
        PROMPT_OVERHEAD_CHARS + min(evidence_chars, settings.llm_bundle_max_chars)
    )
    calls = calls_per_case(mode, settings)
    input_tokens = per_call_input * calls
    output_tokens = settings.llm_max_output_tokens * calls
    return CaseEstimate(
        case_id=scenario.case_id,
        mode=mode,
        calls=calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=compute_cost(
            input_tokens,
            output_tokens,
            settings.llm_price_input_per_mtok,
            settings.llm_price_output_per_mtok,
        ),
    )


def estimate_run(
    scenarios: Sequence[ScenarioInput],
    modes: Sequence[Mode],
    settings: Settings,
    *,
    baseline_window_seconds: int,
    max_usd: float,
) -> RunEstimate:
    cases = [
        estimate_case(s, mode, settings, baseline_window_seconds=baseline_window_seconds)
        for mode in modes
        for s in scenarios
    ]
    return RunEstimate(
        cases=cases,
        calls=sum(c.calls for c in cases),
        input_tokens=sum(c.input_tokens for c in cases),
        output_tokens=sum(c.output_tokens for c in cases),
        cost_usd=sum(c.cost_usd for c in cases),
        max_usd=max_usd,
    )


def render_estimate(estimate: RunEstimate, model: str) -> str:
    modes = list(dict.fromkeys(c.mode for c in estimate.cases))
    scenarios = len({c.case_id for c in estimate.cases})
    lines = [
        f"model: {model}",
        f"cases: {scenarios} x {len(modes)} modes ({', '.join(modes)})",
        f"calls (max): {estimate.calls}",
        f"input tokens (max): {estimate.input_tokens}",
        f"output tokens (max): {estimate.output_tokens}",
        f"estimated upper-bound cost: ${estimate.cost_usd:.4f}",
        f"spend cap (--max-usd): ${estimate.max_usd:.4f}",
    ]
    if estimate.cost_usd > estimate.max_usd:
        lines.append(f"warning: {OVER_CAP_WARNING}")
    return "\n".join(lines)


class SpendCapReached(LLMProviderError):
    def __init__(self) -> None:
        super().__init__(SPEND_CAP_MESSAGE)


class SpendGuard:
    """Wraps a real provider; refuses any call whose worst-case cost would exceed the cap."""

    def __init__(
        self,
        inner: LLMProvider,
        total_cost: Callable[[], float],
        *,
        max_usd: float,
        price_input_per_mtok: float,
        price_output_per_mtok: float,
    ) -> None:
        self._inner = inner
        self._total_cost = total_cost
        self._baseline = total_cost()
        self._max_usd = max_usd
        self._price_input = price_input_per_mtok
        self._price_output = price_output_per_mtok
        self.name = inner.name
        self.model = inner.model
        self.cap_reached = False

    @property
    def spent(self) -> float:
        return self._total_cost() - self._baseline

    def complete(self, request: LLMRequest) -> str:
        if self.cap_reached:
            raise SpendCapReached()
        worst = compute_cost(
            _tokens(len(request.system) + len(request.user)),
            request.max_output_tokens,
            self._price_input,
            self._price_output,
        )
        if self.spent + worst > self._max_usd:
            self.cap_reached = True
            raise SpendCapReached()
        return self._inner.complete(request)
