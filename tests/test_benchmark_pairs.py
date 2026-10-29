"""Case-21 / case-22: same symptom, different cause (migration vs config change)."""

import ast
from pathlib import Path

import pytest
from pydantic import ValidationError

from cre.benchmark import DEFAULT_DATA_DIR
from cre.benchmark.cli import LoadedCase, load_cases
from cre.benchmark.fake import make_fake_provider
from cre.benchmark.runner import run_baseline_case, run_engine_case
from cre.benchmark.scenarios import ScenarioInput, read_json_file
from cre.config import Settings
from cre.llm import LLMProvider, LLMRequest

SETTINGS = Settings()
PAIR = ("case-21", "case-22")
CAUSE_KINDS = {"commit", "deployment", "config_change"}


@pytest.fixture(scope="module")
def pair() -> tuple[LoadedCase, LoadedCase]:
    by_id = {case.scenario.case_id: case for case in load_cases(DEFAULT_DATA_DIR)}
    return by_id[PAIR[0]], by_id[PAIR[1]]


def _cause_kinds(case: LoadedCase) -> set[str]:
    items = {item.id: item for item in case.scenario.evidence}
    return {items[i].kind for i in case.labels.root_cause_evidence_ids}


def test_pair_initial_evidence_identical_except_id(pair: tuple[LoadedCase, LoadedCase]) -> None:
    migration, config = pair
    assert migration.scenario.anchor == config.scenario.anchor
    dumps = [
        [e.model_dump(exclude={"id"}) for e in case.scenario.initial_evidence()] for case in pair
    ]
    assert dumps[0] == dumps[1]
    assert {e.kind for e in migration.scenario.initial_evidence()} == {"log", "span"}

    def error_text(case: LoadedCase) -> list[str]:
        return [
            e.observed.message
            for e in case.scenario.initial_evidence()
            if e.kind == "log" and e.observed.level == "ERROR"
        ]

    assert error_text(migration) == error_text(config)
    assert len(error_text(migration)) == 1
    assert 'column "customer_name" does not exist' in error_text(migration)[0]


def test_pair_labels_differ(pair: tuple[LoadedCase, LoadedCase]) -> None:
    migration, config = pair
    assert migration.labels.category == "schema_incompatibility"
    assert config.labels.category == "configuration_drift"
    assert migration.labels.root_cause_id != config.labels.root_cause_id
    assert _cause_kinds(migration) == {"commit", "deployment"}
    assert _cause_kinds(config) == {"config_change"}
    assert not set(migration.labels.root_cause_evidence_ids) & set(
        config.labels.root_cause_evidence_ids
    )
    for case in pair:
        assert 4 <= len(case.scenario.evidence) <= 10


def test_pair_initial_evidence_hides_cause(pair: tuple[LoadedCase, LoadedCase]) -> None:
    for case in pair:
        assert not {e.kind for e in case.scenario.initial_evidence()} & CAUSE_KINDS


class RecordingProvider:
    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.requests: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> str:
        self.requests.append(request)
        return self.inner.complete(request)


def test_labels_never_reach_investigator_input(pair: tuple[LoadedCase, LoadedCase]) -> None:
    for case in pair:
        engine = RecordingProvider(make_fake_provider(case.script, "engine", "fake-model"))
        baseline = RecordingProvider(make_fake_provider(case.script, "baseline", "fake-model"))
        assert run_engine_case(case.scenario, engine, SETTINGS).stop_reason == "concluded"
        outcome = run_baseline_case(case.scenario, baseline, SETTINGS, window_seconds=300)
        assert outcome.stop_reason == "concluded"
        requests = engine.requests + baseline.requests
        assert engine.requests and baseline.requests
        labels = case.labels
        for request in requests:
            for text in (request.system, request.user):
                for value in (labels.root_cause_id, labels.root_cause_summary, labels.category):
                    assert value not in text, (case.scenario.case_id, value)


@pytest.mark.parametrize(
    ("field", "value"),
    [("category", "schema_incompatibility"), ("root_cause_id", "rc-21-migration-rename")],
)
def test_input_schema_rejects_label_fields(field: str, value: str) -> None:
    doc = read_json_file(DEFAULT_DATA_DIR / "inputs" / "case-21.json", DEFAULT_DATA_DIR)
    assert isinstance(doc, dict)
    ScenarioInput.model_validate(doc)
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ScenarioInput.model_validate(doc | {field: value})


@pytest.mark.parametrize("module", ["scenarios.py", "runner.py", "fake.py"])
def test_investigator_modules_do_not_import_labels(module: str) -> None:
    path = Path(__file__).resolve().parents[1] / "src" / "cre" / "benchmark" / module
    tree = ast.parse(path.read_text("utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            imported.append(base)
            imported.extend(f"{base}.{alias.name}" for alias in node.names)
    assert imported
    assert not any(
        name == "cre.benchmark.labels" or name.startswith("cre.benchmark.labels.")
        for name in imported
    )
