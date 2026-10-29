"""Bounded, redacted, delimited evidence bundle for hypothesis generation.

Every item is redacted before use and rendered inside neutralized delimiters, so untrusted text
cannot close a data or evidence block. Selection is deterministic: symptoms first, then changes,
then other runtime events, each tie broken by timeline position. Items that do not fit the
budget are omitted and recorded. The token budget is a character proxy (``ceil(chars / 4)``).
Pure: no DB, no network, no logging of evidence content.
"""

import json
import math
from collections.abc import Sequence
from typing import Annotated
from uuid import UUID

from pydantic import Field

from cre.evidence import Evidence, _Model
from cre.prompts import HYPOTHESIS_PROMPT_VERSION, render_user_prompt
from cre.redaction import redact_evidence, redact_text
from cre.retrieval import EvidenceQueryResult
from cre.timeline import EntryType, Timeline, TimelineParams, build_timeline

TRUNCATION_MARKER = "…[truncated]"
_PRIORITY: dict[EntryType, int] = {
    "runtime_symptom": 0,
    "deployment": 1,
    "code_commit": 1,
    "config_change": 1,
    "runtime_event": 2,
}


class BundleBudgetError(ValueError):
    """The budget cannot hold the bundle frame or a single evidence item."""


class BundleLimits(_Model):
    max_items: Annotated[int, Field(ge=1, le=1000)] = 50
    max_chars: Annotated[int, Field(ge=1000, le=1_000_000)] = 48_000
    max_item_chars: Annotated[int, Field(ge=200, le=100_000)] = 2_000


class EvidenceBundle(_Model):
    prompt_version: str
    evidence: list[Evidence]
    timeline: Timeline
    omitted_evidence_ids: list[UUID]
    truncated_evidence_ids: list[UUID]
    rendered: str
    char_count: int
    approx_tokens: int

    @property
    def evidence_ids(self) -> frozenset[UUID]:
        return frozenset(item.id for item in self.evidence)


def neutralize_delimiters(text: str) -> str:
    """Break every ``<<<`` and ``>>>`` so untrusted text cannot form a delimiter."""
    while "<<<" in text or ">>>" in text:
        text = text.replace("<<<", "< < <").replace(">>>", "> > >")
    return text


def _render_item(item: Evidence, max_item_chars: int) -> tuple[str, bool]:
    body = neutralize_delimiters(
        redact_text(json.dumps(item.model_dump(mode="json"), sort_keys=True))
    )
    truncated = len(body) > max_item_chars
    if truncated:
        body = body[:max_item_chars] + TRUNCATION_MARKER
    block = f"<<<EVIDENCE id={item.id} kind={item.kind}>>>\n{body}\n<<<END_EVIDENCE>>>"
    return block, truncated


def _filter_timeline(timeline: Timeline, kept_ids: set[str]) -> Timeline:
    return timeline.model_copy(
        update={
            "entries": [e for e in timeline.entries if e.entry_id in kept_ids],
            "relationships": [
                r
                for r in timeline.relationships
                if r.from_entry_id in kept_ids and r.to_entry_id in kept_ids
            ],
            "unknowns": [
                u.model_copy(update={"entry_ids": [i for i in u.entry_ids if i in kept_ids]})
                for u in timeline.unknowns
            ],
        }
    )


def _omitted_line(count: int) -> str:
    return (
        f"OMITTED: {count} evidence items were left out to fit the budget "
        "and are not available to cite."
    )


def _render(blocks: list[str], timeline: Timeline, omitted: int) -> str:
    timeline_json = json.dumps(timeline.model_dump(mode="json"), sort_keys=True)
    parts = [*blocks, f"TIMELINE: {neutralize_delimiters(timeline_json)}"]
    if omitted:
        parts.append(_omitted_line(omitted))
    return render_user_prompt("\n\n".join(parts))


def build_bundle(
    evidence: EvidenceQueryResult | Sequence[Evidence],
    limits: BundleLimits | None = None,
    params: TimelineParams | None = None,
) -> EvidenceBundle:
    """Redact, select and render evidence into a bounded user prompt. Pure."""
    limits = limits or BundleLimits()
    source: EvidenceQueryResult | list[Evidence]
    if isinstance(evidence, EvidenceQueryResult):
        redacted = [redact_evidence(item) for item in evidence.items]
        source = evidence.model_copy(update={"items": redacted})
    else:
        redacted = [redact_evidence(item) for item in evidence]
        source = redacted
    timeline = build_timeline(source, params)
    ordered = sorted(redacted, key=lambda item: (item.timestamp, str(item.id)))
    total = len(ordered)

    if len(_render([], _filter_timeline(timeline, set()), total)) > limits.max_chars:
        raise BundleBudgetError("bundle frame alone exceeds max_chars")

    rendered_items = [_render_item(item, limits.max_item_chars) for item in ordered]
    priority = sorted(
        range(total), key=lambda pos: (_PRIORITY[timeline.entries[pos].entry_type], pos)
    )
    kept: list[int] = []
    for pos in priority:
        if len(kept) >= limits.max_items:
            break
        candidate = sorted([*kept, pos])
        candidate_ids = {str(ordered[i].id) for i in candidate}
        text = _render(
            [rendered_items[i][0] for i in candidate],
            _filter_timeline(timeline, candidate_ids),
            total - len(candidate),
        )
        if len(text) <= limits.max_chars:
            kept = candidate
    if total and not kept:
        raise BundleBudgetError("no evidence item fits within max_chars")

    kept_set = set(kept)
    kept_ids = {str(ordered[i].id) for i in kept}
    filtered = _filter_timeline(timeline, kept_ids)
    omitted = sorted((ordered[i].id for i in range(total) if i not in kept_set), key=str)
    rendered = _render([rendered_items[i][0] for i in kept], filtered, len(omitted))
    return EvidenceBundle(
        prompt_version=HYPOTHESIS_PROMPT_VERSION,
        evidence=[ordered[i] for i in kept],
        timeline=filtered,
        omitted_evidence_ids=omitted,
        truncated_evidence_ids=[ordered[i].id for i in kept if rendered_items[i][1]],
        rendered=rendered,
        char_count=len(rendered),
        approx_tokens=math.ceil(len(rendered) / 4),
    )
