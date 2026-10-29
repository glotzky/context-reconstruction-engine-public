import json
from pathlib import Path

import pytest

from cre.benchmark import DEFAULT_DATA_DIR
from cre.benchmark.cli import load_cases
from cre.benchmark.labels import load_labels
from cre.benchmark.manifest import load_manifest
from cre.benchmark.scenarios import ScenarioError
from tests.benchmark_fixtures import SENTINEL, write_doc, write_manifest, write_scenario


def _shipped_ids() -> list[str]:
    return [path.stem for path in sorted((DEFAULT_DATA_DIR / "inputs").glob("*.json"))]


def test_packaged_manifest_splits_all_cases() -> None:
    ids = _shipped_ids()
    manifest = load_manifest(DEFAULT_DATA_DIR, ids)
    assert len(manifest.dev) == 20 and len(manifest.heldout) == 10
    assert not set(manifest.dev) & set(manifest.heldout)
    assert set(manifest.dev) | set(manifest.heldout) == set(ids)
    assert len(ids) == 30


def test_packaged_sets_each_contain_unanswerable_cases() -> None:
    manifest = load_manifest(DEFAULT_DATA_DIR, _shipped_ids())

    def unanswerable(case_ids: list[str]) -> int:
        paths = [DEFAULT_DATA_DIR / "labels" / f"{case_id}.json" for case_id in case_ids]
        return sum(1 for path in paths if not load_labels(path, DEFAULT_DATA_DIR).answerable)

    assert unanswerable(manifest.heldout) >= 2
    assert unanswerable(manifest.dev) >= 2


def test_split_of_maps_every_case(tmp_path: Path) -> None:
    write_scenario(tmp_path, "case-01")
    write_scenario(tmp_path, "case-02", split="heldout")
    manifest = load_manifest(tmp_path, ["case-01", "case-02"])
    assert manifest.split_of() == {"case-01": "dev", "case-02": "heldout"}
    assert [case.split for case in load_cases(tmp_path)] == ["dev", "heldout"]


def test_missing_manifest(tmp_path: Path) -> None:
    write_scenario(tmp_path)
    (tmp_path / "manifest.json").unlink()
    with pytest.raises(ScenarioError, match="^manifest.json: file: missing manifest file$"):
        load_cases(tmp_path)


@pytest.mark.parametrize(
    ("dev", "heldout", "locator"),
    [
        (["case-01", "case-01"], [], "dev.1"),
        (["case-01"], ["case-01"], "heldout.0"),
    ],
)
def test_duplicate_case_id(
    tmp_path: Path, dev: list[str], heldout: list[str], locator: str
) -> None:
    write_scenario(tmp_path)
    write_manifest(tmp_path, dev, heldout)
    with pytest.raises(ScenarioError, match=f"^manifest.json: {locator}: duplicate case id$"):
        load_cases(tmp_path)


def test_unknown_case_id(tmp_path: Path) -> None:
    write_scenario(tmp_path)
    write_manifest(tmp_path, ["case-01"], ["case-99"])
    with pytest.raises(ScenarioError, match="^manifest.json: heldout.0: unknown case id$"):
        load_cases(tmp_path)


def test_case_not_assigned(tmp_path: Path) -> None:
    write_scenario(tmp_path, "case-01")
    write_scenario(tmp_path, "case-02")
    write_manifest(tmp_path, ["case-01"], [])
    with pytest.raises(
        ScenarioError, match="^manifest.json: document: case not assigned to a set$"
    ):
        load_cases(tmp_path)


@pytest.mark.parametrize(
    "doc",
    [
        {"schema_version": 1, "dev": ["case-01"], "heldout": [], "extra": SENTINEL},
        {"schema_version": SENTINEL, "dev": ["case-01"], "heldout": []},
        {"schema_version": 2, "dev": ["case-01"], "heldout": []},
        {"schema_version": 1, "dev": [SENTINEL], "heldout": []},
    ],
)
def test_invalid_manifest_hides_values(tmp_path: Path, doc: dict[str, object]) -> None:
    write_scenario(tmp_path)
    write_doc(tmp_path / "manifest.json", json.dumps(doc))
    with pytest.raises(ScenarioError, match="^manifest.json: ") as info:
        load_cases(tmp_path)
    assert SENTINEL not in str(info.value)
