"""Normalizers that turn raw source records into typed evidence.

Raw input is redacted first; everything after that, including the ``content_sha256``
identifier, uses the redacted data. Fields not mapped to a typed field are kept in
``observed.attributes``. Normalizers never populate ``inferred``.
"""

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import (
    AwareDatetime,
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
    model_validator,
)

from cre.evidence import (
    CommitEvidence,
    CommitObserved,
    ConfigChangeEvidence,
    ConfigChangeObserved,
    DeploymentEvidence,
    DeploymentObserved,
    LogEvidence,
    LogObserved,
    Provenance,
    SourceRef,
    SpanEvidence,
    SpanObserved,
    _Model,
)
from cre.redaction import REDACTED, is_sensitive_key, redact, redact_text


class NormalizationError(ValueError):
    """Raised when a raw record cannot be normalized into evidence."""


_LOG_MAPPED_KEYS = frozenset(
    {
        "timestamp",
        "level",
        "logger",
        "message",
        "service",
        "version",
        "request_id",
        "trace_id",
        "span_id",
    }
)
_SPAN_MAPPED_KEYS = frozenset(
    {
        "name",
        "context",
        "kind",
        "parent_id",
        "start_time",
        "end_time",
        "status",
        "attributes",
        "events",
        "links",
        "resource",
    }
)
_DEPLOYMENT_MAPPED_KEYS = frozenset(
    {
        "deployed_at",
        "service",
        "version",
        "status",
        "commit",
        "environment",
        "actor",
        "deployment_id",
    }
)


CONFIG_CHANGES_MAX = 500
ConfigKey = Annotated[str, StringConstraints(min_length=1, max_length=200)]
NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]


class ConfigKeyChange(_Model):
    key: ConfigKey
    old_value: JsonValue = None
    new_value: JsonValue = None

    @model_validator(mode="after")
    def _values_differ(self) -> Self:
        if self.old_value is None and self.new_value is None:
            raise ValueError("old_value and new_value cannot both be null")
        if self.old_value == self.new_value:
            raise ValueError("old_value and new_value are equal")
        return self


class ConfigChangeRecord(_Model):
    changed_at: AwareDatetime
    service: NonEmptyStr
    version: NonEmptyStr
    actor: NonEmptyStr
    change_id: NonEmptyStr | None = None
    changes: Annotated[list[ConfigKeyChange], Field(min_length=1, max_length=CONFIG_CHANGES_MAX)]

    @model_validator(mode="after")
    def _unique_keys(self) -> Self:
        keys = [change.key for change in self.changes]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate config key in changes")
        return self


def _parse(raw: Mapping[str, Any] | str) -> dict[str, Any]:
    data: Any = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise NormalizationError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(data, Mapping):
        raise NormalizationError("input must be a JSON object")
    return dict(data)


def _load(raw: Mapping[str, Any] | str) -> dict[str, Any]:
    redacted: dict[str, Any] = redact(_parse(raw))
    return redacted


def _require(data: Mapping[str, Any], field: str) -> Any:
    value = data.get(field)
    if value is None:
        raise NormalizationError(f"missing required field: {field}")
    return value


def _content_sha256(data: Mapping[str, Any]) -> str:
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _normalize_id(value: Any) -> Any:
    if isinstance(value, str):
        lowered = value.lower()
        return lowered[2:] if lowered.startswith("0x") else lowered
    return value


def _provenance(
    data: Mapping[str, Any],
    source: SourceRef,
    retrieved_at: datetime,
    retrieval_method: str,
    extra_identifiers: Mapping[str, Any],
) -> Provenance:
    identifiers: dict[str, Any] = {"content_sha256": _content_sha256(data)}
    identifiers.update({k: v for k, v in extra_identifiers.items() if v is not None})
    return Provenance(
        source=source,
        retrieval_method=retrieval_method,
        retrieved_at=retrieved_at,
        identifiers=identifiers,
    )


def _validation_message(kind: str, exc: ValidationError) -> str:
    problems = "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
        for error in exc.errors()
    )
    return f"invalid {kind} record: {problems}"


def normalize_log(
    record: Mapping[str, Any] | str,
    *,
    source: SourceRef,
    retrieved_at: datetime,
    retrieval_method: str = "structured_log_json",
) -> LogEvidence:
    data = _load(record)
    timestamp = _require(data, "timestamp")
    message = _require(data, "message")
    level = _require(data, "level")
    request_id = data.get("request_id")
    trace_id = _normalize_id(data.get("trace_id"))
    try:
        return LogEvidence(
            timestamp=timestamp,
            provenance=_provenance(
                data,
                source,
                retrieved_at,
                retrieval_method,
                {"request_id": request_id, "trace_id": trace_id},
            ),
            trace_id=trace_id,
            span_id=_normalize_id(data.get("span_id")),
            request_id=request_id,
            service=data.get("service"),
            deployment_version=data.get("version"),
            observed=LogObserved(
                level=level,
                message=message,
                logger=data.get("logger"),
                attributes={k: v for k, v in data.items() if k not in _LOG_MAPPED_KEYS},
            ),
        )
    except ValidationError as exc:
        raise NormalizationError(_validation_message("log", exc)) from exc


def normalize_span(
    span: Mapping[str, Any] | str,
    *,
    source: SourceRef,
    retrieved_at: datetime,
    retrieval_method: str = "otel_sdk_span_json",
) -> SpanEvidence:
    data = _load(span)
    context = _require(data, "context")
    if not isinstance(context, Mapping):
        raise NormalizationError("context must be an object")
    trace_id = _normalize_id(_require(context, "trace_id"))
    span_id = _normalize_id(_require(context, "span_id"))
    start_time = _require(data, "start_time")
    end_time = _require(data, "end_time")
    name = _require(data, "name")

    resource = data.get("resource") or {}
    resource_attributes = (
        (resource.get("attributes") or {}) if isinstance(resource, Mapping) else {}
    )
    span_attributes = data.get("attributes") or {}
    if not isinstance(span_attributes, Mapping) or not isinstance(resource_attributes, Mapping):
        raise NormalizationError("attributes and resource.attributes must be objects")
    attributes: dict[str, Any] = dict(span_attributes)
    attributes["resource"] = dict(resource_attributes)
    for key in ("events", "links"):
        if data.get(key):
            attributes[key] = data[key]
    for key, value in data.items():
        if key not in _SPAN_MAPPED_KEYS:
            attributes.setdefault(key, value)

    status = data.get("status") or {}
    if not isinstance(status, Mapping):
        raise NormalizationError("status must be an object")
    kind = data.get("kind")
    if isinstance(kind, str) and kind.startswith("SpanKind."):
        kind = kind.removeprefix("SpanKind.")

    try:
        return SpanEvidence(
            timestamp=start_time,
            provenance=_provenance(
                data,
                source,
                retrieved_at,
                retrieval_method,
                {"trace_id": trace_id, "span_id": span_id},
            ),
            trace_id=trace_id,
            span_id=span_id,
            request_id=span_attributes.get("request_id"),
            service=resource_attributes.get("service.name"),
            deployment_version=resource_attributes.get("service.version"),
            observed=SpanObserved(
                name=name,
                start_time=start_time,
                end_time=end_time,
                span_kind=kind,
                parent_span_id=_normalize_id(data.get("parent_id")),
                status_code=status.get("status_code"),
                status_message=status.get("description"),
                attributes=attributes,
            ),
        )
    except ValidationError as exc:
        raise NormalizationError(_validation_message("span", exc)) from exc


def normalize_deployment(
    event: Mapping[str, Any] | str,
    *,
    source: SourceRef,
    retrieved_at: datetime,
    retrieval_method: str = "deployment_record",
) -> DeploymentEvidence:
    data = _load(event)
    deployed_at = _require(data, "deployed_at")
    service = _require(data, "service")
    version = _require(data, "version")
    status = _require(data, "status")
    try:
        return DeploymentEvidence(
            timestamp=deployed_at,
            provenance=_provenance(
                data,
                source,
                retrieved_at,
                retrieval_method,
                {"deployment_id": data.get("deployment_id")},
            ),
            service=service,
            deployment_version=version,
            observed=DeploymentObserved(
                version=version,
                status=status,
                commit=data.get("commit"),
                environment=data.get("environment"),
                actor=data.get("actor"),
                attributes={k: v for k, v in data.items() if k not in _DEPLOYMENT_MAPPED_KEYS},
            ),
        )
    except ValidationError as exc:
        raise NormalizationError(_validation_message("deployment", exc)) from exc


def normalize_commit(
    record: Mapping[str, Any] | str,
    *,
    source: SourceRef,
    retrieved_at: datetime,
    retrieval_method: str = "commit_record",
) -> CommitEvidence:
    data = _load(record)
    sha = _require(data, "sha")
    committed_at = _require(data, "committed_at")
    message = _require(data, "message")
    if isinstance(sha, str):
        sha = sha.lower()
    try:
        return CommitEvidence(
            timestamp=committed_at,
            provenance=_provenance(
                data, source, retrieved_at, retrieval_method, {"commit_sha": sha}
            ),
            service=data.get("service"),
            deployment_version=data.get("version"),
            observed=CommitObserved(
                sha=sha,
                message=message,
                author=data.get("author"),
                files=data.get("files") or [],
            ),
        )
    except ValidationError as exc:
        raise NormalizationError(_validation_message("commit", exc)) from exc


def _config_value(key: str, value: JsonValue) -> str | None:
    if value is None:
        return None
    if is_sensitive_key(key):
        return REDACTED
    redacted = redact(value)
    if isinstance(redacted, str):
        return redacted
    return json.dumps(redacted, sort_keys=True, separators=(",", ":"))


def normalize_config_change(
    record: Mapping[str, Any] | str,
    *,
    source: SourceRef,
    retrieved_at: datetime,
    retrieval_method: str = "config_change_record",
) -> list[ConfigChangeEvidence]:
    data = _parse(record)
    try:
        parsed = ConfigChangeRecord.model_validate(data)
    except ValidationError as exc:
        raise NormalizationError(_validation_message("config change", exc)) from exc
    service = redact_text(parsed.service)
    version = redact_text(parsed.version)
    actor = redact_text(parsed.actor)
    change_id = redact_text(parsed.change_id) if parsed.change_id is not None else None
    items: list[ConfigChangeEvidence] = []
    for index, change in enumerate(parsed.changes):
        key = redact_text(change.key)
        old_value = _config_value(change.key, change.old_value)
        new_value = _config_value(change.key, change.new_value)
        redacted = {
            "changed_at": parsed.changed_at.isoformat(),
            "service": service,
            "version": version,
            "actor": actor,
            "change_id": change_id,
            "key": key,
            "old_value": old_value,
            "new_value": new_value,
        }
        locator = f"{source.locator}.changes[{index}]" if source.locator else f"changes[{index}]"
        try:
            items.append(
                ConfigChangeEvidence(
                    timestamp=parsed.changed_at,
                    provenance=_provenance(
                        redacted,
                        source.model_copy(update={"locator": locator}),
                        retrieved_at,
                        retrieval_method,
                        {"config_key": key, "change_id": change_id},
                    ),
                    service=service,
                    deployment_version=version,
                    observed=ConfigChangeObserved(
                        key=key, old_value=old_value, new_value=new_value, changed_by=actor
                    ),
                )
            )
        except ValidationError as exc:
            raise NormalizationError(_validation_message("config change", exc)) from exc
    return items
