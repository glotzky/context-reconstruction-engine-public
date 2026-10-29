import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from cre.evidence import ConfigChangeEvidence, ConfigChangeObserved, Evidence, Provenance, SourceRef
from cre.normalizers import normalize_commit, normalize_deployment, normalize_log, normalize_span
from cre.retrieval import EvidenceQueryResult
from cre.timeline import (
    Timeline,
    TimelineEntry,
    TimelineParams,
    TimelineRelationship,
    TimelineUnknown,
    build_timeline,
)
from tests.evidence_samples import (
    COMMIT_SOURCE,
    DEPLOYMENT_SOURCE,
    LOG_SOURCE,
    RETRIEVED_AT,
    SAMPLE_COMMIT,
    SAMPLE_DEPLOYMENT,
    SAMPLE_LOG,
    SAMPLE_SPAN,
    SPAN_SOURCE,
)

COMMIT = normalize_commit(
    SAMPLE_COMMIT, source=COMMIT_SOURCE, retrieved_at=RETRIEVED_AT
).model_copy(update={"id": UUID(int=1)})
DEPLOYMENT = normalize_deployment(
    SAMPLE_DEPLOYMENT, source=DEPLOYMENT_SOURCE, retrieved_at=RETRIEVED_AT
).model_copy(update={"id": UUID(int=2)})
SPAN = normalize_span(SAMPLE_SPAN, source=SPAN_SOURCE, retrieved_at=RETRIEVED_AT).model_copy(
    update={"id": UUID(int=3)}
)
LOG = normalize_log(SAMPLE_LOG, source=LOG_SOURCE, retrieved_at=RETRIEVED_AT).model_copy(
    update={"id": UUID(int=4)}
)
SCENARIO: list[Evidence] = [LOG, DEPLOYMENT, COMMIT, SPAN]
DEPLOYED_AT = DEPLOYMENT.timestamp
CONFIG_SOURCE = SourceRef(source_type="config_change", name="fake config history")
BANNED = re.compile(r"caused|causes|because|due to|root cause", re.IGNORECASE)


def _config(n: int, at: datetime, **fields: Any) -> ConfigChangeEvidence:
    return ConfigChangeEvidence(
        id=UUID(int=n),
        timestamp=at,
        provenance=Provenance(
            source=CONFIG_SOURCE,
            retrieval_method="fake_config_record",
            identifiers={"change_id": f"fake-change-{n}"},
            retrieved_at=RETRIEVED_AT,
        ),
        service="cre-lab-api",
        observed=ConfigChangeObserved(
            key="db.pool_size", old_value="fake-old-5", new_value="fake-new-50"
        ),
        **fields,
    )


def _bare_log(n: int, at: datetime, **update: Any) -> Evidence:
    return LOG.model_copy(
        update={
            "id": UUID(int=n),
            "timestamp": at,
            "trace_id": None,
            "span_id": None,
            "request_id": None,
            "deployment_version": None,
            **update,
        }
    )


def _kinds(timeline: Timeline, kind: str) -> list[TimelineUnknown]:
    return [u for u in timeline.unknowns if u.kind == kind]


def _rel(timeline: Timeline, a: Evidence, b: Evidence) -> TimelineRelationship:
    ids = {str(a.id), str(b.id)}
    (rel,) = [r for r in timeline.relationships if {r.from_entry_id, r.to_entry_id} == ids]
    return rel


def _result(items: list[Evidence], *, truncated: bool) -> EvidenceQueryResult:
    return EvidenceQueryResult(
        trace_id=None,
        service="cre-lab-api",
        anchor=DEPLOYED_AT,
        start=DEPLOYED_AT - timedelta(hours=1),
        end=DEPLOYED_AT + timedelta(hours=1),
        limit=len(items) or 1,
        count=len(items),
        truncated=truncated,
        items=items,
    )


def _all_scenarios() -> list[Timeline]:
    t0 = DEPLOYED_AT
    return [
        build_timeline(SCENARIO),
        build_timeline(SCENARIO, TimelineParams(gap_threshold=timedelta(minutes=5))),
        build_timeline([_config(10, t0), _bare_log(11, t0 + timedelta(minutes=2))]),
        build_timeline(
            [
                _config(10, t0),
                _config(12, t0 + timedelta(minutes=1)),
                _bare_log(11, t0 + timedelta(minutes=2)),
            ]
        ),
        build_timeline(
            [DEPLOYMENT, LOG.model_copy(update={"timestamp": t0 - timedelta(seconds=1)})]
        ),
        build_timeline(
            [DEPLOYMENT, LOG.model_copy(update={"timestamp": t0 - timedelta(seconds=60)})]
        ),
        build_timeline(
            [
                SPAN,
                LOG.model_copy(update={"timestamp": SPAN.observed.end_time + timedelta(seconds=1)}),
            ]
        ),
        build_timeline([DEPLOYMENT, DEPLOYMENT.model_copy(update={"id": UUID(int=20)}), LOG]),
        build_timeline(_result([LOG], truncated=True)),
        build_timeline([]),
    ]


def test_schema_change_entries_are_ordered_with_original_timestamps() -> None:
    timeline = build_timeline(SCENARIO)
    assert [e.entry_id for e in timeline.entries] == [str(UUID(int=n)) for n in (1, 2, 3, 4)]
    assert [e.position for e in timeline.entries] == [0, 1, 2, 3]
    assert [e.entry_type for e in timeline.entries] == [
        "code_commit",
        "deployment",
        "runtime_symptom",
        "runtime_symptom",
    ]
    assert [e.occurred_at for e in timeline.entries] == [
        COMMIT.timestamp,
        DEPLOYMENT.timestamp,
        SPAN.timestamp,
        LOG.timestamp,
    ]
    assert timeline.entries[2].end_time == SPAN.observed.end_time
    assert timeline.entries[0].evidence_ids == [COMMIT.id]
    assert timeline.entries[0].summary == "commit deadbeef0011: Rename customer_name column"
    assert timeline.entries[1].summary == "deployment 0.1.0 succeeded (commit deadbeef)"
    assert timeline.entries[2].summary == "span GET /orders/{order_id} status ERROR"
    assert timeline.entries[3].summary == "ERROR: order_lookup_failed"
    assert timeline.window_start is None and timeline.window_end is None


def test_schema_change_relationships_are_explicit_with_no_leads() -> None:
    timeline = build_timeline(SCENARIO)
    expected = [
        (COMMIT, DEPLOYMENT, ["commit_sha", "deployment_version"]),
        (DEPLOYMENT, LOG, ["deployment_version"]),
        (DEPLOYMENT, SPAN, ["deployment_version"]),
        (SPAN, LOG, ["request_id", "span_id", "trace_id"]),
    ]
    assert len(timeline.relationships) == len(expected)
    for a, b, identifiers in expected:
        rel = _rel(timeline, a, b)
        assert rel.from_entry_id == str(a.id)
        assert rel.basis == "explicit"
        assert rel.is_lead is False
        assert rel.identifiers == identifiers
        assert rel.statement == (
            f"Linked by shared {', '.join(identifiers)}. "
            "This is a correlation, not a determination of cause."
        )
    positions = {e.entry_id: e.position for e in timeline.entries}
    keys = [(positions[r.from_entry_id], positions[r.to_entry_id]) for r in timeline.relationships]
    assert keys == sorted(keys)


def test_schema_change_has_no_unknowns_by_default_and_gap_with_lower_threshold() -> None:
    assert build_timeline(SCENARIO).unknowns == []
    timeline = build_timeline(SCENARIO, TimelineParams(gap_threshold=timedelta(minutes=5)))
    (gap,) = timeline.unknowns
    assert gap.kind == "unexplained_interval"
    assert gap.category == "gap"
    assert gap.start == COMMIT.timestamp
    assert gap.end == DEPLOYMENT.timestamp
    assert gap.entry_ids == [str(COMMIT.id), str(DEPLOYMENT.id)]


def test_temporal_lead_within_window() -> None:
    config = _config(10, DEPLOYED_AT)
    log = _bare_log(11, DEPLOYED_AT + timedelta(minutes=2))
    (rel,) = build_timeline([log, config]).relationships
    assert rel.basis == "temporal_only"
    assert rel.is_lead is True
    assert rel.identifiers == []
    assert (rel.from_entry_id, rel.to_entry_id) == (str(config.id), str(log.id))
    assert rel.statement == (
        "Occurred 120 s after; timing only. This is a lead to check, not evidence of causation."
    )


def test_no_temporal_lead_outside_window() -> None:
    config = _config(10, DEPLOYED_AT)
    log = _bare_log(11, DEPLOYED_AT + timedelta(minutes=20))
    assert build_timeline([config, log]).relationships == []


def test_no_temporal_lead_for_log_before_change() -> None:
    config = _config(10, DEPLOYED_AT)
    log = _bare_log(11, DEPLOYED_AT - timedelta(minutes=1))
    assert build_timeline([config, log]).relationships == []


def test_different_services_give_no_temporal_lead() -> None:
    config = _config(10, DEPLOYED_AT)
    log = _bare_log(11, DEPLOYED_AT + timedelta(minutes=2), service="other-fake-service")
    assert build_timeline([config, log]).relationships == []


def test_non_error_log_is_runtime_event_without_lead() -> None:
    config = _config(10, DEPLOYED_AT)
    log = _bare_log(11, DEPLOYED_AT + timedelta(minutes=2))
    info = log.model_copy(update={"observed": log.observed.model_copy(update={"level": "info"})})
    timeline = build_timeline([config, info])
    assert timeline.entries[1].entry_type == "runtime_event"
    assert timeline.relationships == []


def test_equal_timestamps_across_offsets() -> None:
    at = datetime(2026, 1, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=1)))
    first = _bare_log(2, DEPLOYED_AT)
    second = _bare_log(1, at)
    timeline = build_timeline([first, second])
    (unknown,) = _kinds(timeline, "equal_timestamps")
    assert unknown.category == "ambiguity"
    assert unknown.entry_ids == [str(UUID(int=1)), str(UUID(int=2))]
    assert "cannot be determined" in unknown.description
    assert [e.entry_id for e in timeline.entries] == unknown.entry_ids
    assert timeline.entries[0].occurred_at.utcoffset() == timedelta(hours=1)
    assert build_timeline([second, first]) == timeline


def test_clock_skew_log_just_before_deployment() -> None:
    early = LOG.model_copy(update={"timestamp": DEPLOYED_AT - timedelta(seconds=1)})
    timeline = build_timeline([DEPLOYMENT, early])
    (skew,) = _kinds(timeline, "clock_skew_suspected")
    assert skew.entry_ids == [str(early.id), str(DEPLOYMENT.id)]
    assert _kinds(timeline, "order_conflict") == []
    assert [e.occurred_at for e in timeline.entries] == [early.timestamp, DEPLOYED_AT]


def test_order_conflict_log_well_before_deployment() -> None:
    early = LOG.model_copy(update={"timestamp": DEPLOYED_AT - timedelta(seconds=60)})
    timeline = build_timeline([DEPLOYMENT, early])
    (conflict,) = _kinds(timeline, "order_conflict")
    assert conflict.entry_ids == [str(early.id), str(DEPLOYMENT.id)]
    assert _kinds(timeline, "clock_skew_suspected") == []


def test_clock_skew_log_after_span_end() -> None:
    late = LOG.model_copy(update={"timestamp": SPAN.observed.end_time + timedelta(seconds=1)})
    timeline = build_timeline([SPAN, late])
    (skew,) = _kinds(timeline, "clock_skew_suspected")
    assert skew.entry_ids == [str(SPAN.id), str(late.id)]


def test_log_inside_span_is_not_flagged() -> None:
    timeline = build_timeline([SPAN, LOG])
    assert _kinds(timeline, "clock_skew_suspected") == []
    assert _kinds(timeline, "order_conflict") == []


def test_ambiguous_correlation_two_deployments_same_version() -> None:
    other = DEPLOYMENT.model_copy(
        update={"id": UUID(int=20), "timestamp": DEPLOYED_AT + timedelta(seconds=1)}
    )
    timeline = build_timeline([DEPLOYMENT, other, LOG])
    (ambiguous,) = _kinds(timeline, "ambiguous_correlation")
    assert set(ambiguous.entry_ids) == {str(DEPLOYMENT.id), str(other.id), str(LOG.id)}


def test_ambiguous_correlation_deployment_commit_matches_two_commits() -> None:
    other = COMMIT.model_copy(
        update={
            "id": UUID(int=21),
            "observed": COMMIT.observed.model_copy(update={"sha": "deadbeef99"}),
        }
    )
    timeline = build_timeline([COMMIT, other, DEPLOYMENT])
    (ambiguous,) = _kinds(timeline, "ambiguous_correlation")
    assert set(ambiguous.entry_ids) == {str(COMMIT.id), str(other.id), str(DEPLOYMENT.id)}


def test_short_deployment_commit_does_not_link() -> None:
    short = DEPLOYMENT.model_copy(
        update={"observed": DEPLOYMENT.observed.model_copy(update={"commit": "deadbe"})}
    )
    rel = _rel(build_timeline([COMMIT, short]), COMMIT, short)
    assert rel.identifiers == ["deployment_version"]


def test_ambiguous_correlation_symptom_with_two_unlinked_leads() -> None:
    first = _config(10, DEPLOYED_AT)
    second = _config(12, DEPLOYED_AT + timedelta(minutes=1))
    log = _bare_log(11, DEPLOYED_AT + timedelta(minutes=2))
    timeline = build_timeline([first, second, log])
    assert [r.basis for r in timeline.relationships] == ["temporal_only", "temporal_only"]
    (ambiguous,) = _kinds(timeline, "ambiguous_correlation")
    assert ambiguous.entry_ids == [str(first.id), str(second.id), str(log.id)]


def test_missing_telemetry_after_deployment() -> None:
    timeline = build_timeline([COMMIT, DEPLOYMENT])
    (missing,) = _kinds(timeline, "missing_telemetry")
    assert missing.category == "gap"
    assert missing.entry_ids == [str(DEPLOYMENT.id)]


def test_missing_telemetry_ignores_other_service_logs() -> None:
    other = _bare_log(11, DEPLOYED_AT + timedelta(seconds=5), service="other-fake-service")
    timeline = build_timeline([DEPLOYMENT, other])
    assert len(_kinds(timeline, "missing_telemetry")) == 1


def test_truncated_retrieval_copies_window() -> None:
    result = _result([LOG, SPAN], truncated=True)
    timeline = build_timeline(result)
    (truncated,) = _kinds(timeline, "truncated_retrieval")
    assert truncated.entry_ids == []
    assert truncated.start == LOG.timestamp
    assert "may be missing" in truncated.description
    assert timeline.window_start == result.start
    assert timeline.window_end == result.end


def test_untruncated_result_has_no_truncation_unknown() -> None:
    assert _kinds(build_timeline(_result([LOG], truncated=False)), "truncated_retrieval") == []


@pytest.mark.parametrize("given", [[], _result([], truncated=False)])
def test_empty_input_gives_only_no_evidence(given: Any) -> None:
    timeline = build_timeline(given)
    assert timeline.entries == []
    assert timeline.relationships == []
    assert [u.kind for u in timeline.unknowns] == ["no_evidence"]
    if isinstance(given, EvidenceQueryResult):
        assert timeline.window_start == given.start
        assert timeline.window_end == given.end


def test_summary_safety() -> None:
    (entry,) = build_timeline([_config(10, DEPLOYED_AT)]).entries
    assert entry.summary == "config key db.pool_size changed"
    assert "fake-old-5" not in entry.summary and "fake-new-50" not in entry.summary
    long_log = LOG.model_copy(
        update={"observed": LOG.observed.model_copy(update={"message": "x" * 500})}
    )
    long_commit = COMMIT.model_copy(
        update={"observed": COMMIT.observed.model_copy(update={"message": "y" * 500 + "\nz"})}
    )
    for item in (long_log, long_commit):
        (entry,) = build_timeline([item]).entries
        assert len(entry.summary) <= 200


def test_no_causation_wording_or_fields() -> None:
    for timeline in _all_scenarios():
        texts = [r.statement for r in timeline.relationships]
        texts += [u.description for u in timeline.unknowns]
        for text in texts:
            assert not BANNED.search(text), text
    for model in (Timeline, TimelineEntry, TimelineRelationship, TimelineUnknown, TimelineParams):
        assert not any("cause" in name for name in model.model_fields)


def test_determinism_repeat_and_reversed_input() -> None:
    items = [*SCENARIO, _config(10, DEPLOYED_AT - timedelta(minutes=1))]
    first = build_timeline(items)
    assert build_timeline(items) == first
    assert build_timeline(list(reversed(items))) == first


def test_params_reject_invalid_values() -> None:
    with pytest.raises(ValidationError):
        TimelineParams(clock_skew_tolerance=timedelta(seconds=-1))
    with pytest.raises(ValidationError):
        TimelineParams(gap_threshold=timedelta(0))
    with pytest.raises(ValidationError):
        TimelineParams(proximity_window=timedelta(seconds=-5))
    assert TimelineParams(clock_skew_tolerance=timedelta(0)).clock_skew_tolerance == timedelta(0)


def test_relationship_rejects_inconsistent_basis() -> None:
    base: dict[str, Any] = {"from_entry_id": "a", "to_entry_id": "b", "statement": "fake"}
    with pytest.raises(ValidationError):
        TimelineRelationship(basis="temporal_only", identifiers=["trace_id"], is_lead=True, **base)
    with pytest.raises(ValidationError):
        TimelineRelationship(basis="explicit", identifiers=["trace_id"], is_lead=True, **base)
    with pytest.raises(ValidationError):
        TimelineRelationship(basis="explicit", identifiers=[], is_lead=False, **base)
    with pytest.raises(ValidationError):
        TimelineRelationship(
            basis="explicit", identifiers=["trace_id", "request_id"], is_lead=False, **base
        )


def test_entries_are_utc_comparable() -> None:
    timeline = build_timeline(SCENARIO)
    assert all(e.occurred_at.tzinfo is not None for e in timeline.entries)
    assert timeline.entries[1].occurred_at == datetime(2026, 1, 1, tzinfo=UTC)


def test_skew_boundary_exactly_at_tolerance_is_skew_not_conflict() -> None:
    early = LOG.model_copy(update={"timestamp": DEPLOYED_AT - timedelta(seconds=2)})
    timeline = build_timeline([DEPLOYMENT, early])
    assert len(_kinds(timeline, "clock_skew_suspected")) == 1
    assert _kinds(timeline, "order_conflict") == []


def test_just_past_tolerance_is_order_conflict() -> None:
    early = LOG.model_copy(update={"timestamp": DEPLOYED_AT - timedelta(seconds=3)})
    timeline = build_timeline([DEPLOYMENT, early])
    assert len(_kinds(timeline, "order_conflict")) == 1
    assert _kinds(timeline, "clock_skew_suspected") == []


def test_log_shortly_after_deployment_is_skew_suspected() -> None:
    late = LOG.model_copy(update={"timestamp": DEPLOYED_AT + timedelta(seconds=1)})
    timeline = build_timeline([DEPLOYMENT, late])
    assert len(_kinds(timeline, "clock_skew_suspected")) == 1


def test_shuffled_mixed_offsets_give_same_timeline() -> None:
    shifted = _bare_log(30, DEPLOYED_AT.astimezone(timezone(timedelta(hours=5))))
    items = [*SCENARIO, shifted]
    expected = build_timeline(items)
    assert build_timeline(items[3:] + items[:3]) == expected
    assert build_timeline(list(reversed(items))) == expected
