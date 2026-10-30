"""``python -m cre.benchmark``: load, validate, run and score every scenario.

All scenario files are validated before any case runs. Output never contains evidence, prompts
or model output, and the run record excludes secrets.
"""

import argparse
import hashlib
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NamedTuple

from pydantic import JsonValue

from cre.benchmark import BENCHMARK_VERSION, DEFAULT_DATA_DIR
from cre.benchmark.cost import RunEstimate, SpendGuard, estimate_run, render_estimate
from cre.benchmark.fake import FakeScript, load_fake_script, make_fake_provider
from cre.benchmark.labels import ScenarioLabels, check_labels_against_input, load_labels
from cre.benchmark.manifest import MANIFEST_FILE, Split, load_manifest
from cre.benchmark.runner import CaseOutcome, Mode, run_baseline_case, run_engine_case
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
CONFIRM_PROMPT = "Proceed with a real-provider run? [y/N] "
DEFAULT_RESULTS_DIR = Path("benchmark-results")
_SUBDIRS = ("inputs", "labels", "fake")
_SECRET_FIELDS = {"llm_api_key", "database_url"}
_SPLITS: tuple[Split, ...] = ("dev", "heldout")

StoppedReason = Literal["completed", "cap_reached", "interrupted"]


class LoadedCase(NamedTuple):
    scenario: ScenarioInput
    labels: ScenarioLabels
    script: FakeScript
    split: Split


class SkippedCase(_Model):
    case_id: str
    mode: Mode
    split: Split
    reason: Literal["cap_reached", "interrupted"]
    partial_cost_usd: float


class RealProvider(NamedTuple):
    provider: LLMProvider
    total_cost: Callable[[], float]
    close: Callable[[], None]


class ScenarioVersion(_Model):
    case_id: str
    scenario_version: int
    input_sha256: str
    labels_sha256: str
    fake_sha256: str


class BenchmarkRun(_Model):
    benchmark_version: str
    results_schema_version: Literal[2]
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
    manifest_sha256: str
    split_sizes: dict[str, int]
    scenarios: list[ScenarioVersion]
    cases: list[CaseScore]
    summaries: list[ModeSummary]
    max_usd: float | None
    estimate: RunEstimate | None
    total_cost_usd: float | None
    stopped_reason: StoppedReason
    skipped: list[SkippedCase]


def make_anthropic_provider(settings: Settings) -> RealProvider:
    from cre.llm_anthropic import AnthropicProvider

    provider = AnthropicProvider.from_settings(settings)
    return RealProvider(provider, lambda: provider.total_usage.cost, provider.close)


def check_real_provider_settings(settings: Settings) -> str | None:
    if not settings.llm_api_key.get_secret_value():
        return "CRE_LLM_API_KEY is required for --provider anthropic"
    if not settings.llm_model:
        return "CRE_LLM_MODEL is required for --provider anthropic"
    if settings.llm_price_input_per_mtok <= 0 or settings.llm_price_output_per_mtok <= 0:
        return (
            "CRE_LLM_PRICE_INPUT_PER_MTOK and CRE_LLM_PRICE_OUTPUT_PER_MTOK must be > 0"
            " for --provider anthropic"
        )
    return None


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
    loaded: list[tuple[ScenarioInput, ScenarioLabels, FakeScript]] = []
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
        loaded.append((scenario, labels, script))
    manifest = load_manifest(root, [scenario.case_id for scenario, _, _ in loaded])
    split_of = manifest.split_of()
    return [
        LoadedCase(scenario, labels, script, split_of[scenario.case_id])
        for scenario, labels, script in loaded
    ]


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
    manifest_path = root / MANIFEST_FILE
    lines.append(f"{relative_path(manifest_path, root)}\0{file_sha256(manifest_path)}\n")
    digest = hashlib.sha256("".join(sorted(lines)).encode("utf-8")).hexdigest()
    return digest, versions


def _run_case(
    case: LoadedCase,
    mode: Mode,
    provider: LLMProvider,
    settings: Settings,
    baseline_window_seconds: int,
    clock: Callable[[], float],
) -> CaseOutcome:
    if mode == "engine":
        return run_engine_case(case.scenario, provider, settings, clock=clock)
    return run_baseline_case(
        case.scenario,
        provider,
        settings,
        window_seconds=baseline_window_seconds,
        clock=clock,
    )


def run_benchmark(
    root: Path,
    settings: Settings,
    *,
    modes: Sequence[Mode],
    baseline_window_seconds: int = 300,
    usd_per_1k_tokens: float | None = None,
    provider_factory: Callable[[LoadedCase, Mode], LLMProvider] | None = None,
    spend_guard: SpendGuard | None = None,
    max_usd: float | None = None,
    estimate: RunEstimate | None = None,
    clock: Callable[[], float] = time.perf_counter,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> BenchmarkRun:
    """Run every case mode-major. With a ``spend_guard``, stop at the cap and record skips."""
    started_at = now()
    cases = load_cases(root)
    set_sha256, versions = _scenario_hashes(root, cases)
    model = settings.llm_model or "fake-model"
    prompt_versions = {
        "hypotheses": HYPOTHESIS_PROMPT_VERSION,
        "investigation": INVESTIGATION_PROMPT_VERSION,
    }

    def default_factory(case: LoadedCase, mode: Mode) -> LLMProvider:
        return make_fake_provider(case.script, mode, model)

    factory = provider_factory or default_factory
    pending = [(mode, case) for mode in modes for case in cases]
    scores: list[CaseScore] = []
    skipped: list[SkippedCase] = []
    stopped_reason: StoppedReason = "completed"

    def skip_from(
        index: int, reason: Literal["cap_reached", "interrupted"], partial: float = 0.0
    ) -> None:
        for offset, (mode, case) in enumerate(pending[index:]):
            skipped.append(
                SkippedCase(
                    case_id=case.scenario.case_id,
                    mode=mode,
                    split=case.split,
                    reason=reason,
                    partial_cost_usd=partial if offset == 0 else 0.0,
                )
            )

    for index, (mode, case) in enumerate(pending):
        if spend_guard is not None and (
            spend_guard.cap_reached or (max_usd is not None and spend_guard.spent >= max_usd)
        ):
            stopped_reason = "cap_reached"
            skip_from(index, stopped_reason)
            break
        spent_before = spend_guard.spent if spend_guard is not None else 0.0
        try:
            outcome = _run_case(
                case, mode, factory(case, mode), settings, baseline_window_seconds, clock
            )
        except KeyboardInterrupt:
            stopped_reason = "interrupted"
            partial = spend_guard.spent - spent_before if spend_guard is not None else 0.0
            skip_from(index, stopped_reason, max(0.0, partial))
            break
        cost_usd: float | None
        if spend_guard is not None:
            cost_usd = spend_guard.spent - spent_before
            if spend_guard.cap_reached:
                stopped_reason = "cap_reached"
                skip_from(index, stopped_reason, max(0.0, cost_usd))
                break
        elif usd_per_1k_tokens is not None:
            cost_usd = outcome.approx_tokens / 1000 * usd_per_1k_tokens
        else:
            cost_usd = None
        scores.append(
            score_case(
                outcome,
                case.labels,
                split=case.split,
                prompt_versions=prompt_versions,
                cost_usd=cost_usd,
            )
        )

    summaries = [
        summarize(mode, split, [s for s in scores if s.mode == mode and s.split == split])
        for mode in modes
        for split in _SPLITS
    ]
    if spend_guard is not None:
        total_cost_usd: float | None = spend_guard.spent
    else:
        costs = [s.cost_usd for s in scores if s.cost_usd is not None]
        total_cost_usd = sum(costs) if costs else None

    config: dict[str, JsonValue] = settings.model_dump(mode="json", exclude=_SECRET_FIELDS)
    config["baseline_window_seconds"] = baseline_window_seconds
    config["usd_per_1k_tokens"] = usd_per_1k_tokens
    return BenchmarkRun(
        benchmark_version=BENCHMARK_VERSION,
        results_schema_version=2,
        started_at=started_at,
        finished_at=now(),
        modes=list(modes),
        provider=settings.llm_provider,
        model=model,
        fake_responses=settings.llm_provider == "fake",
        prompt_versions=prompt_versions,
        code_version=settings.version,
        commit=settings.commit,
        config=config,
        scenario_set_sha256=set_sha256,
        manifest_sha256=file_sha256(root / MANIFEST_FILE),
        split_sizes={split: sum(1 for c in cases if c.split == split) for split in _SPLITS},
        scenarios=versions,
        cases=scores,
        summaries=summaries,
        max_usd=max_usd,
        estimate=estimate,
        total_cost_usd=total_cost_usd,
        stopped_reason=stopped_reason,
        skipped=skipped,
    )


def _fmt(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int):
        return str(value)
    return f"{value:.3f}"


_COLUMNS = (
    "mode",
    "set",
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
                s.split,
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
    lines.extend(_error_lines(run))
    return "\n".join(lines)


def _error_lines(run: BenchmarkRun) -> list[str]:
    """One line per mode listing how often each content-free error detail occurred."""
    lines: list[str] = []
    for mode in ("engine", "baseline"):
        details = Counter(c.error_detail for c in run.cases if c.mode == mode and c.error_detail)
        if details:
            parts = "; ".join(f"{d} x{n}" for d, n in sorted(details.items()))
            lines.append(f"{mode} stopped with error in {sum(details.values())} cases: {parts}")
    return lines


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


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number") from None
    if not parsed > 0 or parsed == float("inf"):
        raise argparse.ArgumentTypeError("must be a finite number > 0")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cre.benchmark",
        description=(
            "Run the seeded failure benchmark. Uses the scripted fake provider unless"
            " --provider anthropic is given."
        ),
    )
    parser.add_argument("--mode", choices=["engine", "baseline", "both"], default="both")
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--baseline-window-seconds", type=_non_negative_int, default=300)
    parser.add_argument("--usd-per-1k-tokens", type=_non_negative_float, default=None)
    parser.add_argument("--provider", choices=["fake", "anthropic"], default="fake")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--max-usd", type=_positive_float, default=10.0)
    return parser


def _confirmed(input_fn: Callable[[str], str], stdin_isatty: Callable[[], bool]) -> bool:
    if not stdin_isatty():
        return False
    try:
        answer = input_fn(CONFIRM_PROMPT)
    except EOFError:
        return False
    return answer.strip().casefold() in {"y", "yes"}


def default_output_path(run: BenchmarkRun) -> Path:
    stamp = run.started_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_RESULTS_DIR / f"{run.benchmark_version}-{run.provider}-{stamp}.json"


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    real_provider_factory: Callable[[Settings], RealProvider] = make_anthropic_provider,
    input_fn: Callable[[str], str] = input,
    stdin_isatty: Callable[[], bool] = lambda: sys.stdin.isatty(),
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    base = settings if settings is not None else Settings()
    if args.baseline_window_seconds > base.retrieval_max_window_seconds:
        parser.error("--baseline-window-seconds exceeds CRE_RETRIEVAL_MAX_WINDOW_SECONDS")
    effective = base.model_copy(update={"llm_provider": args.provider})
    modes: list[Mode] = ["engine", "baseline"] if args.mode == "both" else [args.mode]
    if args.provider == "fake":
        try:
            run = run_benchmark(
                args.scenarios,
                effective,
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

    problem = check_real_provider_settings(effective)
    if problem is not None:
        print(f"error: {problem}", file=sys.stderr)
        return 2
    try:
        cases = load_cases(args.scenarios)
    except ScenarioError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    estimate = estimate_run(
        [case.scenario for case in cases],
        modes,
        effective,
        baseline_window_seconds=args.baseline_window_seconds,
        max_usd=args.max_usd,
    )
    print(render_estimate(estimate, effective.llm_model), flush=True)
    if not (args.yes or _confirmed(input_fn, stdin_isatty)):
        print("aborted: not confirmed", file=sys.stderr)
        return 1

    real = real_provider_factory(effective)
    try:
        guard = SpendGuard(
            real.provider,
            real.total_cost,
            max_usd=args.max_usd,
            price_input_per_mtok=effective.llm_price_input_per_mtok,
            price_output_per_mtok=effective.llm_price_output_per_mtok,
        )
        run = run_benchmark(
            args.scenarios,
            effective,
            modes=modes,
            baseline_window_seconds=args.baseline_window_seconds,
            usd_per_1k_tokens=args.usd_per_1k_tokens,
            provider_factory=lambda case, mode: guard,
            spend_guard=guard,
            max_usd=args.max_usd,
            estimate=estimate,
        )
    except ScenarioError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        real.close()
    print(render_table(run))
    output: Path = args.output if args.output is not None else default_output_path(run)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(run.model_dump_json(indent=2), encoding="utf-8")
    print(f"results written to {output}")
    if run.stopped_reason != "completed":
        print(f"stopped: {run.stopped_reason}", file=sys.stderr)
        return 3
    return 0
