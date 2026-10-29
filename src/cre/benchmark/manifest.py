"""Dev / held-out split manifest: schema and strict loader. Must not import labels.

The split separates reporting, not secrecy: held-out labels stay in the repository.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from cre.benchmark.scenarios import CaseId, ScenarioError, format_validation_error, read_json_file
from cre.evidence import _Model

Split = Literal["dev", "heldout"]
MANIFEST_FILE = "manifest.json"
_SPLITS: tuple[Split, ...] = ("dev", "heldout")


class BenchmarkManifest(_Model):
    schema_version: Literal[1]
    dev: list[CaseId]
    heldout: list[CaseId]

    def split_of(self) -> dict[str, Split]:
        result: dict[str, Split] = {}
        for split in _SPLITS:
            for case_id in getattr(self, split):
                result[case_id] = split
        return result


def load_manifest(root: Path, case_ids: Sequence[str]) -> BenchmarkManifest:
    """Load ``root / manifest.json`` and check it assigns every case to exactly one set."""
    path = root / MANIFEST_FILE
    if not path.is_file():
        raise ScenarioError(f"{MANIFEST_FILE}: file: missing manifest file")
    data = read_json_file(path, root)
    try:
        manifest = BenchmarkManifest.model_validate(data)
    except ValidationError as exc:
        raise format_validation_error(MANIFEST_FILE, exc) from None
    known = set(case_ids)
    seen: set[str] = set()
    for split in _SPLITS:
        for index, case_id in enumerate(getattr(manifest, split)):
            if case_id in seen:
                raise ScenarioError(f"{MANIFEST_FILE}: {split}.{index}: duplicate case id")
            if case_id not in known:
                raise ScenarioError(f"{MANIFEST_FILE}: {split}.{index}: unknown case id")
            seen.add(case_id)
    if known - seen:
        raise ScenarioError(f"{MANIFEST_FILE}: document: case not assigned to a set")
    return manifest
