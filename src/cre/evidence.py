"""Normalized evidence schemas.

Every evidence item carries a timezone-aware timestamp, correlation IDs and full provenance.
Values taken directly from a source live in ``observed``; inferred relationships live only in
``inferred``.
"""

from typing import Annotated, Literal, Self
from uuid import UUID, uuid4

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

EventKind = Literal["log", "span", "deployment", "commit", "config_change"]
TRACE_ID_PATTERN = r"^[0-9a-f]{32}$"
SPAN_ID_PATTERN = r"^[0-9a-f]{16}$"


def _not_all_zeros(value: str) -> str:
    if set(value) == {"0"}:
        raise ValueError("ID must not be all zeros")
    return value


TraceId = Annotated[
    str, StringConstraints(pattern=TRACE_ID_PATTERN), AfterValidator(_not_all_zeros)
]
SpanId = Annotated[str, StringConstraints(pattern=SPAN_ID_PATTERN), AfterValidator(_not_all_zeros)]
NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceRef(_Model):
    source_type: NonEmptyStr
    name: NonEmptyStr
    uri: str | None = None
    locator: str | None = None


class Provenance(_Model):
    source: SourceRef
    retrieval_method: NonEmptyStr
    identifiers: Annotated[dict[str, str], Field(min_length=1)]
    retrieved_at: AwareDatetime


class Inference(_Model):
    statement: NonEmptyStr
    basis: Annotated[list[str], Field(min_length=1)]
    method: NonEmptyStr


class LogObserved(_Model):
    level: str
    message: str
    logger: str | None = None
    attributes: dict[str, JsonValue] = {}


class SpanObserved(_Model):
    name: str
    start_time: AwareDatetime
    end_time: AwareDatetime
    span_kind: str | None = None
    parent_span_id: SpanId | None = None
    status_code: str | None = None
    status_message: str | None = None
    attributes: dict[str, JsonValue] = {}

    @model_validator(mode="after")
    def _end_not_before_start(self) -> Self:
        if self.end_time < self.start_time:
            raise ValueError("end_time must not be before start_time")
        return self


class DeploymentObserved(_Model):
    version: str
    status: str
    commit: str | None = None
    environment: str | None = None
    actor: str | None = None
    attributes: dict[str, JsonValue] = {}


class CommitObserved(_Model):
    sha: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,40}$")]
    message: str
    author: str | None = None
    files: list[str] = []


class ConfigChangeObserved(_Model):
    key: NonEmptyStr
    old_value: str | None = None
    new_value: str | None = None
    changed_by: str | None = None


class _EvidenceBase(_Model):
    id: UUID = Field(default_factory=uuid4)
    timestamp: AwareDatetime
    provenance: Provenance
    trace_id: TraceId | None = None
    span_id: SpanId | None = None
    request_id: Annotated[str, StringConstraints(max_length=200)] | None = None
    service: str | None = None
    deployment_version: str | None = None
    inferred: list[Inference] = []


class LogEvidence(_EvidenceBase):
    kind: Literal["log"] = "log"
    observed: LogObserved


class SpanEvidence(_EvidenceBase):
    kind: Literal["span"] = "span"
    observed: SpanObserved

    @model_validator(mode="after")
    def _requires_trace_and_span_ids(self) -> Self:
        if self.trace_id is None or self.span_id is None:
            raise ValueError("span evidence requires trace_id and span_id")
        return self


class DeploymentEvidence(_EvidenceBase):
    kind: Literal["deployment"] = "deployment"
    observed: DeploymentObserved


class CommitEvidence(_EvidenceBase):
    kind: Literal["commit"] = "commit"
    observed: CommitObserved


class ConfigChangeEvidence(_EvidenceBase):
    kind: Literal["config_change"] = "config_change"
    observed: ConfigChangeObserved


Evidence = Annotated[
    LogEvidence | SpanEvidence | DeploymentEvidence | CommitEvidence | ConfigChangeEvidence,
    Field(discriminator="kind"),
]
EVIDENCE_ADAPTER: TypeAdapter[Evidence] = TypeAdapter(Evidence)
