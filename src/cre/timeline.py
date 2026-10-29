"""Deterministic incident timeline built from retrieved evidence.

Entries are observed facts ordered by event time with a ``str(id)`` tie-break that only makes
output deterministic. Relationships are inferred and labeled ``explicit`` (shared identifiers) or
``temporal_only`` (a lead from timing alone). Unknowns record ambiguities and gaps. Nothing here
states causation. Pure: no DB, no network, no logging of evidence content.
"""

from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from cre.evidence import (
    CommitEvidence,
    ConfigChangeEvidence,
    DeploymentEvidence,
    EventKind,
    Evidence,
    LogEvidence,
    SpanEvidence,
    _Model,
)
from cre.retrieval import EvidenceQueryResult

EntryType = Literal[
    "code_commit", "deployment", "config_change", "runtime_symptom", "runtime_event"
]
LinkBasis = Literal["explicit", "temporal_only"]
LinkIdentifier = Literal["trace_id", "request_id", "span_id", "deployment_version", "commit_sha"]
UnknownKind = Literal[
    "equal_timestamps",
    "clock_skew_suspected",
    "order_conflict",
    "ambiguous_correlation",
    "unexplained_interval",
    "missing_telemetry",
    "truncated_retrieval",
    "no_evidence",
]

SUMMARY_MAX_CHARS = 200
_SYMPTOM_LOG_LEVELS = frozenset({"ERROR", "CRITICAL", "FATAL"})
_CHANGE_TYPES = frozenset({"code_commit", "deployment", "config_change"})
_MIN_COMMIT_PREFIX = 7
_EXPLICIT_SUFFIX = "This is a correlation, not a determination of cause."
_LEAD_SUFFIX = "timing only. This is a lead to check, not evidence of causation."


class TimelineParams(_Model):
    clock_skew_tolerance: timedelta = timedelta(seconds=2)
    proximity_window: timedelta = timedelta(minutes=15)
    gap_threshold: timedelta = timedelta(minutes=10)

    @field_validator("clock_skew_tolerance")
    @classmethod
    def _not_negative(cls, value: timedelta) -> timedelta:
        if value < timedelta(0):
            raise ValueError("must not be negative")
        return value

    @field_validator("proximity_window", "gap_threshold")
    @classmethod
    def _positive(cls, value: timedelta) -> timedelta:
        if value <= timedelta(0):
            raise ValueError("must be positive")
        return value


class TimelineEntry(_Model):
    entry_id: str
    position: int
    occurred_at: AwareDatetime
    end_time: AwareDatetime | None = None
    entry_type: EntryType
    evidence_kind: EventKind
    evidence_ids: Annotated[list[UUID], Field(min_length=1)]
    service: str | None
    deployment_version: str | None
    trace_id: str | None
    request_id: str | None
    summary: Annotated[str, Field(max_length=SUMMARY_MAX_CHARS)]


class TimelineRelationship(_Model):
    from_entry_id: str
    to_entry_id: str
    basis: LinkBasis
    identifiers: list[LinkIdentifier]
    is_lead: bool
    statement: str

    @model_validator(mode="after")
    def _basis_consistent(self) -> Self:
        if self.is_lead != (self.basis == "temporal_only"):
            raise ValueError("is_lead must be true exactly when basis is temporal_only")
        if bool(self.identifiers) != (self.basis == "explicit"):
            raise ValueError("identifiers must be non-empty exactly when basis is explicit")
        if self.identifiers != sorted(set(self.identifiers)):
            raise ValueError("identifiers must be sorted and unique")
        return self


class TimelineUnknown(_Model):
    kind: UnknownKind
    category: Literal["ambiguity", "gap"]
    entry_ids: list[str]
    start: AwareDatetime | None = None
    end: AwareDatetime | None = None
    description: str


class Timeline(_Model):
    window_start: AwareDatetime | None
    window_end: AwareDatetime | None
    entries: list[TimelineEntry]
    relationships: list[TimelineRelationship]
    unknowns: list[TimelineUnknown]


def _entry_type(item: Evidence) -> EntryType:
    if isinstance(item, CommitEvidence):
        return "code_commit"
    if isinstance(item, DeploymentEvidence):
        return "deployment"
    if isinstance(item, ConfigChangeEvidence):
        return "config_change"
    if isinstance(item, LogEvidence):
        symptom = item.observed.level.upper() in _SYMPTOM_LOG_LEVELS
    else:
        symptom = "ERROR" in (item.observed.status_code or "").upper()
    return "runtime_symptom" if symptom else "runtime_event"


def _summary(item: Evidence) -> str:
    if isinstance(item, CommitEvidence):
        lines = item.observed.message.splitlines()
        text = f"commit {item.observed.sha[:12]}: {lines[0] if lines else ''}"
    elif isinstance(item, DeploymentEvidence):
        text = f"deployment {item.observed.version} {item.observed.status}"
        if item.observed.commit:
            text += f" (commit {item.observed.commit})"
    elif isinstance(item, ConfigChangeEvidence):
        text = f"config key {item.observed.key} changed"
    elif isinstance(item, LogEvidence):
        text = f"{item.observed.level}: {item.observed.message}"
    else:
        text = f"span {item.observed.name} status {item.observed.status_code or 'unset'}"
    return text[:SUMMARY_MAX_CHARS]


def _entry(item: Evidence, position: int) -> TimelineEntry:
    return TimelineEntry(
        entry_id=str(item.id),
        position=position,
        occurred_at=item.timestamp,
        end_time=item.observed.end_time if isinstance(item, SpanEvidence) else None,
        entry_type=_entry_type(item),
        evidence_kind=item.kind,
        evidence_ids=[item.id],
        service=item.service,
        deployment_version=item.deployment_version,
        trace_id=item.trace_id,
        request_id=item.request_id,
        summary=_summary(item),
    )


def _services_compatible(a: Evidence, b: Evidence) -> bool:
    return a.service is None or b.service is None or a.service == b.service


def _commit_matches(deploy_commit: str | None, sha: str) -> bool:
    if deploy_commit is None:
        return False
    commit = deploy_commit.lower()
    if len(commit) < _MIN_COMMIT_PREFIX:
        return False
    return commit.startswith(sha) or sha.startswith(commit)


def _format_seconds(delta: timedelta) -> str:
    return f"{delta.total_seconds():g}"


class _Builder:
    def __init__(self, items: list[Evidence], params: TimelineParams) -> None:
        self.items = items
        self.params = params
        self.entries = [_entry(item, pos) for pos, item in enumerate(items)]
        self.links: dict[tuple[int, int], set[LinkIdentifier]] = defaultdict(set)
        self.unknowns: list[TimelineUnknown] = []
        self.version_matches: dict[int, list[int]] = defaultdict(list)

    def _link(self, a: int, b: int, identifier: LinkIdentifier) -> None:
        if a != b:
            self.links[(min(a, b), max(a, b))].add(identifier)

    def _unknown(
        self,
        kind: UnknownKind,
        positions: Sequence[int],
        description: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> None:
        category: Literal["ambiguity", "gap"] = (
            "gap"
            if kind
            in ("unexplained_interval", "missing_telemetry", "truncated_retrieval", "no_evidence")
            else "ambiguity"
        )
        self.unknowns.append(
            TimelineUnknown(
                kind=kind,
                category=category,
                entry_ids=[self.entries[p].entry_id for p in sorted(positions)],
                start=start,
                end=end,
                description=description,
            )
        )

    def _positions(self, cls: type) -> list[int]:
        return [p for p, item in enumerate(self.items) if isinstance(item, cls)]

    def link_shared_ids(self) -> None:
        for field in ("trace_id", "request_id"):
            groups: dict[str, list[int]] = defaultdict(list)
            for pos, item in enumerate(self.items):
                value = getattr(item, field)
                if value is not None:
                    groups[value].append(pos)
            for positions in groups.values():
                for a, b in zip(positions, positions[1:], strict=False):
                    self._link(a, b, field)

    def link_logs_to_spans(self) -> None:
        spans: dict[tuple[str | None, str | None], list[int]] = defaultdict(list)
        for pos in self._positions(SpanEvidence):
            spans[(self.items[pos].trace_id, self.items[pos].span_id)].append(pos)
        for log_pos in self._positions(LogEvidence):
            log = self.items[log_pos]
            if log.trace_id is None or log.span_id is None:
                continue
            for span_pos in spans.get((log.trace_id, log.span_id), []):
                self._link(log_pos, span_pos, "span_id")
                self._check_log_in_span(log_pos, span_pos)

    def link_deployments(self) -> None:
        deployments = self._positions(DeploymentEvidence)
        commits = self._positions(CommitEvidence)
        for d_pos in deployments:
            deploy = self.items[d_pos]
            assert isinstance(deploy, DeploymentEvidence)
            for pos, item in enumerate(self.items):
                if (
                    isinstance(item, DeploymentEvidence)
                    or item.deployment_version != deploy.observed.version
                    or not _services_compatible(deploy, item)
                ):
                    continue
                self._link(d_pos, pos, "deployment_version")
                self.version_matches[pos].append(d_pos)
                if not isinstance(item, CommitEvidence):
                    self._check_change_order(d_pos, pos)
            matched = []
            for c_pos in commits:
                commit = self.items[c_pos]
                assert isinstance(commit, CommitEvidence)
                if _commit_matches(deploy.observed.commit, commit.observed.sha):
                    matched.append(c_pos)
                    self._link(d_pos, c_pos, "commit_sha")
                    self._check_change_order(c_pos, d_pos)
            if len(matched) >= 2:
                self._unknown(
                    "ambiguous_correlation",
                    [d_pos, *matched],
                    f"The deployment commit matches {len(matched)} commits; which one was "
                    "deployed cannot be determined from this evidence.",
                )
        for pos, matches in sorted(self.version_matches.items()):
            if len(matches) >= 2:
                self._unknown(
                    "ambiguous_correlation",
                    [pos, *matches],
                    f"The entry's deployment version matches {len(matches)} deployments; which "
                    "one it belongs to cannot be determined from this evidence.",
                )

    def _flag_order(self, delta: timedelta, earlier: int, later: int, what: str) -> None:
        tolerance = self.params.clock_skew_tolerance
        if delta < -tolerance:
            self._unknown(
                "order_conflict",
                [earlier, later],
                f"Timestamps contradict the {what}; either a clock or the link is wrong.",
            )
        elif abs(delta) <= tolerance:
            self._unknown(
                "clock_skew_suspected",
                [earlier, later],
                f"Timestamps of the {what} are within the clock-skew tolerance, so their "
                "relative order cannot be trusted. Timestamps are kept unchanged.",
            )

    def _check_change_order(self, earlier: int, later: int) -> None:
        delta = self.items[later].timestamp - self.items[earlier].timestamp
        self._flag_order(delta, earlier, later, "expected order of this explicit relationship")

    def _check_log_in_span(self, log_pos: int, span_pos: int) -> None:
        span = self.items[span_pos]
        assert isinstance(span, SpanEvidence)
        log_time = self.items[log_pos].timestamp
        if log_time < span.observed.start_time:
            delta = log_time - span.observed.start_time
        elif log_time > span.observed.end_time:
            delta = span.observed.end_time - log_time
        else:
            return
        self._flag_order(delta, span_pos, log_pos, "log and the span it belongs to")

    def connected(self) -> list[int]:
        parent = list(range(len(self.items)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a, b in self.links:
            parent[find(a)] = find(b)
        return [find(x) for x in range(len(self.items))]

    def temporal_leads(self) -> dict[tuple[int, int], timedelta]:
        component = self.connected()
        leads: dict[tuple[int, int], timedelta] = {}
        changes = [p for p, e in enumerate(self.entries) if e.entry_type in _CHANGE_TYPES]
        symptoms = [p for p, e in enumerate(self.entries) if e.entry_type == "runtime_symptom"]
        for s_pos in symptoms:
            linked_to_change = any((min(s_pos, c), max(s_pos, c)) in self.links for c in changes)
            lead_changes = []
            for c_pos in changes:
                delta = self.items[s_pos].timestamp - self.items[c_pos].timestamp
                if not timedelta(0) <= delta <= self.params.proximity_window:
                    continue
                if not _services_compatible(self.items[s_pos], self.items[c_pos]):
                    continue
                if component[s_pos] == component[c_pos]:
                    continue
                leads[(min(s_pos, c_pos), max(s_pos, c_pos))] = delta
                lead_changes.append(c_pos)
            if not linked_to_change and len(lead_changes) >= 2:
                self._unknown(
                    "ambiguous_correlation",
                    [s_pos, *lead_changes],
                    f"The symptom has no explicit relationship to any change but timing leads "
                    f"to {len(lead_changes)} changes; which one, if any, is relevant is unknown.",
                )
        return leads

    def equal_timestamps(self) -> None:
        groups: dict[datetime, list[int]] = defaultdict(list)
        for pos, item in enumerate(self.items):
            groups[item.timestamp].append(pos)
        for positions in groups.values():
            if len(positions) >= 2:
                self._unknown(
                    "equal_timestamps",
                    positions,
                    f"{len(positions)} entries share the same timestamp; their relative order "
                    "cannot be determined. Listed order is a stable tie-break only.",
                    start=self.items[positions[0]].timestamp,
                )

    def gaps(self) -> None:
        for a in range(len(self.items) - 1):
            start, end = self.items[a].timestamp, self.items[a + 1].timestamp
            if end - start > self.params.gap_threshold:
                self._unknown(
                    "unexplained_interval",
                    [a, a + 1],
                    f"No evidence for {_format_seconds(end - start)} s between consecutive "
                    "entries.",
                    start=start,
                    end=end,
                )
        for d_pos in self._positions(DeploymentEvidence):
            deploy = self.items[d_pos]
            has_telemetry = any(
                isinstance(item, LogEvidence | SpanEvidence)
                and item.timestamp >= deploy.timestamp
                and (deploy.service is None or item.service == deploy.service)
                for item in self.items
            )
            if not has_telemetry:
                self._unknown(
                    "missing_telemetry",
                    [d_pos],
                    "No log or span at or after this deployment is in the retrieved evidence.",
                    start=deploy.timestamp,
                )

    def relationships(self, leads: dict[tuple[int, int], timedelta]) -> list[TimelineRelationship]:
        result: list[tuple[tuple[int, int], TimelineRelationship]] = []
        for (a, b), identifiers in self.links.items():
            names = sorted(identifiers)
            result.append(
                (
                    (a, b),
                    TimelineRelationship(
                        from_entry_id=self.entries[a].entry_id,
                        to_entry_id=self.entries[b].entry_id,
                        basis="explicit",
                        identifiers=names,
                        is_lead=False,
                        statement=f"Linked by shared {', '.join(names)}. {_EXPLICIT_SUFFIX}",
                    ),
                )
            )
        for (a, b), delta in leads.items():
            result.append(
                (
                    (a, b),
                    TimelineRelationship(
                        from_entry_id=self.entries[a].entry_id,
                        to_entry_id=self.entries[b].entry_id,
                        basis="temporal_only",
                        identifiers=[],
                        is_lead=True,
                        statement=f"Occurred {_format_seconds(delta)} s after; {_LEAD_SUFFIX}",
                    ),
                )
            )
        return [rel for _, rel in sorted(result, key=lambda pair: pair[0])]

    def sorted_unknowns(self) -> list[TimelineUnknown]:
        positions = {e.entry_id: e.position for e in self.entries}

        def key(u: TimelineUnknown) -> tuple[str, tuple[int, ...], str, str, str]:
            return (
                u.kind,
                tuple(positions[i] for i in u.entry_ids) or (-1,),
                u.start.isoformat() if u.start else "",
                u.end.isoformat() if u.end else "",
                u.description,
            )

        return sorted(self.unknowns, key=key)


def build_timeline(
    evidence: EvidenceQueryResult | Sequence[Evidence],
    params: TimelineParams | None = None,
) -> Timeline:
    """Order evidence into a timeline of entries, relationships and unknowns. Pure."""
    params = params or TimelineParams()
    if isinstance(evidence, EvidenceQueryResult):
        items, window_start, window_end = list(evidence.items), evidence.start, evidence.end
        truncated = evidence.truncated
    else:
        items, window_start, window_end, truncated = list(evidence), None, None, False
    items.sort(key=lambda item: (item.timestamp, str(item.id)))
    if not items:
        return Timeline(
            window_start=window_start,
            window_end=window_end,
            entries=[],
            relationships=[],
            unknowns=[
                TimelineUnknown(
                    kind="no_evidence",
                    category="gap",
                    entry_ids=[],
                    start=window_start,
                    end=window_end,
                    description="No evidence was retrieved, so no timeline can be built.",
                )
            ],
        )
    builder = _Builder(items, params)
    builder.link_shared_ids()
    builder.link_logs_to_spans()
    builder.link_deployments()
    leads = builder.temporal_leads()
    builder.equal_timestamps()
    builder.gaps()
    if truncated:
        builder._unknown(
            "truncated_retrieval",
            [],
            "Retrieval hit its limit; evidence after the last entry may be missing.",
            start=items[-1].timestamp,
        )
    return Timeline(
        window_start=window_start,
        window_end=window_end,
        entries=builder.entries,
        relationships=builder.relationships(leads),
        unknowns=builder.sorted_unknowns(),
    )
