import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from cre.benchmark import BENCHMARK_VERSION, DEFAULT_DATA_DIR
from cre.benchmark.cli import (
    CONFIRM_PROMPT,
    FAKE_NOTE,
    BenchmarkRun,
    RealProvider,
    main,
    render_table,
    run_benchmark,
)
from cre.config import Settings
from cre.llm import LLMRequest, compute_cost
from cre.llm_anthropic import AnthropicProvider
from cre.prompts import HYPOTHESIS_PROMPT_VERSION, INVESTIGATION_PROMPT_VERSION
from tests.benchmark_fixtures import (
    SENTINEL,
    engine_script,
    fake_doc,
    hypothesis,
    input_doc,
    write_scenario,
)

SETTINGS = Settings()


@pytest.fixture(scope="module")
def shipped_run() -> BenchmarkRun:
    return run_benchmark(DEFAULT_DATA_DIR, SETTINGS, modes=["engine", "baseline"])


def test_shipped_run_concludes_and_records_versions(shipped_run: BenchmarkRun) -> None:
    run = shipped_run
    assert len(run.cases) == 60
    assert [c.stop_reason for c in run.cases] == ["concluded"] * 60
    assert run.benchmark_version == BENCHMARK_VERSION == "benchmark-v2"
    assert run.results_schema_version == 2
    assert run.stopped_reason == "completed" and run.skipped == []
    assert run.max_usd is None and run.estimate is None and run.total_cost_usd is None
    assert run.split_sizes == {"dev": 20, "heldout": 10}
    assert len(run.manifest_sha256) == 64
    assert run.provider == "fake" and run.model == "fake-model" and run.fake_responses
    assert run.modes == ["engine", "baseline"]
    assert run.prompt_versions == {
        "hypotheses": HYPOTHESIS_PROMPT_VERSION,
        "investigation": INVESTIGATION_PROMPT_VERSION,
    }
    assert run.code_version == SETTINGS.version and run.commit == SETTINGS.commit
    assert len(run.scenarios) == 30
    for version in run.scenarios:
        for digest in (version.input_sha256, version.labels_sha256, version.fake_sha256):
            assert len(digest) == 64 and int(digest, 16) >= 0
    assert len(run.scenario_set_sha256) == 64
    assert [(s.mode, s.split) for s in run.summaries] == [
        ("engine", "dev"),
        ("engine", "heldout"),
        ("baseline", "dev"),
        ("baseline", "heldout"),
    ]
    for summary in run.summaries:
        assert summary.cases == (20 if summary.split == "dev" else 10) and summary.errors == 0
        assert summary.latency_seconds.mean is not None
        assert summary.approx_tokens_total > 0
        assert summary.cost_usd is None
    assert run.config["baseline_window_seconds"] == 300
    assert run.started_at <= run.finished_at
    for case in run.cases:
        assert case.provider == "fake" and case.model == "fake-model"
        assert case.prompt_versions == run.prompt_versions and case.cost_usd is None


def test_shipped_run_abstains_on_every_unanswerable_case(shipped_run: BenchmarkRun) -> None:
    unanswerable = [c for c in shipped_run.cases if not c.answerable]
    assert len(unanswerable) == 14
    assert all(c.abstained and c.abstention_correct for c in unanswerable)
    counts = {(s.mode, s.split): s.abstention.unanswerable for s in shipped_run.summaries}
    for mode in ("engine", "baseline"):
        assert counts[(mode, "dev")] == 4 and counts[(mode, "heldout")] == 3
    engine = [c for c in shipped_run.cases if c.mode == "engine" and c.answerable]
    assert all(c.top1_correct for c in engine)


def test_config_snapshot_excludes_secrets(tmp_path: Path) -> None:
    write_scenario(tmp_path)
    settings = Settings(llm_api_key="sk-fake-SENTINEL", database_url="postgresql://u:p@h/db")
    dumped = run_benchmark(tmp_path, settings, modes=["baseline"]).model_dump_json()
    assert "SENTINEL" not in dumped and "sk-fake" not in dumped
    assert "postgresql://" not in dumped
    assert "llm_api_key" not in dumped and "database_url" not in dumped


def test_scenario_set_hash_tracks_file_bytes(tmp_path: Path) -> None:
    write_scenario(tmp_path)
    first = run_benchmark(tmp_path, SETTINGS, modes=["baseline"])
    second = run_benchmark(tmp_path, SETTINGS, modes=["baseline"])
    assert first.scenario_set_sha256 == second.scenario_set_sha256
    fake = tmp_path / "fake" / "case-01.json"
    fake.write_text(fake.read_text("utf-8") + "\n", encoding="utf-8")
    third = run_benchmark(tmp_path, SETTINGS, modes=["baseline"])
    assert third.scenario_set_sha256 != first.scenario_set_sha256
    assert third.scenarios[0].fake_sha256 != first.scenarios[0].fake_sha256
    assert third.scenarios[0].input_sha256 == first.scenarios[0].input_sha256


def test_main_writes_run_record(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = write_scenario(tmp_path / "scenarios")
    out = tmp_path / "run.json"
    assert main(["--scenarios", str(root), "--output", str(out), "--mode", "engine"]) == 0
    run = BenchmarkRun.model_validate_json(out.read_text("utf-8"))
    assert run.modes == ["engine"] and len(run.cases) == 1
    stdout = capsys.readouterr().out
    assert stdout.splitlines()[0] == FAKE_NOTE
    for name in (
        "top1_accuracy",
        "evidence_coverage",
        "unsupported_claim_rate",
        "abstention_accuracy",
        "latency_mean_s",
        "approx_tokens",
        "cost_usd",
    ):
        assert name in stdout
    assert "fake request failed" not in stdout


def test_main_malformed_scenarios(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc = input_doc()
    doc["anchor"] = SENTINEL
    write_scenario(tmp_path, inputs=doc)
    assert main(["--scenarios", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("error: inputs/case-01.json: anchor")
    assert SENTINEL not in captured.err and captured.out == ""


@pytest.mark.parametrize(
    "argv",
    [
        ["--baseline-window-seconds", "-1"],
        ["--baseline-window-seconds", "999999999"],
        ["--usd-per-1k-tokens", "-0.5"],
        ["--mode", "other"],
        ["--provider", "other"],
        ["--max-usd", "0"],
        ["--max-usd", "-1"],
        ["--max-usd", "inf"],
        ["--max-usd", "nan"],
    ],
)
def test_main_rejects_bad_arguments(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main(argv)
    assert info.value.code == 2


def test_cost_uses_price(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = write_scenario(tmp_path / "scenarios")
    out = tmp_path / "run.json"
    assert main(["--scenarios", str(root), "--output", str(out), "--usd-per-1k-tokens", "0.5"]) == 0
    record = json.loads(out.read_text("utf-8"))
    assert len(record["summaries"]) == 4
    for case in record["cases"]:
        assert case["cost_usd"] == pytest.approx(case["approx_tokens"] / 1000 * 0.5)
    for summary in record["summaries"]:
        cases = [
            c
            for c in record["cases"]
            if c["mode"] == summary["mode"] and c["split"] == summary["split"]
        ]
        if cases:
            assert summary["cost_usd"] == pytest.approx(sum(c["cost_usd"] for c in cases))
        else:
            assert summary["cost_usd"] is None
    assert record["total_cost_usd"] == pytest.approx(sum(c["cost_usd"] for c in record["cases"]))
    assert record["config"]["usd_per_1k_tokens"] == 0.5


KEY = "sk-fake-SENTINEL"
REAL = Settings(
    llm_api_key=KEY,
    llm_model="stub-model",
    llm_price_input_per_mtok=1,
    llm_price_output_per_mtok=1,
)


def _no_factory(settings: Settings) -> RealProvider:
    raise AssertionError("real provider must not be constructed")


class StubProvider:
    """Scripted responses for N fixture cases in mode-major order; fixed cost per call."""

    name = "anthropic"
    model = "stub-model"

    def __init__(
        self, cases: int, *, cost_per_call: float = 0.01, interrupt_at: int | None = None
    ) -> None:
        engine = [json.dumps(item) for item in engine_script()] * cases
        baseline = [json.dumps(item) for item in fake_doc()["baseline"]] * cases
        self.responses = engine + baseline
        self.cost_per_call = cost_per_call
        self.interrupt_at = interrupt_at
        self.cost = 0.0
        self.calls = 0
        self.closed = False

    def complete(self, request: LLMRequest) -> str:
        self.calls += 1
        if self.interrupt_at is not None and self.calls == self.interrupt_at:
            raise KeyboardInterrupt
        self.cost += self.cost_per_call
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def _stub_factory(stub: StubProvider) -> Callable[[Settings], RealProvider]:
    def factory(settings: Settings) -> RealProvider:
        assert settings.llm_provider == "anthropic"
        return RealProvider(stub, lambda: stub.cost, stub.close)

    return factory


def _scenarios(root: Path, count: int) -> Path:
    for n in range(1, count + 1):
        write_scenario(root, f"case-{n:02d}")
    return root


def _real_main(
    argv: list[str],
    stub: StubProvider | None = None,
    *,
    settings: Settings = REAL,
    **kwargs: Any,
) -> int:
    factory = _stub_factory(stub) if stub is not None else _no_factory
    return main(
        ["--provider", "anthropic", *argv],
        settings=settings,
        real_provider_factory=factory,
        **kwargs,
    )


def test_default_provider_is_fake_even_if_env_says_anthropic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CRE_LLM_PROVIDER", "anthropic")
    root = write_scenario(tmp_path / "scenarios")
    out = tmp_path / "run.json"
    assert (
        main(["--scenarios", str(root), "--output", str(out)], real_provider_factory=_no_factory)
        == 0
    )
    run = BenchmarkRun.model_validate_json(out.read_text("utf-8"))
    assert run.provider == "fake" and run.fake_responses
    assert capsys.readouterr().out.splitlines()[0] == FAKE_NOTE


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"llm_api_key": ""}, "error: CRE_LLM_API_KEY is required for --provider anthropic"),
        ({"llm_model": ""}, "error: CRE_LLM_MODEL is required for --provider anthropic"),
        (
            {"llm_price_input_per_mtok": 0},
            "error: CRE_LLM_PRICE_INPUT_PER_MTOK and CRE_LLM_PRICE_OUTPUT_PER_MTOK must be > 0"
            " for --provider anthropic",
        ),
        (
            {"llm_price_output_per_mtok": 0},
            "error: CRE_LLM_PRICE_INPUT_PER_MTOK and CRE_LLM_PRICE_OUTPUT_PER_MTOK must be > 0"
            " for --provider anthropic",
        ),
    ],
)
def test_real_run_refuses_incomplete_settings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    update: dict[str, Any],
    message: str,
) -> None:
    root = write_scenario(tmp_path)
    settings = Settings(**{**REAL.model_dump(), **update})
    assert _real_main(["--scenarios", str(root), "--yes"], settings=settings) == 2
    captured = capsys.readouterr()
    assert captured.err.strip() == message and captured.out == ""
    assert KEY not in captured.err


def test_real_run_without_tty_is_declined(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = write_scenario(tmp_path)
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return "y"

    code = _real_main(["--scenarios", str(root)], input_fn=answer, stdin_isatty=lambda: False)
    assert code == 1 and prompts == []
    captured = capsys.readouterr()
    assert "estimated upper-bound cost" in captured.out
    assert "aborted: not confirmed" in captured.err


def _eof(prompt: str) -> str:
    raise EOFError


@pytest.mark.parametrize("answer", [lambda p: "n", lambda p: "", lambda p: "yes please", _eof])
def test_real_run_declined_at_prompt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], answer: Callable[[str], str]
) -> None:
    root = write_scenario(tmp_path)
    code = _real_main(["--scenarios", str(root)], input_fn=answer, stdin_isatty=lambda: True)
    assert code == 1
    assert "aborted: not confirmed" in capsys.readouterr().err


@pytest.mark.parametrize("reply", ["y", " YES "])
def test_real_run_confirmed_at_prompt(tmp_path: Path, reply: str) -> None:
    root = write_scenario(tmp_path / "scenarios")
    stub = StubProvider(1)
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return reply

    argv = ["--scenarios", str(root), "--output", str(tmp_path / "run.json")]
    assert _real_main(argv, stub, input_fn=answer, stdin_isatty=lambda: True) == 0
    assert prompts == [CONFIRM_PROMPT] and stub.calls == 5 and stub.closed


def test_real_run_records_provider_cost_and_estimate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _scenarios(tmp_path / "scenarios", 2)
    stub = StubProvider(2)
    out = tmp_path / "run.json"
    assert _real_main(["--scenarios", str(root), "--output", str(out), "--yes"], stub) == 0
    text = out.read_text("utf-8")
    run = BenchmarkRun.model_validate_json(text)
    assert run.provider == "anthropic" and run.model == "stub-model"
    assert not run.fake_responses and run.max_usd == 10.0
    assert run.estimate is not None and len(run.estimate.cases) == 4
    assert run.stopped_reason == "completed" and run.skipped == []
    assert run.prompt_versions == {
        "hypotheses": HYPOTHESIS_PROMPT_VERSION,
        "investigation": INVESTIGATION_PROMPT_VERSION,
    }
    assert len(run.cases) == 4
    for case in run.cases:
        assert case.split == "dev" and case.provider == "anthropic"
        assert case.model == "stub-model" and case.prompt_versions == run.prompt_versions
        assert case.cost_usd is not None and case.cost_usd > 0
    assert run.total_cost_usd == pytest.approx(sum(c.cost_usd or 0 for c in run.cases))
    assert run.total_cost_usd == pytest.approx(stub.cost)
    captured = capsys.readouterr()
    assert FAKE_NOTE not in captured.out
    assert f"results written to {out}" in captured.out
    for blob in (captured.out, captured.err, text):
        assert KEY not in blob and "SENTINEL" not in blob
        assert "fake request failed" not in blob


def test_real_run_stops_at_spend_cap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _scenarios(tmp_path / "scenarios", 3)
    cost = 1 / 32
    stub = StubProvider(3, cost_per_call=cost)
    max_usd = 4 * cost + cost + 0.002
    out = tmp_path / "run.json"
    argv = ["--scenarios", str(root), "--output", str(out), "--yes", "--mode", "engine"]
    assert _real_main([*argv, "--max-usd", str(max_usd)], stub) == 3
    run = BenchmarkRun.model_validate_json(out.read_text("utf-8"))
    assert run.stopped_reason == "cap_reached"
    assert [c.case_id for c in run.cases] == ["case-01"]
    assert [(s.case_id, s.reason) for s in run.skipped] == [
        ("case-02", "cap_reached"),
        ("case-03", "cap_reached"),
    ]
    assert run.skipped[0].partial_cost_usd == pytest.approx(cost)
    assert run.skipped[1].partial_cost_usd == 0.0
    assert run.total_cost_usd is not None and run.total_cost_usd <= max_usd
    assert stub.calls == 5 and stub.closed
    summary = run.summaries[0]
    assert (summary.mode, summary.split, summary.cases) == ("engine", "dev", 1)
    assert "stopped: cap_reached" in capsys.readouterr().err


def test_cap_reached_in_engine_skips_baseline(tmp_path: Path) -> None:
    root = _scenarios(tmp_path / "scenarios", 1)
    stub = StubProvider(1, cost_per_call=1.0)
    out = tmp_path / "run.json"
    argv = ["--scenarios", str(root), "--output", str(out), "--yes", "--max-usd", "1.5"]
    assert _real_main(argv, stub) == 3
    run = BenchmarkRun.model_validate_json(out.read_text("utf-8"))
    assert run.cases == []
    assert [(s.mode, s.reason) for s in run.skipped] == [
        ("engine", "cap_reached"),
        ("baseline", "cap_reached"),
    ]


def test_keyboard_interrupt_writes_partial_results(tmp_path: Path) -> None:
    root = _scenarios(tmp_path / "scenarios", 2)
    stub = StubProvider(2, interrupt_at=5)
    out = tmp_path / "run.json"
    assert _real_main(["--scenarios", str(root), "--output", str(out), "--yes"], stub) == 3
    run = BenchmarkRun.model_validate_json(out.read_text("utf-8"))
    assert run.stopped_reason == "interrupted"
    assert [c.case_id for c in run.cases] == ["case-01"]
    assert [(s.case_id, s.mode, s.reason) for s in run.skipped] == [
        ("case-02", "engine", "interrupted"),
        ("case-01", "baseline", "interrupted"),
        ("case-02", "baseline", "interrupted"),
    ]
    assert stub.closed


def test_real_run_default_output_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = write_scenario(tmp_path / "scenarios")
    monkeypatch.chdir(tmp_path)
    assert _real_main(["--scenarios", str(root), "--yes"], StubProvider(1)) == 0
    [path] = list((tmp_path / "benchmark-results").glob("*.json"))
    assert path.name.startswith("benchmark-v2-anthropic-") and path.name.endswith("Z.json")
    assert BenchmarkRun.model_validate_json(path.read_text("utf-8")).provider == "anthropic"


def test_fake_run_writes_no_default_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = write_scenario(tmp_path / "scenarios")
    monkeypatch.chdir(tmp_path)
    assert main(["--scenarios", str(root)]) == 0
    assert not (tmp_path / "benchmark-results").exists()


def test_real_run_malformed_scenarios(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    doc = input_doc()
    doc["anchor"] = SENTINEL
    write_scenario(tmp_path, inputs=doc)
    assert _real_main(["--scenarios", str(tmp_path), "--yes"]) == 2
    assert capsys.readouterr().err.startswith("error: inputs/case-01.json: anchor")


def test_metrics_are_reported_per_set(tmp_path: Path) -> None:
    write_scenario(tmp_path, "case-01")
    wrong = fake_doc("case-02")
    wrong["engine"][2] = {"hypotheses": [hypothesis("Request fails", [1])], "unknowns": []}
    write_scenario(tmp_path, "case-02", fake=wrong, split="heldout")
    run = run_benchmark(tmp_path, Settings(), modes=["engine"])
    by_split = {s.split: s for s in run.summaries}
    assert by_split["dev"].top1_accuracy == 1.0
    assert by_split["heldout"].top1_accuracy == 0.0
    assert run.split_sizes == {"dev": 1, "heldout": 1}


def test_real_anthropic_provider_cost_via_mock_transport(tmp_path: Path) -> None:
    root = write_scenario(tmp_path / "scenarios")
    responses = [json.dumps(item) for item in engine_script()]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "msg_fake",
                "type": "message",
                "role": "assistant",
                "model": "stub-model",
                "content": [{"type": "text", "text": responses.pop(0)}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
        )

    def factory(settings: Settings) -> RealProvider:
        client = httpx.Client(transport=httpx.MockTransport(handler))
        provider = AnthropicProvider.from_settings(settings, client=client)
        return RealProvider(provider, lambda: provider.total_usage.cost, client.close)

    out = tmp_path / "run.json"
    argv = ["--provider", "anthropic", "--scenarios", str(root), "--output", str(out)]
    code = main([*argv, "--yes", "--mode", "engine"], settings=REAL, real_provider_factory=factory)
    assert code == 0
    run = BenchmarkRun.model_validate_json(out.read_text("utf-8"))
    [case] = run.cases
    assert case.stop_reason == "concluded" and case.provider == "anthropic"
    assert case.cost_usd == pytest.approx(4 * compute_cost(100, 50, 1, 1))
    assert KEY not in out.read_text("utf-8")


def test_table_has_no_error_lines_when_every_case_concludes(shipped_run: BenchmarkRun) -> None:
    assert "stopped with error" not in render_table(shipped_run)


def test_table_counts_error_details_per_mode(shipped_run: BenchmarkRun) -> None:
    cases = list(shipped_run.cases)
    engine = [i for i, c in enumerate(cases) if c.mode == "engine"][:3]
    baseline = [i for i, c in enumerate(cases) if c.mode == "baseline"][:1]
    for i, detail in zip(
        engine, ["revision output rejected"] * 2 + ["all tool calls failed"], strict=True
    ):
        cases[i] = cases[i].model_copy(update={"stop_reason": "error", "error_detail": detail})
    cases[baseline[0]] = cases[baseline[0]].model_copy(
        update={"stop_reason": "error", "error_detail": "baseline hypothesis generation failed"}
    )
    text = render_table(shipped_run.model_copy(update={"cases": cases}))
    assert (
        "engine stopped with error in 3 cases: all tool calls failed x1; "
        "revision output rejected x2" in text
    )
    assert (
        "baseline stopped with error in 1 cases: baseline hypothesis generation failed x1" in text
    )


def test_case_error_detail_is_written_to_the_run_record(shipped_run: BenchmarkRun) -> None:
    record = json.loads(shipped_run.model_dump_json())
    assert all("error_detail" in c and c["error_detail"] is None for c in record["cases"])
