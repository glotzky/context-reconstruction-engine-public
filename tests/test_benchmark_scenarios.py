import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cre.benchmark import DEFAULT_DATA_DIR
from cre.benchmark.cli import load_cases
from cre.benchmark.fake import load_fake_script
from cre.benchmark.labels import load_labels
from cre.benchmark.scenarios import MAX_SCENARIO_FILE_BYTES, ScenarioError, load_input
from tests.benchmark_fixtures import (
    SENTINEL,
    fake_doc,
    input_doc,
    labels_doc,
    uid,
    write_doc,
    write_scenario,
)


def test_valid_files_load(tmp_path: Path) -> None:
    write_scenario(tmp_path)
    scenario = load_input(tmp_path / "inputs" / "case-01.json", tmp_path)
    labels = load_labels(tmp_path / "labels" / "case-01.json", tmp_path)
    script = load_fake_script(tmp_path / "fake" / "case-01.json", tmp_path)
    assert scenario.case_id == labels.case_id == script.case_id == "case-01"
    assert len(scenario.evidence) == 5 and len(script.engine) == 4
    [case] = load_cases(tmp_path)
    assert case.scenario == scenario and case.labels == labels and case.script == script


def test_initial_evidence_keeps_order(tmp_path: Path) -> None:
    doc = input_doc()
    doc["initial_evidence_ids"] = [uid(4), uid(1), uid(3)]
    write_scenario(tmp_path, inputs=doc)
    scenario = load_input(tmp_path / "inputs" / "case-01.json", tmp_path)
    assert [item.id.hex[-2:] for item in scenario.initial_evidence()] == ["04", "01", "03"]


def _with_sentinel_message(doc: dict[str, Any]) -> dict[str, Any]:
    doc["evidence"][2]["observed"]["message"] = SENTINEL
    return doc


def _input(mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    doc = _with_sentinel_message(input_doc())
    mutate(doc)
    return doc


def _labels(mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    doc = labels_doc()
    doc["root_cause_summary"] = SENTINEL
    mutate(doc)
    return doc


def _set(key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    return lambda doc: doc.__setitem__(key, value)


def _dup(doc: dict[str, Any]) -> None:
    doc["evidence"][1]["id"] = doc["evidence"][0]["id"]


def _bad_kind(doc: dict[str, Any]) -> None:
    doc["evidence"][0]["kind"] = SENTINEL


def _naive(doc: dict[str, Any]) -> None:
    doc["anchor"] = "2026-01-01T00:00:00"


MALFORMED_INPUTS: dict[str, tuple[Any, str]] = {
    "invalid_json": ('{"case_id": "' + SENTINEL + '",\n  oops', "line 2: invalid JSON"),
    "missing_anchor": (_input(lambda d: d.pop("anchor")), "anchor: missing"),
    "naive_anchor": (_input(_naive), "anchor: timezone_aware"),
    "unknown_kind": (_input(_bad_kind), "evidence.0: union_tag_invalid"),
    "duplicate_id": (_input(_dup), "evidence: value_error, duplicate evidence id"),
    "initial_not_in_evidence": (
        _input(_set("initial_evidence_ids", [uid(99)])),
        "document: value_error, initial evidence id not in evidence",
    ),
    "case_id_not_stem": (_input(_set("case_id", "case-02")), "case_id: does not match"),
    "oversize": (
        json.dumps(input_doc()) + " " * MAX_SCENARIO_FILE_BYTES + SENTINEL,
        "file: file too large",
    ),
}


@pytest.mark.parametrize("name", sorted(MALFORMED_INPUTS))
def test_malformed_input(tmp_path: Path, name: str) -> None:
    doc, expected = MALFORMED_INPUTS[name]
    write_scenario(tmp_path, inputs=doc)
    with pytest.raises(ScenarioError) as info:
        load_cases(tmp_path)
    message = str(info.value)
    assert message.startswith("inputs/case-01.json: ")
    assert expected in message
    assert SENTINEL not in message


def _unknown_required(doc: dict[str, Any]) -> None:
    doc["required_evidence_ids"].append(uid(77))


MALFORMED_LABELS: dict[str, tuple[Any, str]] = {
    "unknown_evidence_id": (
        _labels(_unknown_required),
        "required_evidence_ids.2: unknown evidence id",
    ),
    "answerable_without_root_cause": (
        _labels(_set("root_cause_evidence_ids", [])),
        "document: value_error, answerable case needs",
    ),
    "insufficient_tag_but_answerable": (
        _labels(_set("tags", ["insufficient_evidence"])),
        "document: value_error, insufficient_evidence tag",
    ),
    "bad_category": (_labels(_set("category", SENTINEL)), "category: literal_error"),
}


@pytest.mark.parametrize("name", sorted(MALFORMED_LABELS))
def test_malformed_labels(tmp_path: Path, name: str) -> None:
    doc, expected = MALFORMED_LABELS[name]
    write_scenario(tmp_path, labels=doc)
    with pytest.raises(ScenarioError) as info:
        load_cases(tmp_path)
    message = str(info.value)
    assert message.startswith("labels/case-01.json: ")
    assert expected in message
    assert SENTINEL not in message


def test_malformed_fake_script(tmp_path: Path) -> None:
    doc = fake_doc()
    doc["baseline"] = [SENTINEL, SENTINEL]
    write_scenario(tmp_path, fake=doc)
    with pytest.raises(ScenarioError) as info:
        load_cases(tmp_path)
    assert str(info.value).startswith("fake/case-01.json: baseline: too_long")
    assert SENTINEL not in str(info.value)


def test_missing_labels_file(tmp_path: Path) -> None:
    write_scenario(tmp_path)
    (tmp_path / "labels" / "case-01.json").unlink()
    with pytest.raises(ScenarioError, match=r"^labels/case-01.json: file: missing labels file$"):
        load_cases(tmp_path)


def test_orphan_labels_file(tmp_path: Path) -> None:
    write_scenario(tmp_path)
    write_doc(tmp_path / "labels" / "case-02.json", labels_doc("case-02"))
    with pytest.raises(ScenarioError, match=r"^labels/case-02.json: file: missing inputs file$"):
        load_cases(tmp_path)


def test_empty_dir(tmp_path: Path) -> None:
    with pytest.raises(ScenarioError, match="no scenarios found"):
        load_cases(tmp_path)


def test_one_bad_case_fails_whole_load(tmp_path: Path) -> None:
    write_scenario(tmp_path, "case-01")
    write_scenario(
        tmp_path, "case-02", inputs=_input(_set("case_id", "case-02")) | {"anchor": "soon"}
    )
    with pytest.raises(ScenarioError, match="^inputs/case-02.json: anchor"):
        load_cases(tmp_path)


def test_shipped_scenarios_cover_plan() -> None:
    cases = load_cases(DEFAULT_DATA_DIR)
    assert [c.scenario.case_id for c in cases] == [f"case-{n:02d}" for n in range(1, 21)]
    categories = Counter(c.labels.category for c in cases)
    assert len(categories) == 5 and min(categories.values()) >= 3
    tags = Counter(t for c in cases for t in c.labels.tags)
    assert tags["lookalike"] >= 1
    assert tags["insufficient_evidence"] >= 1
    assert tags["misleading_change"] >= 1
    for case in cases:
        assert 4 <= len(case.scenario.evidence) <= 10


def test_case_02_is_lookalike_of_case_01() -> None:
    by_id = {c.scenario.case_id: c for c in load_cases(DEFAULT_DATA_DIR)}

    def cause_kinds(case_id: str) -> set[str]:
        case = by_id[case_id]
        items = {item.id: item for item in case.scenario.evidence}
        return {items[i].kind for i in case.labels.root_cause_evidence_ids}

    def has_undefined_column(case_id: str) -> bool:
        return any(
            item.kind == "log" and "UndefinedColumn" in item.observed.message
            for item in by_id[case_id].scenario.evidence
        )

    assert cause_kinds("case-02") == {"config_change"}
    assert "commit" in cause_kinds("case-01")
    assert not set(by_id["case-02"].labels.root_cause_evidence_ids) & set(
        by_id["case-01"].labels.root_cause_evidence_ids
    )
    assert by_id["case-02"].labels.tags == ["lookalike"]
    assert has_undefined_column("case-01") and has_undefined_column("case-02")


def test_shipped_inputs_contain_no_label_text() -> None:
    for case in load_cases(DEFAULT_DATA_DIR):
        name = f"{case.scenario.case_id}.json"
        texts = [(DEFAULT_DATA_DIR / sub / name).read_text("utf-8") for sub in ("inputs", "fake")]
        labels = case.labels
        for text in texts:
            for value in (labels.root_cause_id, labels.root_cause_summary, labels.category):
                assert value not in text, (name, value)
