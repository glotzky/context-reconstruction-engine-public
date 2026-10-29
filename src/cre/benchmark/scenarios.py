"""Investigator-facing scenario input: schema and strict loader.

Error messages name the file and the failing location only and never echo input values.
This module must not import ground-truth labels.
"""

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from cre.evidence import Evidence, _Model

CASE_ID_PATTERN = r"^case-[0-9]{2,3}$"
MAX_SCENARIO_FILE_BYTES = 1_048_576

CaseId = Annotated[str, StringConstraints(pattern=CASE_ID_PATTERN)]


class ScenarioError(ValueError):
    """A scenario file is missing, malformed or inconsistent. Never contains input values."""


class ScenarioInput(_Model):
    schema_version: Literal[1]
    case_id: CaseId
    scenario_version: Annotated[int, Field(ge=1)]
    anchor: AwareDatetime
    initial_evidence_ids: Annotated[list[UUID], Field(min_length=1)]
    evidence: Annotated[list[Evidence], Field(min_length=1, max_length=500)]

    @field_validator("evidence")
    @classmethod
    def _unique_evidence_ids(cls, evidence: list[Evidence]) -> list[Evidence]:
        if len({item.id for item in evidence}) != len(evidence):
            raise ValueError("duplicate evidence id")
        return evidence

    @model_validator(mode="after")
    def _initial_ids_in_evidence(self) -> Self:
        known = {item.id for item in self.evidence}
        if any(evidence_id not in known for evidence_id in self.initial_evidence_ids):
            raise ValueError("initial evidence id not in evidence")
        return self

    def initial_evidence(self) -> list[Evidence]:
        by_id = {item.id: item for item in self.evidence}
        return [by_id[evidence_id] for evidence_id in self.initial_evidence_ids]


def relative_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def read_json_file(path: Path, root: Path) -> object:
    relpath = relative_path(path, root)
    try:
        if path.stat().st_size > MAX_SCENARIO_FILE_BYTES:
            raise ScenarioError(f"{relpath}: file: file too large")
        raw = path.read_bytes()
    except OSError:
        raise ScenarioError(f"{relpath}: file: unreadable file") from None
    if len(raw) > MAX_SCENARIO_FILE_BYTES:
        raise ScenarioError(f"{relpath}: file: file too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ScenarioError(f"{relpath}: file: invalid JSON") from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ScenarioError(f"{relpath}: line {exc.lineno}: invalid JSON") from None


def format_validation_error(relpath: str, exc: ValidationError) -> ScenarioError:
    first = exc.errors(include_url=False, include_context=False, include_input=False)[0]
    locator = ".".join(str(part) for part in first["loc"]) or "document"
    detail = first["type"]
    if detail == "value_error":
        detail = f"value_error, {first['msg'].removeprefix('Value error, ')}"
    return ScenarioError(f"{relpath}: {locator}: {detail}")


def check_case_id(case_id: str, path: Path, relpath: str) -> None:
    if case_id != path.stem:
        raise ScenarioError(f"{relpath}: case_id: does not match file name")


def load_input(path: Path, root: Path) -> ScenarioInput:
    relpath = relative_path(path, root)
    data = read_json_file(path, root)
    try:
        scenario = ScenarioInput.model_validate(data)
    except ValidationError as exc:
        raise format_validation_error(relpath, exc) from None
    check_case_id(scenario.case_id, path, relpath)
    return scenario


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
