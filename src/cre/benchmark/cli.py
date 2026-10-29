"""``python -m cre.benchmark``: load, validate, run and score every scenario.

All scenario files are validated before any case runs. Output never contains evidence, prompts
or model output, and the run record excludes secrets.
"""

import argparse
import hashlib
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from pydantic import JsonValue

from cre.benchmark import BENCHMARK_VERSION, DEFAULT_DATA_DIR
from cre.benchmark.fake import FakeScript, load_fake_script, make_fake_provider
from cre.benchmark.labels import ScenarioLabels, check_labels_against_input, load_labels
from cre.benchmark.runner import Mode, run_baseline_case, run_engine_case
from cre.benchmark.scenarios import (
    ScenarioError,
    ScenarioInput,
    file_sha256,
    load_input,
    relative_path,
)
from cre.benchmark.scoring import CaseScore, ModeSummary, score_case, summarize
from cre.config import Settings
from cre.evidence import _Model
from cre.llm import LLMProvider
from cre.prompts import HYPOTHESIS_PROMPT_VERSION, INVESTIGATION_PROMPT_VERSION

FAKE_NOTE = (
    "NOTE: fake provider with scripted responses; numbers check the pipeline, not engine quality."
)
_SUBDIRS = ("inputs", "labels", "fake")
_SECRET_FIELDS = {"llm_api_key", "database_url"}


class LoadedCase(NamedTuple):
    scenario: ScenarioInput
    labels: ScenarioLabels
    script: FakeScript


class ScenarioVersion(_Model):
    case_id: str
    scenario_version: int
    input_sha256: str
    labels_sha256: str
    fake_sha256: str


class BenchmarkRun(_Model):
    benchmark_version: str
    started_at: datetime
    finished_at: datetime
    modes: list[Mode]
    provider: str
    model: str
    fake_responses: bool
    prompt_versions: dict[str, str]
    code_version: str
    commit: str
    config: dict[str, JsonValue]
    scenario_set_sha256: str
    scenarios: list[ScenarioVersion]
    cases: list[CaseScore]
    summaries: list[ModeSummary]


def _json_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


def load_cases(root: Path) -> list[LoadedCase]:
    """Validate every scenario file under ``root``; raise ``ScenarioError`` on the first fault."""
    files = {name: _json_files(root / name) for name in _SUBDIRS}
    if not files["inputs"]:
        raise ScenarioError(f"{root}: no scenarios found")
    stems = {path.stem for path in files["inputs"]}
    for name in ("labels", "fake"):
        for path in files[name]:
            if path.stem not in stems:
                raise ScenarioError(f"{relative_path(path, root)}: file: missing inputs file")
    cases: list[LoadedCase] = []
    for input_path in files["inputs"]:
        labels_path = root / "labels" / input_path.name
        fake_path = root / "fake" / input_path.name
        for name, path in (("labels", labels_path), ("fake", fake_path)):
            if not path.is_file():
                raise ScenarioError(f"{relative_path(path, root)}: file: missing {name} file")
        scenario = load_input(input_path, root)
        labels = load_labels(labels_path, root)
        script = load_fake_script(fake_path, root)
        check_labels_against_input(
            labels,
            frozenset(item.id for item in scenario.evidence),
            relative_path(labels_path, root),
        )
        cases.append(LoadedCase(scenario, labels, script))
    return cases


def _scenario_hashes(root: Path, cases: Sequence[LoadedCase]) -> tuple[str, list[ScenarioVersion]]:
    lines: list[str] = []
    versions: list[ScenarioVersion] = []
    for case in cases:
        hashes: dict[str, str] = {}
        for name in _SUBDIRS:
            path = root / name / f"{case.scenario.case_id}.json"
            hashes[name] = file_sha256(path)
            lines.append(f"{relative_path(path, root)}\0{hashes[name]}\n")
        versions.append(
            ScenarioVersion(
                case_id=case.scenario.case_id,
                scenario_version=case.scenario.scenario_version,
                input_sha256=hashes["inputs"],
                labels_sha256=hashes["labels"],
                fake_sha256=hashes["fake"],
            )
        )
    digest = hashlib.sha256("".join(sorted(lines)).encode("utf-8")).hexdigest()
    return digest, versions


def run_benchmark(
    root: Path,
    settings: Settings,
    *,
    modes: Sequence[Mode],
    baseline_window_seconds: int = 300,
    usd_per_1k_tokens: float | None = None,
    provider_factory: Callable[[LoadedCase, Mode], LLMProvider] | None = None,
    clock: Callable[[], float] = time.perf_counter,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BenchmarkRun:
    started_at = now()
    cases = load_cases(root)
    set_sha256, versions = _scenario_hashes(root, cases)
    model = settings.llm_model or "fake-model"

    def default_factory(case: LoadedCase, mode: Mode) -> LLMProvider:
        return make_fake_provider(case.script, mode, model)

    factory = provider_factory or default_factory
    scores: list[CaseScore] = []
    summaries: list[ModeSummary] = []
    for mode in modes:
        mode_scores: list[CaseScore] = []
        for case in cases:
            provider = factory(case, mode)
            if mode == "engine":
                outcome = run_engine_case(case.scenario, provider, settings, clock=clock)
            else:
                outcome = run_baseline_case(
                    case.scenario,
                    provider,
                    settings,
                    window_seconds=baseline_window_seconds,
                    clock=clock,
                )
            mode_scores.append(score_case(outcome, case.labels))
        scores.extend(mode_scores)
        summaries.append(summarize(mode, mode_scores, usd_per_1k_tokens))

    config: dict[str, JsonValue] = settings.model_dump(mode="json", exclude=_SECRET_FIELDS)
    config["baseline_window_seconds"] = baseline_window_seconds
    config["usd_per_1k_tokens"] = usd_per_1k_tokens
    return BenchmarkRun(
        benchmark_version=BENCHMARK_VERSION,
        started_at=started_at,
        finished_at=now(),
        modes=list(modes),
        provider=settings.llm_provider,
        model=model,
        fake_responses=True,
        prompt_versions={
            "hypotheses": HYPOTHESIS_PROMPT_VERSION,
            "investigation": INVESTIGATION_PROMPT_VERSION,
        },
        code_version=settings.version,
        commit=settings.commit,
        config=config,
        scenario_set_sha256=set_sha256,
        scenarios=versions,
        cases=scores,
        summaries=summaries,
    )


def _fmt(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int):
        return str(value)
    return f"{value:.3f}"


_COLUMNS = (
    "mode",
    "cases",
    "errors",
    "top1_accuracy",
    "evidence_coverage",
    "unsupported_claim_rate",
    "abstention_accuracy",
    "abstention_precision",
    "abstention_recall",
    "latency_mean_s",
    "latency_p50_s",
    "latency_max_s",
    "approx_tokens",
    "cost_usd",
)


def render_table(run: BenchmarkRun) -> str:
    rows = [list(_COLUMNS)]
    for s in run.summaries:
        rows.append(
            [
                s.mode,
                _fmt(s.cases),
                _fmt(s.errors),
                _fmt(s.top1_accuracy),
                _fmt(s.evidence_coverage_mean),
                _fmt(s.unsupported_claim_rate),
                _fmt(s.abstention.accuracy),
                _fmt(s.abstention.precision),
                _fmt(s.abstention.recall),
                _fmt(s.latency_seconds.mean),
                _fmt(s.latency_seconds.p50),
                _fmt(s.latency_seconds.max),
                _fmt(s.approx_tokens_total),
                _fmt(s.cost_usd),
            ]
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(_COLUMNS))]
    lines = [FAKE_NOTE] if run.fake_responses else []
    lines.extend(
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip()
        for row in rows
    )
    return "\n".join(lines)


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number") from None
    if parsed < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return parsed


def _non_negative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number") from None
    if not parsed >= 0 or parsed == float("inf"):
        raise argparse.ArgumentTypeError("must be a finite number >= 0")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cre.benchmark",
        description="Run the seeded failure benchmark with the scripted fake provider.",
    )
    parser.add_argument("--mode", choices=["engine", "baseline", "both"], default="both")
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--baseline-window-seconds", type=_non_negative_int, default=300)
    parser.add_argument("--usd-per-1k-tokens", type=_non_negative_float, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    settings = Settings()
    if args.baseline_window_seconds > settings.retrieval_max_window_seconds:
        parser.error("--baseline-window-seconds exceeds CRE_RETRIEVAL_MAX_WINDOW_SECONDS")
    modes: list[Mode] = ["engine", "baseline"] if args.mode == "both" else [args.mode]
    try:
        run = run_benchmark(
            args.scenarios,
            settings,
            modes=modes,
            baseline_window_seconds=args.baseline_window_seconds,
            usd_per_1k_tokens=args.usd_per_1k_tokens,
        )
    except ScenarioError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(render_table(run))
    if args.output is not None:
        args.output.write_text(run.model_dump_json(indent=2), encoding="utf-8")
    return 0
