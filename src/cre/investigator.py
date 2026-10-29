"""Bounded diagnostic tool loop: hypotheses, read-only evidence queries, revision.

Model output is untrusted: its size is capped and it is parsed into strict schemas. The model
may only request tools from the fixed ``TOOLS`` allowlist; tool names, arguments, limits, windows
and call counts are checked in application code, and rejected calls are never executed. Every
query is anchored on the caller's incident anchor and runs read-only through
``retrieve_evidence``. Iteration, token (``ceil(chars / 4)`` proxy) and wall-clock budgets are
enforced between steps. Nothing logs prompts, evidence or model output, and error details are
fixed strings that never echo raw input or exception messages.
"""

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, JsonValue, ValidationError, model_validator
from sqlalchemy import Engine

from cre.bundle import (
    BundleBudgetError,
    BundleLimits,
    EvidenceBundle,
    build_bundle,
    neutralize_delimiters,
)
from cre.config import Settings
from cre.evidence import EventKind, Evidence, TraceId, _Model
from cre.hypotheses import (
    MAX_OUTPUT_CHARS,
    Hypothesis,
    HypothesisOutputError,
    HypothesisResult,
    Uncertainty,
    _strip_fence,
    generate_hypotheses,
    parse_and_validate,
)
from cre.llm import LLMProvider, LLMProviderError, LLMRequest
from cre.prompts import (
    HYPOTHESIS_SYSTEM_PROMPT,
    INVESTIGATION_PROMPT_VERSION,
    MAX_TOOL_CALLS_PLACEHOLDER,
    REVISION_SYSTEM_PROMPT,
    TOOL_REQUEST_SYSTEM_PROMPT,
    render_investigation_prompt,
)
from cre.retrieval import (
    EvidenceQuery,
    EvidenceQueryResult,
    ResolvedQuery,
    RetrievalQueryError,
    resolve_query,
    retrieve_evidence,
)

ToolName = Literal["fetch_by_trace", "fetch_by_service_window", "fetch_changes_in_window"]
StopReason = Literal["concluded", "budget_exhausted", "no_new_evidence", "error"]
BudgetKind = Literal["iterations", "tokens", "wall_clock"]
RejectionReason = Literal[
    "tool_not_allowed", "invalid_arguments", "limit_exceeded", "too_many_calls"
]
ToolExecutor = Callable[[EvidenceQuery], EvidenceQueryResult]
Clock = Callable[[], float]

_TOOL_NAME_MAX_CHARS = 64
_UNCERTAINTY_ORDER: dict[Uncertainty, int] = {"low": 0, "medium": 1, "high": 2}
_NEXT_UNCERTAINTY: dict[Uncertainty, Uncertainty] = {"low": "medium", "medium": "high"}
ServiceName = Annotated[str, Field(min_length=1, max_length=200)]


class _WindowArgs(_Model):
    window_before_seconds: Annotated[int, Field(ge=0)] | None = None
    window_after_seconds: Annotated[int, Field(ge=0)] | None = None
    limit: Annotated[int, Field(ge=1)] | None = None


class FetchByTraceArgs(_WindowArgs):
    trace_id: TraceId


class FetchByServiceWindowArgs(_WindowArgs):
    service: ServiceName


class FetchChangesInWindowArgs(_WindowArgs):
    service: ServiceName


@dataclass(frozen=True)
class ToolSpec:
    name: ToolName
    args_model: type[_WindowArgs]
    kinds: frozenset[EventKind] | None


TOOLS: Mapping[str, ToolSpec] = MappingProxyType(
    {
        "fetch_by_trace": ToolSpec("fetch_by_trace", FetchByTraceArgs, None),
        "fetch_by_service_window": ToolSpec(
            "fetch_by_service_window", FetchByServiceWindowArgs, None
        ),
        "fetch_changes_in_window": ToolSpec(
            "fetch_changes_in_window",
            FetchChangesInWindowArgs,
            frozenset({"deployment", "config_change"}),
        ),
    }
)


class ToolCallRequest(_Model):
    tool: Annotated[str, Field(max_length=_TOOL_NAME_MAX_CHARS)]
    arguments: dict[str, JsonValue]


class ToolRequestOutput(_Model):
    action: Literal["call_tools", "conclude"]
    tool_calls: Annotated[list[ToolCallRequest], Field(max_length=20)] = []
    reason: Annotated[str, Field(max_length=1000)] = ""


class InvestigationBudget(_Model):
    max_iterations: Annotated[int, Field(ge=1)]
    max_total_tokens: Annotated[int, Field(ge=1)]
    per_query_limit: Annotated[int, Field(ge=1)]
    max_tool_calls_per_iteration: Annotated[int, Field(ge=1)]
    wall_clock_seconds: Annotated[float, Field(gt=0)]

    @classmethod
    def from_settings(cls, settings: Settings) -> "InvestigationBudget":
        return cls(
            max_iterations=settings.investigation_max_iterations,
            max_total_tokens=settings.investigation_max_total_tokens,
            per_query_limit=settings.investigation_per_query_limit,
            max_tool_calls_per_iteration=settings.investigation_max_tool_calls_per_iteration,
            wall_clock_seconds=settings.investigation_wall_clock_seconds,
        )


class ToolCallRecord(_Model):
    iteration: int
    tool: str
    status: Literal["executed", "rejected", "failed"]
    rejection_reason: RejectionReason | None = None
    error_type: str | None = None
    arguments: dict[str, JsonValue] | None = None
    retrieval_method: str
    start: datetime | None = None
    end: datetime | None = None
    limit: int | None = None
    count: int = 0
    truncated: bool = False
    evidence_ids: list[UUID] = []
    new_evidence_ids: list[UUID] = []
    executed_at: datetime | None = None


class ContradictionAdjustment(_Model):
    hypothesis_title: str
    contradicting_evidence_ids: list[UUID]
    previous_uncertainty: Uncertainty
    new_uncertainty: Uncertainty


class InvestigationRound(_Model):
    iteration: int
    action: Literal["call_tools", "conclude"]
    tool_calls: list[ToolCallRecord]
    new_evidence_ids: list[UUID]
    revised: bool
    adjustments: list[ContradictionAdjustment]


class InvestigationResult(_Model):
    prompt_version: str
    provider: str
    model: str
    stop_reason: StopReason
    budget_exhausted: BudgetKind | None
    error_detail: str | None
    iterations_used: int
    approx_tokens_used: int
    initial_hypotheses: HypothesisResult | None
    hypotheses: HypothesisResult | None
    rounds: list[InvestigationRound]
    evidence_ids: list[UUID]

    @model_validator(mode="after")
    def _stop_details_consistent(self) -> Self:
        if (self.budget_exhausted is not None) != (self.stop_reason == "budget_exhausted"):
            raise ValueError("budget_exhausted is set exactly when stop_reason is budget_exhausted")
        if (self.error_detail is not None) != (self.stop_reason == "error"):
            raise ValueError("error_detail is set exactly when stop_reason is error")
        return self


class InvestigationCheckpoint(_Model):
    initial_hypotheses: HypothesisResult
    hypotheses: HypothesisResult
    rounds: list[InvestigationRound]
    approx_tokens_used: int
    evidence_ids: list[UUID]


StepSink = Callable[[str, InvestigationCheckpoint], None]


class _MeteredProvider:
    """Wraps a provider and adds the char/4 token proxy for every request and response."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider
        self.name = provider.name
        self.model = provider.model
        self.tokens_used = 0

    def complete(self, request: LLMRequest) -> str:
        self.tokens_used += math.ceil((len(request.system) + len(request.user)) / 4)
        raw = self._provider.complete(request)
        self.tokens_used += math.ceil(len(raw) / 4)
        return raw


class _Stop(Exception):
    def __init__(
        self,
        reason: StopReason,
        budget_kind: BudgetKind | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason: StopReason = reason
        self.budget_kind = budget_kind
        self.detail = detail


def _rejected(iteration: int, tool: str, reason: RejectionReason) -> ToolCallRecord:
    return ToolCallRecord(
        iteration=iteration,
        tool=tool,
        status="rejected",
        rejection_reason=reason,
        retrieval_method=f"tool:{tool}",
    )


def _validate_call(
    call: ToolCallRequest,
    anchor: datetime,
    budget: InvestigationBudget,
    settings: Settings,
) -> tuple[ToolSpec, _WindowArgs, EvidenceQuery, ResolvedQuery] | RejectionReason:
    spec = TOOLS.get(call.tool)
    if spec is None:
        return "tool_not_allowed"
    try:
        args = spec.args_model.model_validate(call.arguments)
    except ValidationError:
        return "invalid_arguments"
    limit = args.limit if args.limit is not None else budget.per_query_limit
    if limit > budget.per_query_limit:
        return "limit_exceeded"
    trace_id = args.trace_id if isinstance(args, FetchByTraceArgs) else None
    service = (
        args.service
        if isinstance(args, FetchByServiceWindowArgs | FetchChangesInWindowArgs)
        else None
    )
    try:
        query = EvidenceQuery(
            trace_id=trace_id,
            service=service,
            anchor=anchor,
            window_before=(
                timedelta(seconds=args.window_before_seconds)
                if args.window_before_seconds is not None
                else None
            ),
            window_after=(
                timedelta(seconds=args.window_after_seconds)
                if args.window_after_seconds is not None
                else None
            ),
            limit=limit,
            kinds=spec.kinds,
        )
        resolved = resolve_query(query, settings)
    except (RetrievalQueryError, ValidationError, OverflowError):
        return "invalid_arguments"
    return spec, args, query, resolved


def _apply_contradiction_rule(
    previous: HypothesisResult, revised: HypothesisResult, new_ids: set[UUID]
) -> tuple[HypothesisResult, list[ContradictionAdjustment]]:
    earlier: dict[str, Hypothesis] = {}
    for hypothesis in previous.hypotheses:
        earlier.setdefault(hypothesis.title, hypothesis)
    adjustments: list[ContradictionAdjustment] = []
    updated: list[Hypothesis] = []
    for hypothesis in revised.hypotheses:
        before = earlier.get(hypothesis.title)
        cited = [i for i in hypothesis.contradicting_evidence_ids if i in new_ids]
        if (
            before is not None
            and cited
            and _UNCERTAINTY_ORDER[hypothesis.uncertainty] <= _UNCERTAINTY_ORDER[before.uncertainty]
            and hypothesis.rank <= before.rank
            and not (before.uncertainty == "high" and hypothesis.uncertainty == "high")
        ):
            raised = _NEXT_UNCERTAINTY.get(before.uncertainty, "high")
            adjustments.append(
                ContradictionAdjustment(
                    hypothesis_title=hypothesis.title,
                    contradicting_evidence_ids=cited,
                    previous_uncertainty=before.uncertainty,
                    new_uncertainty=raised,
                )
            )
            hypothesis = hypothesis.model_copy(update={"uncertainty": raised})
        updated.append(hypothesis)
    return revised.model_copy(update={"hypotheses": updated}), adjustments


def investigate(
    initial_evidence: Sequence[Evidence],
    *,
    anchor: datetime,
    provider: LLMProvider,
    executor: ToolExecutor,
    settings: Settings,
    budget: InvestigationBudget | None = None,
    clock: Clock = time.monotonic,
    checkpoint: InvestigationCheckpoint | None = None,
    on_step: StepSink | None = None,
) -> InvestigationResult:
    """Run the bounded tool loop and return every round with its provenance.

    With ``checkpoint``, initial generation and completed rounds are skipped and the token
    count is restored. ``on_step`` receives a checkpoint after the initial hypotheses and
    after every round that ended with a revision; its exceptions propagate unchanged.
    """
    if not initial_evidence:
        raise ValueError("initial evidence must not be empty")
    if anchor.tzinfo is None or anchor.utcoffset() is None:
        raise ValueError("anchor must be timezone-aware")
    budget = budget or InvestigationBudget.from_settings(settings)
    started = clock()
    metered = _MeteredProvider(provider)
    if checkpoint is not None:
        metered.tokens_used = checkpoint.approx_tokens_used
    limits = BundleLimits(
        max_items=settings.llm_bundle_max_items,
        max_chars=settings.llm_bundle_max_chars,
        max_item_chars=settings.llm_bundle_max_item_chars,
    )
    max_output_tokens = settings.llm_max_output_tokens

    accumulated: list[Evidence] = []
    seen: set[UUID] = set()
    for item in initial_evidence:
        if item.id not in seen:
            seen.add(item.id)
            accumulated.append(item)
    rounds: list[InvestigationRound] = []
    initial: HypothesisResult | None = None
    current: HypothesisResult | None = None
    iterations_used = 0
    if checkpoint is not None:
        initial = checkpoint.initial_hypotheses
        current = checkpoint.hypotheses
        rounds = list(checkpoint.rounds)
        iterations_used = len(rounds)

    def emit(step_key: str) -> None:
        if on_step is None or initial is None or current is None:
            return
        on_step(
            step_key,
            InvestigationCheckpoint(
                initial_hypotheses=initial,
                hypotheses=current,
                rounds=list(rounds),
                approx_tokens_used=metered.tokens_used,
                evidence_ids=[item.id for item in accumulated],
            ),
        )

    def remaining_seconds() -> float:
        remaining = budget.wall_clock_seconds - (clock() - started)
        if remaining <= 0:
            raise _Stop("budget_exhausted", "wall_clock")
        return remaining

    def timeout_for(system: str, user: str) -> float:
        remaining = remaining_seconds()
        reserve = math.ceil((len(system) + len(user)) / 4) + max_output_tokens
        if metered.tokens_used + reserve > budget.max_total_tokens:
            raise _Stop("budget_exhausted", "tokens")
        return min(settings.llm_timeout_seconds, remaining)

    def bundle_of(evidence: Sequence[Evidence]) -> EvidenceBundle:
        try:
            return build_bundle(evidence, limits)
        except BundleBudgetError:
            raise _Stop("error", detail="evidence bundle over budget") from None

    def run_round(iteration: int, bundle: EvidenceBundle, hypotheses: HypothesisResult) -> None:
        records: list[ToolCallRecord] = []
        round_new: list[UUID] = []

        def record(
            action: Literal["call_tools", "conclude"],
            revised: bool = False,
            adjustments: Sequence[ContradictionAdjustment] = (),
        ) -> None:
            rounds.append(
                InvestigationRound(
                    iteration=iteration,
                    action=action,
                    tool_calls=list(records),
                    new_evidence_ids=list(round_new),
                    revised=revised,
                    adjustments=list(adjustments),
                )
            )

        system = TOOL_REQUEST_SYSTEM_PROMPT.replace(
            MAX_TOOL_CALLS_PLACEHOLDER, str(budget.max_tool_calls_per_iteration)
        )
        user = render_investigation_prompt(
            neutralize_delimiters(hypotheses.model_dump_json(include={"hypotheses", "unknowns"})),
            bundle.rendered,
        )
        timeout = timeout_for(system, user)
        try:
            raw = metered.complete(
                LLMRequest(
                    system=system,
                    user=user,
                    max_output_tokens=max_output_tokens,
                    timeout_seconds=timeout,
                )
            )
        except Exception:
            raise _Stop("error", detail="LLM provider call failed") from None
        try:
            if len(raw) > MAX_OUTPUT_CHARS:
                raise ValueError
            output = ToolRequestOutput.model_validate_json(_strip_fence(raw))
        except ValueError:
            raise _Stop("error", detail="tool request output rejected") from None

        if output.action == "conclude":
            record("conclude")
            raise _Stop("concluded")

        executed = 0
        failed = 0
        try:
            for index, call in enumerate(output.tool_calls):
                name = call.tool[:_TOOL_NAME_MAX_CHARS]
                if index >= budget.max_tool_calls_per_iteration:
                    records.append(_rejected(iteration, name, "too_many_calls"))
                    continue
                validated = _validate_call(call, anchor, budget, settings)
                if isinstance(validated, str):
                    records.append(_rejected(iteration, name, validated))
                    continue
                spec, args, query, resolved = validated
                remaining_seconds()
                arguments = args.model_dump(mode="json")
                executed_at = datetime.now(UTC)
                executed += 1
                try:
                    result = executor(query)
                except Exception as exc:
                    failed += 1
                    records.append(
                        ToolCallRecord(
                            iteration=iteration,
                            tool=spec.name,
                            status="failed",
                            error_type=type(exc).__name__,
                            arguments=arguments,
                            retrieval_method=f"tool:{spec.name}",
                            start=resolved.start,
                            end=resolved.end,
                            limit=resolved.limit,
                            executed_at=executed_at,
                        )
                    )
                    continue
                call_new: list[UUID] = []
                for item in result.items:
                    if item.id not in seen:
                        seen.add(item.id)
                        accumulated.append(item)
                        call_new.append(item.id)
                round_new.extend(call_new)
                records.append(
                    ToolCallRecord(
                        iteration=iteration,
                        tool=spec.name,
                        status="executed",
                        arguments=arguments,
                        retrieval_method=f"tool:{spec.name}",
                        start=result.start,
                        end=result.end,
                        limit=result.limit,
                        count=result.count,
                        truncated=result.truncated,
                        evidence_ids=[item.id for item in result.items],
                        new_evidence_ids=call_new,
                        executed_at=executed_at,
                    )
                )
        except _Stop:
            record("call_tools")
            raise

        if executed and failed == executed:
            record("call_tools")
            raise _Stop("error", detail="all tool calls failed")
        if not round_new:
            record("call_tools")
            raise _Stop("no_new_evidence")

        nonlocal current
        try:
            revision_bundle = bundle_of(accumulated)
            user = render_investigation_prompt(
                neutralize_delimiters(
                    hypotheses.model_dump_json(include={"hypotheses", "unknowns"})
                ),
                revision_bundle.rendered,
                round_new,
            )
            timeout = timeout_for(REVISION_SYSTEM_PROMPT, user)
            try:
                raw = metered.complete(
                    LLMRequest(
                        system=REVISION_SYSTEM_PROMPT,
                        user=user,
                        max_output_tokens=max_output_tokens,
                        timeout_seconds=timeout,
                    )
                )
            except Exception:
                raise _Stop("error", detail="LLM provider call failed") from None
            try:
                revised = parse_and_validate(
                    raw, revision_bundle, provider=metered.name, model=metered.model
                )
            except HypothesisOutputError:
                raise _Stop("error", detail="revision output rejected") from None
        except _Stop:
            record("call_tools")
            raise
        revised, adjustments = _apply_contradiction_rule(hypotheses, revised, set(round_new))
        record("call_tools", revised=True, adjustments=adjustments)
        current = revised

    def finish(stop: _Stop) -> InvestigationResult:
        return InvestigationResult(
            prompt_version=INVESTIGATION_PROMPT_VERSION,
            provider=provider.name,
            model=provider.model,
            stop_reason=stop.reason,
            budget_exhausted=stop.budget_kind,
            error_detail=stop.detail,
            iterations_used=iterations_used,
            approx_tokens_used=metered.tokens_used,
            initial_hypotheses=initial,
            hypotheses=current,
            rounds=rounds,
            evidence_ids=[item.id for item in accumulated],
        )

    try:
        if checkpoint is None:
            bundle = bundle_of(accumulated)
            timeout = timeout_for(HYPOTHESIS_SYSTEM_PROMPT, bundle.rendered)
            try:
                initial = generate_hypotheses(
                    bundle, metered, max_output_tokens=max_output_tokens, timeout_seconds=timeout
                )
            except (LLMProviderError, HypothesisOutputError):
                raise _Stop("error", detail="initial hypothesis generation failed") from None
            current = initial
            emit("hypotheses:initial")
        for iteration in range(len(rounds) + 1, budget.max_iterations + 1):
            iterations_used = iteration
            bundle = bundle_of(accumulated)
            assert current is not None
            run_round(iteration, bundle, current)
            emit(f"round:{iteration}")
    except _Stop as stop:
        return finish(stop)
    return finish(_Stop("budget_exhausted", "iterations"))


def make_db_executor(engine: Engine, settings: Settings) -> ToolExecutor:
    """Return an executor that runs each query in its own fresh read-only transaction."""

    def execute(query: EvidenceQuery) -> EvidenceQueryResult:
        with engine.connect() as conn, conn.begin():
            return retrieve_evidence(conn, query, settings)

    return execute
