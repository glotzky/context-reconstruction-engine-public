import json
from pathlib import Path

import pytest

from cre.benchmark import BENCHMARK_VERSION, DEFAULT_DATA_DIR
from cre.benchmark.cli import FAKE_NOTE, BenchmarkRun, main, run_benchmark
from cre.config import Settings
from cre.prompts import HYPOTHESIS_PROMPT_VERSION, INVESTIGATION_PROMPT_VERSION
from tests.benchmark_fixtures import SENTINEL, input_doc, write_scenario

SETTINGS = Settings()


@pytest.fixture(scope="module")
def shipped_run() -> BenchmarkRun:
    return run_benchmark(DEFAULT_DATA_DIR, SETTINGS, modes=["engine", "baseline"])


def test_shipped_run_concludes_and_records_versions(shipped_run: BenchmarkRun) -> None:
    run = shipped_run
    assert len(run.cases) == 40
    assert [c.stop_reason for c in run.cases] == ["concluded"] * 40
    assert run.benchmark_version == BENCHMARK_VERSION
    assert run.provider == "fake" and run.model == "fake-model" and run.fake_responses
    assert run.modes == ["engine", "baseline"]
    assert run.prompt_versions == {
        "hypotheses": HYPOTHESIS_PROMPT_VERSION,
        "investigation": INVESTIGATION_PROMPT_VERSION,
    }
    assert run.code_version == SETTINGS.version and run.commit == SETTINGS.commit
    assert len(run.scenarios) == 20
    for version in run.scenarios:
        for digest in (version.input_sha256, version.labels_sha256, version.fake_sha256):
            assert len(digest) == 64 and int(digest, 16) >= 0
    assert len(run.scenario_set_sha256) == 64
    assert [s.mode for s in run.summaries] == ["engine", "baseline"]
    for summary in run.summaries:
        assert summary.cases == 20 and summary.errors == 0
        assert summary.latency_seconds.mean is not None
        assert summary.approx_tokens_total > 0
        assert summary.cost_usd is None
    assert run.config["baseline_window_seconds"] == 300
    assert run.started_at <= run.finished_at


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
    assert len(record["summaries"]) == 2
    for summary in record["summaries"]:
        assert summary["cost_usd"] == pytest.approx(summary["approx_tokens_total"] / 1000 * 0.5)
    assert record["config"]["usd_per_1k_tokens"] == 0.5
