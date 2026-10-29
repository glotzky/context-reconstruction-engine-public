"""Scripted fake model responses per scenario. Must not import ground-truth labels."""

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, JsonValue, ValidationError

from cre.benchmark.runner import Mode
from cre.benchmark.scenarios import (
    CaseId,
    check_case_id,
    format_validation_error,
    read_json_file,
    relative_path,
)
from cre.evidence import _Model
from cre.llm import FakeProvider


class FakeScript(_Model):
    schema_version: Literal[1]
    case_id: CaseId
    engine: Annotated[list[JsonValue], Field(min_length=1, max_length=20)]
    baseline: Annotated[list[JsonValue], Field(min_length=1, max_length=1)]


def load_fake_script(path: Path, root: Path) -> FakeScript:
    relpath = relative_path(path, root)
    data = read_json_file(path, root)
    try:
        script = FakeScript.model_validate(data)
    except ValidationError as exc:
        raise format_validation_error(relpath, exc) from None
    check_case_id(script.case_id, path, relpath)
    return script


def make_fake_provider(script: FakeScript, mode: Mode, model: str) -> FakeProvider:
    items = script.engine if mode == "engine" else script.baseline
    responses = [item if isinstance(item, str) else json.dumps(item) for item in items]
    return FakeProvider(responses, model=model)
