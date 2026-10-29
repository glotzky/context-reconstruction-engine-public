import math
from pathlib import Path

import pytest

from cre import prompts
from cre.benchmark.cost import (
    CHARS_PER_TOKEN_ESTIMATE,
    OVER_CAP_WARNING,
    PROMPT_OVERHEAD_CHARS,
    SpendCapReached,
    SpendGuard,
    calls_per_case,
    estimate_case,
    estimate_run,
    render_estimate,
)
from cre.benchmark.runner import baseline_evidence
from cre.benchmark.scenarios import ScenarioInput, load_input
from cre.config import Settings
from cre.llm import FakeProvider, LLMProviderError, LLMRequest, compute_cost
from tests.benchmark_fixtures import write_scenario

SETTINGS = Settings(
    llm_api_key="sk-fake-SENTINEL",
    llm_model="stub-model",
    llm_price_input_per_mtok=1,
    llm_price_output_per_mtok=1,
)


@pytest.fixture
def scenario(tmp_path: Path) -> ScenarioInput:
    write_scenario(tmp_path)
    return load_input(tmp_path / "inputs" / "case-01.json", tmp_path)


def test_calls_per_case() -> None:
    assert calls_per_case("engine", SETTINGS) == 1 + 2 * SETTINGS.investigation_max_iterations
    five = SETTINGS.model_copy(update={"investigation_max_iterations": 5})
    assert calls_per_case("engine", five) == 11
    assert calls_per_case("baseline", SETTINGS) == 1


@pytest.mark.parametrize("mode", ["engine", "baseline"])
def test_estimate_case_matches_hand_computation(scenario: ScenarioInput, mode: str) -> None:
    items = scenario.evidence if mode == "engine" else baseline_evidence(scenario, SETTINGS, 300)
    chars = sum(len(item.model_dump_json()) for item in items)
    calls = 7 if mode == "engine" else 1
    per_call = math.ceil((PROMPT_OVERHEAD_CHARS + chars) / 3)
    estimate = estimate_case(scenario, mode, SETTINGS, baseline_window_seconds=300)  # type: ignore[arg-type]
    assert estimate.case_id == "case-01" and estimate.mode == mode
    assert estimate.calls == calls
    assert estimate.input_tokens == per_call * calls
    assert estimate.output_tokens == SETTINGS.llm_max_output_tokens * calls
    expected = (per_call * calls + SETTINGS.llm_max_output_tokens * calls) / 1_000_000
    assert estimate.cost_usd == pytest.approx(expected)


def test_baseline_estimate_uses_windowed_evidence(scenario: ScenarioInput) -> None:
    engine = estimate_case(scenario, "engine", SETTINGS, baseline_window_seconds=300)
    baseline = estimate_case(scenario, "baseline", SETTINGS, baseline_window_seconds=300)
    assert baseline.input_tokens < engine.input_tokens / engine.calls


def test_evidence_chars_are_capped(scenario: ScenarioInput) -> None:
    small = SETTINGS.model_copy(
        update={"llm_bundle_max_chars": 1000, "llm_bundle_max_item_chars": 200}
    )
    assert sum(len(item.model_dump_json()) for item in scenario.evidence) > 1000
    estimate = estimate_case(scenario, "baseline", small, baseline_window_seconds=300)
    assert estimate.input_tokens == math.ceil(
        (PROMPT_OVERHEAD_CHARS + 1000) / CHARS_PER_TOKEN_ESTIMATE
    )


def test_estimate_run_totals_and_render(scenario: ScenarioInput) -> None:
    run = estimate_run(
        [scenario, scenario.model_copy(update={"case_id": "case-02"})],
        ["engine", "baseline"],
        SETTINGS,
        baseline_window_seconds=300,
        max_usd=10.0,
    )
    assert len(run.cases) == 4
    assert [c.mode for c in run.cases] == ["engine", "engine", "baseline", "baseline"]
    assert run.calls == sum(c.calls for c in run.cases)
    assert run.input_tokens == sum(c.input_tokens for c in run.cases)
    assert run.output_tokens == sum(c.output_tokens for c in run.cases)
    assert run.cost_usd == pytest.approx(sum(c.cost_usd for c in run.cases))
    assert run.max_usd == 10.0
    text = render_estimate(run, "stub-model")
    assert "stub-model" in text and f"${run.cost_usd:.4f}" in text and "$10.0000" in text
    assert OVER_CAP_WARNING not in text
    assert "fake request failed" not in text and "SENTINEL" not in text
    over = render_estimate(run.model_copy(update={"max_usd": run.cost_usd / 2}), "stub-model")
    assert OVER_CAP_WARNING in over


def test_prompt_overhead_covers_system_prompts() -> None:
    for name in (
        "HYPOTHESIS_SYSTEM_PROMPT",
        "TOOL_REQUEST_SYSTEM_PROMPT",
        "REVISION_SYSTEM_PROMPT",
    ):
        assert PROMPT_OVERHEAD_CHARS >= len(getattr(prompts, name))


class CostingProvider:
    name = "anthropic"
    model = "stub-model"

    def __init__(self, cost_per_call: float) -> None:
        self.cost_per_call = cost_per_call
        self.cost = 0.0
        self.calls = 0

    def complete(self, request: LLMRequest) -> str:
        self.calls += 1
        self.cost += self.cost_per_call
        return "fake answer"


REQUEST = LLMRequest(system="s" * 300, user="u" * 300, max_output_tokens=1000, timeout_seconds=5)
WORST = compute_cost(200, 1000, 1, 1)


def _guard(inner: CostingProvider, max_usd: float) -> SpendGuard:
    return SpendGuard(
        inner,
        lambda: inner.cost,
        max_usd=max_usd,
        price_input_per_mtok=1,
        price_output_per_mtok=1,
    )


def test_spend_guard_delegates_and_tracks_spent() -> None:
    inner = CostingProvider(0.001)
    inner.cost = 5.0
    guard = _guard(inner, 1.0)
    assert (guard.name, guard.model) == ("anthropic", "stub-model")
    assert guard.spent == 0.0
    assert guard.complete(REQUEST) == "fake answer"
    assert guard.complete(REQUEST) == "fake answer"
    assert inner.calls == 2 and guard.spent == pytest.approx(0.002)
    assert not guard.cap_reached


def test_spend_guard_works_with_fake_provider() -> None:
    guard = SpendGuard(
        FakeProvider(["one"], model="m"),
        lambda: 0.0,
        max_usd=1.0,
        price_input_per_mtok=1,
        price_output_per_mtok=1,
    )
    assert guard.complete(REQUEST) == "one" and guard.name == "fake"


def test_spend_guard_refuses_call_over_cap() -> None:
    inner = CostingProvider(0.004)
    guard = _guard(inner, 0.004 + WORST / 2)
    assert guard.complete(REQUEST) == "fake answer"
    with pytest.raises(SpendCapReached):
        guard.complete(REQUEST)
    assert inner.calls == 1 and guard.cap_reached
    tiny = LLMRequest(system="s", user="u", max_output_tokens=1, timeout_seconds=5)
    with pytest.raises(SpendCapReached):
        guard.complete(tiny)
    assert inner.calls == 1


def test_spend_guard_refuses_first_call_when_worst_case_exceeds_cap() -> None:
    inner = CostingProvider(0.0)
    guard = _guard(inner, WORST / 2)
    with pytest.raises(SpendCapReached):
        guard.complete(REQUEST)
    assert inner.calls == 0 and guard.cap_reached


def test_spend_cap_reached_is_provider_error() -> None:
    error = SpendCapReached()
    assert isinstance(error, LLMProviderError)
    assert str(error) == "benchmark spend cap reached"
