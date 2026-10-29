import ast
import inspect
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import cre.benchmark
from cre.benchmark.cli import LoadedCase, run_benchmark
from cre.benchmark.fake import make_fake_provider
from cre.benchmark.runner import (
    Mode,
    TokenMeter,
    make_memory_executor,
    run_baseline_case,
    run_engine_case,
)
from cre.benchmark.scenarios import ScenarioInput
from cre.config import Settings
from cre.llm import FakeProvider, LLMProvider, LLMRequest
from cre.retrieval import EvidenceQuery, RetrievalQueryError
from tests.benchmark_fixtures import (
    SERVICE,
    TRACE,
    fake_doc,
    input_doc,
    labels_doc,
    uid,
    write_scenario,
)

SETTINGS = Settings()
ANCHOR = datetime(2026, 1, 1, tzinfo=UTC)
SCENARIO = ScenarioInput.model_validate(input_doc())


def _ids(items: list[object]) -> list[str]:
    return [str(getattr(item, "id")) for item in items]  # noqa: B009


def _query(**fields: object) -> EvidenceQuery:
    return EvidenceQuery.model_validate({"anchor": ANCHOR, **fields})


def test_memory_executor_filters_and_orders() -> None:
    execute = make_memory_executor(SCENARIO.evidence, SETTINGS)
    by_service = execute(_query(service=SERVICE, window_before=timedelta(seconds=900)))
    assert _ids(by_service.items) == [uid(2), uid(3), uid(1), uid(4)]
    assert by_service.count == 4 and not by_service.truncated
    assert by_service.start == ANCHOR - timedelta(seconds=900)
    assert by_service.anchor == ANCHOR
    by_trace = execute(_query(trace_id=TRACE))
    assert _ids(by_trace.items) == [uid(1), uid(4)]
    changes = execute(_query(service=SERVICE, kinds=frozenset({"config_change"})))
    assert _ids(changes.items) == [uid(2)]
    other = execute(_query(service="fake-other"))
    assert _ids(other.items) == [uid(5)]


def test_memory_executor_window_bounds_are_inclusive() -> None:
    execute = make_memory_executor(SCENARIO.evidence, SETTINGS)
    at_edge = execute(
        _query(service=SERVICE, window_before=timedelta(seconds=600), window_after=timedelta(0))
    )
    assert uid(2) in _ids(at_edge.items) and uid(1) in _ids(at_edge.items)
    past_edge = execute(_query(service=SERVICE, window_before=timedelta(seconds=599)))
    assert uid(2) not in _ids(past_edge.items)


def test_memory_executor_truncates_at_limit() -> None:
    execute = make_memory_executor(SCENARIO.evidence, SETTINGS)
    result = execute(_query(service=SERVICE, limit=2))
    assert result.truncated and result.count == 2 and result.limit == 2
    assert _ids(result.items) == [uid(2), uid(3)]


def test_memory_executor_rejects_oversized_window() -> None:
    execute = make_memory_executor(SCENARIO.evidence, SETTINGS)
    too_big = timedelta(seconds=SETTINGS.retrieval_max_window_seconds + 1)
    with pytest.raises(RetrievalQueryError):
        execute(_query(service=SERVICE, window_before=too_big))


def test_token_meter() -> None:
    inner = FakeProvider(["abcde"], model="m-1")
    meter = TokenMeter(inner)
    assert (meter.name, meter.model) == ("fake", "m-1")
    request = LLMRequest(system="abc", user="defg", max_output_tokens=10, timeout_seconds=1)
    assert meter.complete(request) == "abcde"
    assert meter.calls == 1 and meter.approx_tokens == math.ceil(12 / 4)
    with pytest.raises(Exception, match="no more responses"):
        meter.complete(request)
    assert meter.calls == 2 and meter.approx_tokens == 3 + math.ceil(7 / 4)


def _engine_provider() -> FakeProvider:
    from cre.benchmark.fake import FakeScript

    return make_fake_provider(FakeScript.model_validate(fake_doc()), "engine", "fake-model")


def test_engine_run() -> None:
    clock = iter([10.0, 12.5]).__next__
    outcome = run_engine_case(SCENARIO, _engine_provider(), SETTINGS, clock=clock)
    assert outcome.stop_reason == "concluded" and outcome.error_detail is None
    assert outcome.mode == "engine" and outcome.case_id == "case-01"
    assert {uid(1), uid(2), uid(3), uid(4)} <= set(map(str, outcome.available_evidence_ids))
    assert uid(5) not in map(str, outcome.available_evidence_ids)
    assert outcome.llm_calls == 4 and outcome.approx_tokens > 0
    assert outcome.iterations_used == 2
    assert outcome.latency_seconds == 2.5
    assert outcome.hypotheses is not None
    assert outcome.hypotheses.hypotheses[0].title == "Connection pool was reduced"


def test_engine_run_with_exhausted_script_errors() -> None:
    outcome = run_engine_case(SCENARIO, FakeProvider([]), SETTINGS)
    assert outcome.stop_reason == "error" and outcome.hypotheses is None


def _baseline_provider() -> FakeProvider:
    from cre.benchmark.fake import FakeScript

    return make_fake_provider(FakeScript.model_validate(fake_doc()), "baseline", "fake-model")


def test_baseline_run() -> None:
    provider = _baseline_provider()
    clock = iter([1.0, 1.25]).__next__
    outcome = run_baseline_case(SCENARIO, provider, SETTINGS, window_seconds=300, clock=clock)
    assert outcome.stop_reason == "concluded" and outcome.mode == "baseline"
    available = set(map(str, outcome.available_evidence_ids))
    assert available == {uid(1), uid(3), uid(4), uid(5)}
    assert uid(2) not in available
    assert len(provider.requests) == 1 and outcome.llm_calls == 1
    assert outcome.iterations_used == 0 and outcome.latency_seconds == 0.25
    assert outcome.hypotheses is not None


def test_baseline_zero_window_keeps_initial_evidence() -> None:
    outcome = run_baseline_case(SCENARIO, _baseline_provider(), SETTINGS, window_seconds=0)
    assert set(map(str, outcome.available_evidence_ids)) == {uid(1), uid(4)}


def test_baseline_rejects_out_of_range_window() -> None:
    with pytest.raises(ValueError):
        run_baseline_case(SCENARIO, _baseline_provider(), SETTINGS, window_seconds=-1)


@pytest.mark.parametrize("provider", [FakeProvider([]), FakeProvider(["not json"])])
def test_baseline_failure(provider: FakeProvider) -> None:
    outcome = run_baseline_case(SCENARIO, provider, SETTINGS, window_seconds=300)
    assert outcome.stop_reason == "error"
    assert outcome.error_detail == "baseline hypothesis generation failed"
    assert outcome.hypotheses is None and outcome.llm_calls == 1


LABEL_SENTINEL = "ZZ-LABEL-SENTINEL"


def test_labels_never_reach_the_provider(tmp_path: Path) -> None:
    labels = labels_doc()
    labels["root_cause_id"] = LABEL_SENTINEL
    labels["root_cause_summary"] = f"summary {LABEL_SENTINEL}"
    labels["accepted_keywords"] = [LABEL_SENTINEL]
    write_scenario(tmp_path, labels=labels)
    providers: list[FakeProvider] = []

    def factory(case: LoadedCase, mode: Mode) -> LLMProvider:
        provider = make_fake_provider(case.script, mode, "fake-model")
        providers.append(provider)
        return provider

    run = run_benchmark(tmp_path, SETTINGS, modes=["engine", "baseline"], provider_factory=factory)
    requests = [r for p in providers for r in p.requests]
    assert len(requests) == 5
    for request in requests:
        assert LABEL_SENTINEL not in request.system
        assert LABEL_SENTINEL not in request.user
    assert all(c.stop_reason == "concluded" for c in run.cases)


@pytest.mark.parametrize("module", ["runner.py", "scenarios.py", "fake.py"])
def test_investigator_side_modules_do_not_import_labels(module: str) -> None:
    source = (Path(cre.benchmark.__file__).parent / module).read_text("utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            assert all("labels" not in alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert "labels" not in (node.module or "")
            assert all(alias.name != "ScenarioLabels" for alias in node.names)
    assert "ScenarioLabels" not in source


@pytest.mark.parametrize("func", [run_engine_case, run_baseline_case])
def test_runner_signatures_have_no_labels(func: object) -> None:
    params = inspect.signature(func).parameters  # type: ignore[arg-type]
    assert all("label" not in name for name in params)
    assert all("Label" not in str(p.annotation) for p in params.values())
