"""Ground-truth labels: schema and loader. Only scoring reads these."""

from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import StringConstraints, ValidationError, model_validator

from cre.benchmark.scenarios import (
    CaseId,
    ScenarioError,
    check_case_id,
    format_validation_error,
    read_json_file,
    relative_path,
)
from cre.evidence import _Model

Category = Literal[
    "schema_incompatibility",
    "configuration_drift",
    "downstream_failure",
    "race_condition",
    "missing_telemetry",
]
Tag = Literal["lookalike", "insufficient_evidence", "misleading_change"]


class ScenarioLabels(_Model):
    schema_version: Literal[1]
    case_id: CaseId
    category: Category
    tags: list[Tag] = []
    answerable: bool
    root_cause_id: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    root_cause_summary: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    root_cause_evidence_ids: list[UUID]
    accepted_keywords: list[Annotated[str, StringConstraints(min_length=1, max_length=100)]]
    required_evidence_ids: list[UUID]
    misleading_evidence_ids: list[UUID] = []

    @model_validator(mode="after")
    def _answerable_consistency(self) -> Self:
        if self.answerable and not (self.root_cause_evidence_ids and self.accepted_keywords):
            raise ValueError("answerable case needs root cause evidence and keywords")
        if ("insufficient_evidence" in self.tags) == self.answerable:
            raise ValueError("insufficient_evidence tag must be set exactly when not answerable")
        return self


def load_labels(path: Path, root: Path) -> ScenarioLabels:
    relpath = relative_path(path, root)
    data = read_json_file(path, root)
    try:
        labels = ScenarioLabels.model_validate(data)
    except ValidationError as exc:
        raise format_validation_error(relpath, exc) from None
    check_case_id(labels.case_id, path, relpath)
    return labels


def check_labels_against_input(
    labels: ScenarioLabels, evidence_ids: frozenset[UUID], relpath: str
) -> None:
    fields = {
        "root_cause_evidence_ids": labels.root_cause_evidence_ids,
        "required_evidence_ids": labels.required_evidence_ids,
        "misleading_evidence_ids": labels.misleading_evidence_ids,
    }
    for name, ids in fields.items():
        for index, evidence_id in enumerate(ids):
            if evidence_id not in evidence_ids:
                raise ScenarioError(f"{relpath}: {name}.{index}: unknown evidence id")
