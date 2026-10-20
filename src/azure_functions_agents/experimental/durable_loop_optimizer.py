"""Segment optimizer combining Decision-1 routing, prefix-cache cost ledger, and inline DAG execution."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from ..strict_json import canonical_json_bytes
from ..workflows.workflow_schema import (
    PlanValidationError,
    TemplateResolutionError,
    WorkflowPlan,
    WorkflowPlanPolicy,
    WorkflowTask,
    resolve_template_value,
    validate_plan,
)
from .durable_chat_execution_observer import DurableChatExecutionObserver
from .durable_chat_protocol import DurableChatModelProducerV1
from .durable_loop_activities import (
    DurableChatStreamingModelProvider,
    DurableContentStore,
    OneStepModelProvider,
    OneStepModelRequest,
    get_protocol_model,
    put_protocol_model,
)
from .durable_loop_config import DurableLoopSettings
from .durable_loop_protocol import (
    ContentRefV1,
    DurableFaultProfile,
    DurableRunDocumentV1,
    ErrorEnvelopeV1,
    FrozenToolCatalogV1,
    FrozenToolDescriptorV1,
    MAFMessageBundleV1,
    ModelDecisionEnvelopeV1,
    PrefixCacheWatermarkV1,
    SandboxExecutionProfile,
    SegmentOptimizerStateV1,
    ToolBehavior,
    ToolDispatchRefV1,
    ToolProvenance,
    ToolRequestV1,
    ToolResultRefV1,
    ToolResultStatus,
    ToolResultV1,
    canonical_hash,
    deterministic_call_key,
    tool_request_hash,
)
from .durable_loop_tools import (
    REQUEST_HUMAN_INPUT_TOOL_NAME,
    DurableRetainedSandboxInspection,
    DurableRetainedSandboxInspectionPort,
    DurableToolCatalogPort,
    DurableToolCatalogSnapshot,
    DurableToolCleanupPort,
    DurableToolDispatchPort,
)

COMPILE_SEGMENT_DAG_TOOL_NAME = "compile_segment_dag"
DECISION_SYSTEMONE_PATH = "/providers/microsoft/v1/systemone"

_ASSUMED_FRONTIER_INPUT_RATE_MICROUNITS = 2_500_000
_ASSUMED_FRONTIER_CACHED_INPUT_RATE_MICROUNITS = 250_000
_ASSUMED_FRONTIER_OUTPUT_RATE_MICROUNITS = 10_000_000
_ASSUMED_FAST_INPUT_RATE_MICROUNITS = 250_000
_ASSUMED_FAST_CACHED_INPUT_RATE_MICROUNITS = 25_000
_ASSUMED_FAST_OUTPUT_RATE_MICROUNITS = 1_000_000
_ASSUMED_DECISION_INPUT_RATE_MICROUNITS = 42_000

_CACHE_TTL_SECONDS = 300.0
_CACHE_MIN_TOKENS = 1024
_CACHE_BUCKET_TOKENS = 128
ESTIMATED_STEP_OUTPUT_TOKENS = 250
_DEFAULT_DAG_THRESHOLD = 0.70
_DEFAULT_FAST_THRESHOLD = 0.75
_DECISION_AGENT_INSTRUCTIONS_MAX_CHARS = 16_384
_DECISION_RECENT_MESSAGE_MAX_CHARS = 4_096
_DECISION_RECENT_CONVERSATION_MAX_CHARS = 24_576
_DECISION_RECENT_CONVERSATION_MAX_MESSAGES = 12
_DECISION_TOOL_DESCRIPTION_MAX_CHARS = 1_024
_DECISION_TOOL_CATALOG_MAX_CHARS = 16_384
_DECISION_TOOL_CATALOG_MAX_TOOLS = 64
_DECISION_COMPLETED_TOOLS_MAX_COUNT = 64
_DECISION_USER_GOAL_MAX_CHARS = 8_192
_TRUNCATION_MARKER = "\n...[truncated]...\n"

_CREDENTIAL_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|account[_-]?key|authorization|client[_-]?secret|"
    r"connection[_-]?string|password|secret|subscription[_-]?key|token)\b"
    r"(\s*[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER_TOKEN_PATTERN = re.compile(r"(?i)\bbearer\s+[^\s,;]+")

_DAG_PLANNING_ADDENDUM = (
    "\n\nSegment Optimizer DAG Mode: When 2 or more tool calls can be planned "
    "upfront (in parallel or chained via ${node_id.result.field} templates) "
    "without inspecting intermediate results first, emit a single "
    "`compile_segment_dag` tool call containing the `tasks` DAG instead of "
    "calling tools one turn at a time."
)


class ModelTier(StrEnum):
    """Reasoning tier selected for a durable segment."""

    FAST = "fast"
    FRONTIER = "frontier"


class SegmentMode(StrEnum):
    """Execution shape selected for the next segment."""

    STEP = "step"
    DAG = "dag"


class RouteDecisionSource(StrEnum):
    """Bounded origin of one segment route."""

    LIVE = "live"
    HEURISTIC = "heuristic"
    SHORT_CIRCUIT = "short_circuit"


class ModelRateSource(StrEnum):
    """Provenance for one static model-pricing snapshot."""

    AZURE_RETAIL_GLOBAL_STANDARD_SHORT_CONTEXT = (
        "azure_retail_global_standard_short_context_2026-10-20"
    )
    ASSUMED_TIER_DEFAULT = "assumed_tier_default"


@dataclass(frozen=True, slots=True)
class ModelTokenRates:
    """USD microunits per million tokens for one exact model identity."""

    input_microunits: int
    cached_input_microunits: int
    output_microunits: int
    source: ModelRateSource


_VERIFIED_MODEL_RATES = {
    "gpt-6-luna": ModelTokenRates(
        input_microunits=100_000,
        cached_input_microunits=10_000,
        output_microunits=500_000,
        source=ModelRateSource.AZURE_RETAIL_GLOBAL_STANDARD_SHORT_CONTEXT,
    ),
    "gpt-6.1-sol": ModelTokenRates(
        input_microunits=2_000_000,
        cached_input_microunits=100_000,
        output_microunits=10_000_000,
        source=ModelRateSource.AZURE_RETAIL_GLOBAL_STANDARD_SHORT_CONTEXT,
    ),
}


@dataclass(frozen=True, slots=True)
class ModelWatermark:
    """Per-model prompt-cache watermark recorded after an inference step."""

    model: str
    message_count: int
    prefix_hash: str
    input_tokens: int
    cached_tokens: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StepCostEstimate:
    """Deterministic warm vs. cold microunit cost estimate before a step."""

    estimated_input_tokens: int
    frontier_warm_tokens: int
    frontier_cold_tokens: int
    frontier_cost_microunits: int
    fast_warm_tokens: int
    fast_cold_tokens: int
    fast_cost_microunits: int
    frontier_cache_warm: bool


@dataclass(frozen=True, slots=True)
class DecisionEvaluation:
    """Structured output returned by Microsoft-Decision-1 (`/v1/systemone`)."""

    dag_viable: float
    step_tier: ModelTier
    step_fast_probability: float
    dag_tier: ModelTier
    dag_fast_probability: float
    decision_input_tokens: int
    source: RouteDecisionSource = RouteDecisionSource.HEURISTIC
    fallback_reason: str | None = None
    http_status: int | None = None
    duration_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class SegmentRouteDecision:
    """Recorded routing decision for one durable loop step."""

    run_id: str
    step_index: int
    mode: SegmentMode
    tier: ModelTier
    target_model: str
    short_circuited: bool
    reason: str
    cost_estimate: StepCostEstimate
    evaluation: DecisionEvaluation | None = None
    source: RouteDecisionSource = RouteDecisionSource.SHORT_CIRCUIT
    recorded_at: datetime | None = None


def compile_segment_dag_tool_descriptor() -> FrozenToolDescriptorV1:
    """Return the model-visible descriptor for inline bounded DAG execution."""
    return FrozenToolDescriptorV1(
        name=COMPILE_SEGMENT_DAG_TOOL_NAME,
        description=(
            "Execute a bounded multi-tool DAG in parallel/topological waves "
            "and return one collapsed result. Tasks may reference upstream "
            "outputs via ${node_id.result} or ${node_id.result.field}."
        ),
        parameters={
            "additionalProperties": False,
            "properties": {
                "tasks": {
                    "items": {
                        "additionalProperties": False,
                        "properties": {
                            "args": {"type": "object"},
                            "depends_on": {
                                "items": {"type": "string"},
                                "type": "array",
                            },
                            "id": {"type": "string"},
                            "tool": {"type": "string"},
                            "type": {"enum": ["tool"], "type": "string"},
                        },
                        "required": ["id", "tool"],
                        "type": "object",
                    },
                    "minItems": 2,
                    "type": "array",
                }
            },
            "required": ["tasks"],
            "type": "object",
        },
        provenance=ToolProvenance.REMOTE,
        behavior=ToolBehavior.READ_ONLY,
        parallel_safe=False,
    )


class PrefixCacheLedger:
    """Track per-model prefix watermarks and compute warm/cold step economics."""

    def __init__(self) -> None:
        self._watermarks: dict[tuple[str, str], ModelWatermark] = {}
        self._last_routed_model: dict[str, str] = {}
        self._last_step_was_dag: dict[str, bool] = {}

    def last_routed_model(self, run_id: str) -> str | None:
        """Return the model used on the previous step of this run."""
        return self._last_routed_model.get(run_id)

    def last_step_was_dag(self, run_id: str) -> bool:
        """Return whether the previous step compiled and ran a sub-DAG."""
        return self._last_step_was_dag.get(run_id, False)

    def restore(
        self,
        *,
        run_id: str,
        state: SegmentOptimizerStateV1 | None,
    ) -> None:
        """Replace one run's worker-local view from its durable snapshot."""
        for key in tuple(self._watermarks):
            if key[0] == run_id:
                del self._watermarks[key]
        self._last_routed_model.pop(run_id, None)
        self._last_step_was_dag.pop(run_id, None)
        if state is None:
            return
        for watermark in state.watermarks:
            self._watermarks[(run_id, watermark.model)] = ModelWatermark(
                model=watermark.model,
                message_count=watermark.message_count,
                prefix_hash=watermark.prefix_hash,
                input_tokens=watermark.input_tokens,
                cached_tokens=watermark.cached_tokens,
                updated_at=watermark.updated_at,
            )
        if state.last_routed_model is not None:
            self._last_routed_model[run_id] = state.last_routed_model
        self._last_step_was_dag[run_id] = state.last_step_was_dag

    def snapshot(self, *, run_id: str) -> SegmentOptimizerStateV1:
        """Return the bounded durable snapshot for one run."""
        watermarks = tuple(
            PrefixCacheWatermarkV1(
                model=watermark.model,
                message_count=watermark.message_count,
                prefix_hash=watermark.prefix_hash,
                input_tokens=watermark.input_tokens,
                cached_tokens=watermark.cached_tokens,
                updated_at=watermark.updated_at,
            )
            for (watermark_run_id, _model), watermark in sorted(
                self._watermarks.items()
            )
            if watermark_run_id == run_id
        )
        return SegmentOptimizerStateV1(
            watermarks=watermarks,
            last_routed_model=self._last_routed_model.get(run_id),
            last_step_was_dag=self._last_step_was_dag.get(run_id, False),
        )

    def estimate_step_cost(
        self,
        *,
        run_id: str,
        messages: Sequence[dict[str, object]],
        catalog: FrozenToolCatalogV1,
        frontier_model: str,
        fast_model: str,
        now: datetime,
    ) -> StepCostEstimate:
        """Compute warm vs. cold expected step cost for Frontier and Fast."""
        schema_tokens = max(
            1,
            len(canonical_json_bytes([t.model_dump(mode="json") for t in catalog.tools])) // 4,
        )
        msg_tokens = max(1, len(canonical_json_bytes(tuple(messages))) // 4)
        total_input_tokens = schema_tokens + msg_tokens

        frontier_warm, frontier_cold = self._split_warm_cold(
            run_id=run_id,
            model=frontier_model,
            messages=messages,
            total_input_tokens=total_input_tokens,
            now=now,
        )
        fast_warm, fast_cold = self._split_warm_cold(
            run_id=run_id,
            model=fast_model,
            messages=messages,
            total_input_tokens=total_input_tokens,
            now=now,
        )
        frontier_cost = _compute_tier_cost_microunits(
            model=frontier_model,
            tier=ModelTier.FRONTIER,
            warm_input_tokens=frontier_warm,
            cold_input_tokens=frontier_cold,
            output_tokens=ESTIMATED_STEP_OUTPUT_TOKENS,
        )
        switch_penalty_tokens = frontier_warm if frontier_warm > 0 and fast_warm == 0 else 0
        fast_cost = _compute_tier_cost_microunits(
            model=fast_model,
            tier=ModelTier.FAST,
            warm_input_tokens=fast_warm,
            cold_input_tokens=fast_cold + switch_penalty_tokens,
            output_tokens=ESTIMATED_STEP_OUTPUT_TOKENS,
        )
        return StepCostEstimate(
            estimated_input_tokens=total_input_tokens,
            frontier_warm_tokens=frontier_warm,
            frontier_cold_tokens=frontier_cold,
            frontier_cost_microunits=frontier_cost,
            fast_warm_tokens=fast_warm,
            fast_cold_tokens=fast_cold,
            fast_cost_microunits=fast_cost,
            frontier_cache_warm=frontier_warm > 0,
        )

    def record_step(
        self,
        *,
        run_id: str,
        model: str,
        tier: ModelTier,
        messages: Sequence[dict[str, object]],
        input_tokens: int,
        cached_tokens: int,
        output_tokens: int,
        decision_input_tokens: int,
        emitted_dag: bool,
        now: datetime,
    ) -> int:
        """Update watermark and return total step cost in microunits."""
        effective_input = max(
            input_tokens,
            max(1, len(canonical_json_bytes(tuple(messages))) // 4),
        )
        warm_tokens = cached_tokens
        if warm_tokens == 0:
            warm_tokens, _ = self._split_warm_cold(
                run_id=run_id,
                model=model,
                messages=messages,
                total_input_tokens=effective_input,
                now=now,
            )
        cold_tokens = max(0, effective_input - warm_tokens)
        self._watermarks[(run_id, model)] = ModelWatermark(
            model=model,
            message_count=len(messages),
            prefix_hash=canonical_hash(tuple(messages)),
            input_tokens=effective_input,
            cached_tokens=warm_tokens,
            updated_at=now,
        )
        self._last_routed_model[run_id] = model
        self._last_step_was_dag[run_id] = emitted_dag

        model_cost = _compute_tier_cost_microunits(
            model=model,
            tier=tier,
            warm_input_tokens=warm_tokens,
            cold_input_tokens=cold_tokens,
            output_tokens=output_tokens,
        )
        decision_cost = math.ceil(
            (decision_input_tokens * _ASSUMED_DECISION_INPUT_RATE_MICROUNITS)
            / 1_000_000
        )
        return model_cost + decision_cost

    def _split_warm_cold(
        self,
        *,
        run_id: str,
        model: str,
        messages: Sequence[dict[str, object]],
        total_input_tokens: int,
        now: datetime,
    ) -> tuple[int, int]:
        watermark = self._watermarks.get((run_id, model))
        if watermark is None:
            return 0, total_input_tokens
        age_seconds = (now - watermark.updated_at).total_seconds()
        if age_seconds > _CACHE_TTL_SECONDS or len(messages) < watermark.message_count:
            return 0, total_input_tokens
        prefix = tuple(messages[: watermark.message_count])
        if canonical_hash(prefix) != watermark.prefix_hash:
            return 0, total_input_tokens
        cached_candidate = (
            total_input_tokens
            if len(messages) == watermark.message_count
            else watermark.input_tokens
        )
        if cached_candidate < _CACHE_MIN_TOKENS:
            return 0, total_input_tokens
        bucketed = (cached_candidate // _CACHE_BUCKET_TOKENS) * _CACHE_BUCKET_TOKENS
        warm = min(total_input_tokens, bucketed)
        return warm, max(0, total_input_tokens - warm)


@runtime_checkable
class DecisionOneClient(Protocol):
    """Client seam for Microsoft-Decision-1 (`/providers/microsoft/v1/systemone`)."""

    async def evaluate(
        self,
        payload: Mapping[str, object],
    ) -> DecisionEvaluation:
        """Score `dag_viable`, `step_tier`, and `dag_tier` in one pass."""


def build_decision_one_payload(
    *,
    decision_model: str,
    state: Mapping[str, object],
) -> dict[str, object]:
    """Construct the `/providers/microsoft/v1/systemone` request payload."""
    return {
        "model": decision_model,
        "state": dict(state),
        "questions": {
            "dag_viable": {
                "type": "noul",
                "instructions": (
                    "Return true only when the next 2+ executable tool calls can "
                    "be fully specified now as one fixed DAG. Static arguments "
                    "and declared ${node.result.field} dependencies are allowed. "
                    "Return false when there is no executable task, only one tool "
                    "call, missing arguments, clarification, branching, recovery, "
                    "or any need to inspect an intermediate output before deciding "
                    "the next call."
                ),
            },
            "step_tier": {
                "type": "choice",
                "instructions": (
                    "Choose the minimum sufficient configured tier for the next "
                    "single step. Judge the actual next action, not casual wording "
                    "or prompt length."
                ),
                "criteria": {
                    ModelTier.FAST.value: (
                        "Greetings, small talk, acknowledgements, simple "
                        "clarification, direct answers, straightforward summaries, "
                        "or one obvious tool call with known arguments."
                    ),
                    ModelTier.FRONTIER.value: (
                        "Substantive next-action reasoning, consequential ambiguity, "
                        "multi-constraint synthesis, or recovery after failure. "
                        "Do not choose frontier merely for casual wording or when "
                        "there is no executable task."
                    ),
                },
            },
            "dag_tier": {
                "type": "choice",
                "instructions": (
                    "If a fixed multi-tool DAG is viable, choose the minimum "
                    "sufficient configured tier to author it."
                ),
                "criteria": {
                    ModelTier.FAST.value: (
                        "Simple parallel fan-out or an obvious fixed dependency "
                        "chain with fully known arguments."
                    ),
                    ModelTier.FRONTIER.value: (
                        "A fixed DAG whose construction still requires substantive "
                        "reasoning across constraints or consequential ambiguity. "
                        "Template dependencies alone do not require frontier."
                    ),
                },
            },
        },
    }


def parse_decision_one_response(
    response: Mapping[str, object],
    *,
    fallback_input_tokens: int,
) -> DecisionEvaluation:
    """Parse `/providers/microsoft/v1/systemone` structured answers."""
    answers = response.get("answers")
    if not isinstance(answers, Mapping):
        raise ValueError("Decision-1 response is missing answers")

    dag_answer = answers.get("dag_viable")
    dag_viable = 0.0
    if isinstance(dag_answer, Mapping):
        raw_noul = dag_answer.get("noul", dag_answer.get("probability", 0.0))
        if isinstance(raw_noul, int | float) and not isinstance(raw_noul, bool):
            dag_viable = float(max(0.0, min(1.0, raw_noul)))

    step_tier, step_fast_prob = _parse_tier_choice(answers.get("step_tier"))
    dag_tier, dag_fast_prob = _parse_tier_choice(answers.get("dag_tier"))

    usage = response.get("usage")
    decision_tokens = fallback_input_tokens
    if isinstance(usage, Mapping):
        for key in ("input_tokens", "prompt_tokens", "input_token_count"):
            raw_tokens = usage.get(key)
            if isinstance(raw_tokens, int) and not isinstance(raw_tokens, bool) and raw_tokens > 0:
                decision_tokens = raw_tokens
                break

    return DecisionEvaluation(
        dag_viable=dag_viable,
        step_tier=step_tier,
        step_fast_probability=step_fast_prob,
        dag_tier=dag_tier,
        dag_fast_probability=dag_fast_prob,
        decision_input_tokens=decision_tokens,
    )


class HeuristicDecisionOneClient:
    """Local/offline Decision-1 evaluator implementing `/v1/systemone` semantics."""

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    async def evaluate(
        self,
        payload: Mapping[str, object],
    ) -> DecisionEvaluation:
        recorded = dict(payload)
        self.requests.append(recorded)
        state = recorded.get("state")
        if not isinstance(state, Mapping):
            state = {}
        input_tokens = max(1, len(canonical_json_bytes(recorded)) // 4)
        step_index = state.get("step_index", 0)
        completed_tools = state.get("completed_tools")
        completed_count = len(completed_tools) if isinstance(completed_tools, list | tuple) else 0
        available_tools = state.get("available_tools")
        tool_count = len(available_tools) if isinstance(available_tools, list | tuple) else 0
        last_status = str(state.get("last_tool_status", "none"))
        user_goal = str(state.get("user_goal", "")).lower()
        explicit_dag_hint = any(
            token in user_goal
            for token in ("all ", "parallel", "fan-out", "fan out", "pipeline", "triage", "every ", "both ")
        )

        if last_status not in {"none", "succeeded"}:
            return DecisionEvaluation(
                dag_viable=0.05,
                step_tier=ModelTier.FRONTIER,
                step_fast_probability=0.10,
                dag_tier=ModelTier.FRONTIER,
                dag_fast_probability=0.10,
                decision_input_tokens=input_tokens,
            )

        if step_index == 0 and tool_count >= 2 and explicit_dag_hint:
            needs_chained_templates = any(
                token in user_goal for token in ("then ", "correlate", "depend", "chain")
            )
            dag_tier = ModelTier.FRONTIER if needs_chained_templates else ModelTier.FAST
            dag_fast_prob = 0.20 if needs_chained_templates else 0.85
            return DecisionEvaluation(
                dag_viable=0.88,
                step_tier=ModelTier.FRONTIER,
                step_fast_probability=0.30,
                dag_tier=dag_tier,
                dag_fast_probability=dag_fast_prob,
                decision_input_tokens=input_tokens,
            )

        if completed_count > 0 and last_status == "succeeded":
            complex_synthesis = any(
                token in user_goal for token in ("conflict", "contradict", "root cause", "prove")
            ) and step_index == 1 and not explicit_dag_hint
            if not complex_synthesis:
                return DecisionEvaluation(
                    dag_viable=0.10,
                    step_tier=ModelTier.FAST,
                    step_fast_probability=0.90,
                    dag_tier=ModelTier.FAST,
                    dag_fast_probability=0.80,
                    decision_input_tokens=input_tokens,
                )

        if tool_count <= 1 and len(user_goal) < 240:
            return DecisionEvaluation(
                dag_viable=0.05,
                step_tier=ModelTier.FAST,
                step_fast_probability=0.85,
                dag_tier=ModelTier.FAST,
                dag_fast_probability=0.80,
                decision_input_tokens=input_tokens,
            )

        return DecisionEvaluation(
            dag_viable=0.15,
            step_tier=ModelTier.FRONTIER,
            step_fast_probability=0.25,
            dag_tier=ModelTier.FRONTIER,
            dag_fast_probability=0.20,
            decision_input_tokens=input_tokens,
        )


class HttpDecisionOneClient:
    """HTTP client for `/providers/microsoft/v1/systemone` with heuristic fallback."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        headers_provider: Callable[[], Any] | None = None,
        timeout_seconds: float = 10.0,
        fallback: DecisionOneClient | None = None,
    ) -> None:
        self._endpoint_url = endpoint_url
        self._headers_provider = headers_provider
        self._timeout_seconds = timeout_seconds
        self._fallback = fallback or HeuristicDecisionOneClient()

    async def evaluate(
        self,
        payload: Mapping[str, object],
    ) -> DecisionEvaluation:
        if not self._endpoint_url:
            return replace(
                await self._fallback.evaluate(payload),
                fallback_reason="endpoint_unconfigured",
            )
        import aiohttp

        started_at = time.perf_counter()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._headers_provider is not None:
            extra = self._headers_provider()
            if asyncio.iscoroutine(extra):
                extra = await extra
            if isinstance(extra, Mapping):
                headers.update({str(k): str(v) for k, v in extra.items()})
        fallback_tokens = max(1, len(canonical_json_bytes(payload)) // 4)
        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        try:
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.post(
                    self._endpoint_url,
                    headers=headers,
                    data=canonical_json_bytes(payload),
                ) as response,
            ):
                if response.status != 200:
                    return replace(
                        await self._fallback.evaluate(payload),
                        fallback_reason="http_non_200",
                        http_status=response.status,
                        duration_ms=max(
                            0.0,
                            (time.perf_counter() - started_at) * 1000.0,
                        ),
                    )
                body = json.loads(await response.read())
                if not isinstance(body, Mapping):
                    return replace(
                        await self._fallback.evaluate(payload),
                        fallback_reason="invalid_response",
                        http_status=response.status,
                        duration_ms=max(
                            0.0,
                            (time.perf_counter() - started_at) * 1000.0,
                        ),
                    )
                return replace(
                    parse_decision_one_response(
                        body,
                        fallback_input_tokens=fallback_tokens,
                    ),
                    source=RouteDecisionSource.LIVE,
                    http_status=response.status,
                    duration_ms=max(
                        0.0,
                        (time.perf_counter() - started_at) * 1000.0,
                    ),
                )
        except TimeoutError:
            fallback_reason = "timeout"
        except (aiohttp.ClientError, json.JSONDecodeError, ValueError):
            fallback_reason = "request_failed"
        except Exception:
            fallback_reason = "unexpected_failure"
        return replace(
            await self._fallback.evaluate(payload),
            fallback_reason=fallback_reason,
            duration_ms=max(
                0.0,
                (time.perf_counter() - started_at) * 1000.0,
            ),
        )


class DurableSegmentOptimizer(
    OneStepModelProvider,
    DurableChatStreamingModelProvider,
):
    """Wrap a one-step model provider with Decision-1 routing and DAG injection."""

    def __init__(
        self,
        inner: OneStepModelProvider,
        *,
        settings: DurableLoopSettings,
        decision_client: DecisionOneClient | None = None,
        ledger: PrefixCacheLedger | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._inner = inner
        self._settings = settings
        self._decision_client = decision_client or HeuristicDecisionOneClient()
        self._ledger = ledger or PrefixCacheLedger()
        self._clock = clock or (lambda: datetime.now(UTC))
        self.route_history: list[SegmentRouteDecision] = []

    @property
    def ledger(self) -> PrefixCacheLedger:
        """Return the prompt-cache ledger for inspection."""
        return self._ledger

    async def run_one_step(
        self,
        request: OneStepModelRequest,
    ) -> ModelDecisionEnvelopeV1:
        if not self._settings.decision_routing_enabled:
            return await self._inner.run_one_step(request)
        route = await self.route_request(request)
        routed_request = self.apply_route(request, route)
        decision = await self._inner.run_one_step(routed_request)
        return self.finalize_decision(request, routed_request, route, decision)

    async def run_one_step_with_observer(
        self,
        request: OneStepModelRequest,
        *,
        observer: DurableChatExecutionObserver,
        producers: Sequence[DurableChatModelProducerV1],
    ) -> ModelDecisionEnvelopeV1:
        if not self._settings.decision_routing_enabled:
            if isinstance(self._inner, DurableChatStreamingModelProvider):
                return await self._inner.run_one_step_with_observer(
                    request,
                    observer=observer,
                    producers=producers,
                )
            return await self._inner.run_one_step(request)
        route = await self.route_request(request)
        routed_request = self.apply_route(request, route)
        if isinstance(self._inner, DurableChatStreamingModelProvider):
            decision = await self._inner.run_one_step_with_observer(
                routed_request,
                observer=observer,
                producers=producers,
            )
        else:
            decision = await self._inner.run_one_step(routed_request)
        return self.finalize_decision(request, routed_request, route, decision)

    async def route_request(
        self,
        request: OneStepModelRequest,
    ) -> SegmentRouteDecision:
        """Select one immutable route without invoking the model provider."""
        now = self._clock()
        run_id = request.identity.run_id
        frontier_model = request.working_context.bundle.model
        fast_model = self._settings.fast_model or "gpt-4.1-mini"
        messages = request.working_context.bundle.messages

        cost_estimate = self._ledger.estimate_step_cost(
            run_id=run_id,
            messages=messages,
            catalog=request.catalog,
            frontier_model=frontier_model,
            fast_model=fast_model,
            now=now,
        )
        executable_tools = [
            tool.name
            for tool in request.catalog.tools
            if tool.provenance is not ToolProvenance.RUNTIME
            and tool.name != COMPILE_SEGMENT_DAG_TOOL_NAME
        ]
        completed_tools, last_status = _inspect_tool_history(messages)
        remaining_tool_budget = max(
            0,
            request.identity.budget.max_tool_calls - len(completed_tools),
        )
        dag_possible = (
            len(executable_tools) >= 2
            and remaining_tool_budget >= 2
            and not self._ledger.last_step_was_dag(run_id)
        )

        if last_status == "failed":
            route = SegmentRouteDecision(
                run_id=run_id,
                step_index=request.step_index,
                mode=SegmentMode.STEP,
                tier=ModelTier.FRONTIER,
                target_model=frontier_model,
                short_circuited=True,
                reason="last_tool_failed_fail_safe",
                cost_estimate=cost_estimate,
                recorded_at=now,
            )
        elif (
            not dag_possible
            and cost_estimate.frontier_cache_warm
            and cost_estimate.fast_cost_microunits >= cost_estimate.frontier_cost_microunits
        ):
            route = SegmentRouteDecision(
                run_id=run_id,
                step_index=request.step_index,
                mode=SegmentMode.STEP,
                tier=ModelTier.FRONTIER,
                target_model=frontier_model,
                short_circuited=True,
                reason="warm_frontier_cheaper_than_cold_fast",
                cost_estimate=cost_estimate,
                recorded_at=now,
            )
        else:
            state_projection = _build_decision_state(
                request=request,
                messages=messages,
                executable_tools=frozenset(executable_tools),
                completed_tools=completed_tools,
                last_tool_status=last_status,
                fast_model=fast_model,
                frontier_model=frontier_model,
                cost_estimate=cost_estimate,
            )
            payload = build_decision_one_payload(
                decision_model=self._settings.decision_model,
                state=state_projection,
            )
            evaluation = await self._decision_client.evaluate(payload)
            if dag_possible and evaluation.dag_viable >= _DEFAULT_DAG_THRESHOLD:
                mode = SegmentMode.DAG
                if (
                    evaluation.dag_tier is ModelTier.FAST
                    and evaluation.dag_fast_probability >= _DEFAULT_FAST_THRESHOLD
                    and cost_estimate.fast_cost_microunits < cost_estimate.frontier_cost_microunits
                ):
                    tier = ModelTier.FAST
                else:
                    tier = ModelTier.FRONTIER
                reason = f"decision_dag_viable:{evaluation.dag_viable:.2f}"
            else:
                mode = SegmentMode.STEP
                if (
                    evaluation.step_tier is ModelTier.FAST
                    and evaluation.step_fast_probability >= _DEFAULT_FAST_THRESHOLD
                    and cost_estimate.fast_cost_microunits < cost_estimate.frontier_cost_microunits
                ):
                    tier = ModelTier.FAST
                    reason = f"decision_fast_step:{evaluation.step_fast_probability:.2f}"
                else:
                    tier = ModelTier.FRONTIER
                    reason = "decision_frontier_step"
            target_model = fast_model if tier is ModelTier.FAST else frontier_model
            route = SegmentRouteDecision(
                run_id=run_id,
                step_index=request.step_index,
                mode=mode,
                tier=tier,
                target_model=target_model,
                short_circuited=False,
                reason=reason,
                cost_estimate=cost_estimate,
                evaluation=evaluation,
                source=evaluation.source,
                recorded_at=now,
            )

        self.route_history.append(route)
        return route

    def apply_route(
        self,
        request: OneStepModelRequest,
        route: SegmentRouteDecision,
    ) -> OneStepModelRequest:
        """Apply a previously selected route without evaluating routing again."""
        if route.run_id != request.identity.run_id or route.step_index != request.step_index:
            raise ValueError("segment route does not match the model request")
        run_id = request.identity.run_id
        messages = request.working_context.bundle.messages
        frontier_model = request.working_context.bundle.model
        previous_model = self._ledger.last_routed_model(run_id) or frontier_model
        sanitized_messages = (
            _strip_foreign_reasoning(messages)
            if route.target_model != previous_model
            else messages
        )
        rebased_bundle = MAFMessageBundleV1.create(
            messages=sanitized_messages,
            maf_core_version=request.working_context.bundle.maf_core_version,
            provider=request.working_context.bundle.provider,
            model=route.target_model,
            api_version=request.working_context.bundle.api_version,
        )
        rebased_context = request.working_context.model_copy(
            update={"bundle": rebased_bundle}
        )
        catalog = request.catalog
        instructions = request.instructions
        model_settings = {
            **request.model_settings,
            "_routed_model": route.target_model,
        }
        if route.mode is SegmentMode.DAG:
            if COMPILE_SEGMENT_DAG_TOOL_NAME not in instructions:
                instructions = f"{instructions}{_build_dag_planning_addendum(request.catalog)}"
            catalog = _dag_only_catalog(request.catalog)
            model_settings["_force_tool_name"] = COMPILE_SEGMENT_DAG_TOOL_NAME
        routed_request = OneStepModelRequest(
            identity=request.identity,
            step_index=request.step_index,
            instructions=instructions,
            working_context=rebased_context,
            catalog=catalog,
            model_settings=model_settings,
            fault_profile=request.fault_profile,
            effective_active_deadline=request.effective_active_deadline,
        )
        return routed_request

    def finalize_decision(
        self,
        original_request: OneStepModelRequest,
        routed_request: OneStepModelRequest,
        route: SegmentRouteDecision,
        decision: ModelDecisionEnvelopeV1,
    ) -> ModelDecisionEnvelopeV1:
        emitted_dag = any(
            call.name == COMPILE_SEGMENT_DAG_TOOL_NAME
            for call in decision.tool_calls
        )
        decision_tokens = (
            route.evaluation.decision_input_tokens
            if route.evaluation is not None
            else 0
        )
        cost_microunits = self._ledger.record_step(
            run_id=original_request.identity.run_id,
            model=route.target_model,
            tier=route.tier,
            messages=routed_request.working_context.bundle.messages,
            input_tokens=decision.usage.input_tokens,
            cached_tokens=0,
            output_tokens=decision.usage.output_tokens,
            decision_input_tokens=decision_tokens,
            emitted_dag=emitted_dag,
            now=route.recorded_at or self._clock(),
        )
        updated_usage = decision.usage.model_copy(
            update={"cost_microunits": cost_microunits}
        )
        return decision.model_copy(
            update={
                "deployment_hash": original_request.identity.deployment_hash,
                "usage": updated_usage,
            }
        )


def serialize_segment_route_decision(
    route: SegmentRouteDecision,
    *,
    request_hash: str,
) -> dict[str, object]:
    """Serialize one content-free route for Durable activity handoff."""
    evaluation = route.evaluation
    return {
        "request_hash": request_hash,
        "run_id": route.run_id,
        "step_index": route.step_index,
        "mode": route.mode.value,
        "tier": route.tier.value,
        "target_model": route.target_model,
        "short_circuited": route.short_circuited,
        "reason": route.reason,
        "source": route.source.value,
        "recorded_at": (
            route.recorded_at.isoformat()
            if route.recorded_at is not None
            else None
        ),
        "cost_estimate": {
            "estimated_input_tokens": route.cost_estimate.estimated_input_tokens,
            "frontier_warm_tokens": route.cost_estimate.frontier_warm_tokens,
            "frontier_cold_tokens": route.cost_estimate.frontier_cold_tokens,
            "frontier_cost_microunits": route.cost_estimate.frontier_cost_microunits,
            "fast_warm_tokens": route.cost_estimate.fast_warm_tokens,
            "fast_cold_tokens": route.cost_estimate.fast_cold_tokens,
            "fast_cost_microunits": route.cost_estimate.fast_cost_microunits,
            "frontier_cache_warm": route.cost_estimate.frontier_cache_warm,
        },
        "evaluation": (
            {
                "dag_viable": evaluation.dag_viable,
                "step_tier": evaluation.step_tier.value,
                "step_fast_probability": evaluation.step_fast_probability,
                "dag_tier": evaluation.dag_tier.value,
                "dag_fast_probability": evaluation.dag_fast_probability,
                "decision_input_tokens": evaluation.decision_input_tokens,
                "source": evaluation.source.value,
                "fallback_reason": evaluation.fallback_reason,
                "http_status": evaluation.http_status,
                "duration_ms": evaluation.duration_ms,
            }
            if evaluation is not None
            else None
        ),
    }


def deserialize_segment_route_decision(
    value: Mapping[str, object],
) -> tuple[str, SegmentRouteDecision]:
    """Parse one route emitted by the routing activity."""
    step_index = value["step_index"]
    if not isinstance(step_index, int) or isinstance(step_index, bool):
        raise ValueError("segment route step index is invalid")
    cost = value["cost_estimate"]
    if not isinstance(cost, Mapping):
        raise ValueError("segment route cost estimate is invalid")
    evaluation_value = value.get("evaluation")
    evaluation = None
    if evaluation_value is not None:
        if not isinstance(evaluation_value, Mapping):
            raise ValueError("segment route evaluation is invalid")
        evaluation = DecisionEvaluation(
            dag_viable=float(evaluation_value["dag_viable"]),
            step_tier=ModelTier(str(evaluation_value["step_tier"])),
            step_fast_probability=float(evaluation_value["step_fast_probability"]),
            dag_tier=ModelTier(str(evaluation_value["dag_tier"])),
            dag_fast_probability=float(evaluation_value["dag_fast_probability"]),
            decision_input_tokens=int(evaluation_value["decision_input_tokens"]),
            source=RouteDecisionSource(str(evaluation_value["source"])),
            fallback_reason=(
                str(evaluation_value["fallback_reason"])
                if evaluation_value.get("fallback_reason") is not None
                else None
            ),
            http_status=(
                int(evaluation_value["http_status"])
                if evaluation_value.get("http_status") is not None
                else None
            ),
            duration_ms=float(evaluation_value["duration_ms"]),
        )
    route = SegmentRouteDecision(
        run_id=str(value["run_id"]),
        step_index=step_index,
        mode=SegmentMode(str(value["mode"])),
        tier=ModelTier(str(value["tier"])),
        target_model=str(value["target_model"]),
        short_circuited=bool(value["short_circuited"]),
        reason=str(value["reason"]),
        source=RouteDecisionSource(str(value["source"])),
        cost_estimate=StepCostEstimate(
            estimated_input_tokens=int(cost["estimated_input_tokens"]),
            frontier_warm_tokens=int(cost["frontier_warm_tokens"]),
            frontier_cold_tokens=int(cost["frontier_cold_tokens"]),
            frontier_cost_microunits=int(cost["frontier_cost_microunits"]),
            fast_warm_tokens=int(cost["fast_warm_tokens"]),
            fast_cold_tokens=int(cost["fast_cold_tokens"]),
            fast_cost_microunits=int(cost["fast_cost_microunits"]),
            frontier_cache_warm=bool(cost["frontier_cache_warm"]),
        ),
        evaluation=evaluation,
        recorded_at=(
            datetime.fromisoformat(str(value["recorded_at"]))
            if value.get("recorded_at") is not None
            else None
        ),
    )
    return str(value["request_hash"]), route


class SegmentDagToolDispatcher(
    DurableToolDispatchPort,
    DurableToolCleanupPort,
    DurableRetainedSandboxInspectionPort,
):
    """Intercept `compile_segment_dag` and execute its waves over the underlying tool port."""

    def __init__(
        self,
        inner: DurableToolDispatchPort,
        *,
        catalog_provider: Callable[[], FrozenToolCatalogV1 | None] | None = None,
        content: DurableContentStore | None = None,
        max_parallel_reads: int = 4,
    ) -> None:
        self._inner = inner
        self._catalog_provider = catalog_provider
        self._content = content
        self._max_parallel_reads = max(1, max_parallel_reads)

    async def freeze_catalog(
        self,
        *,
        policy_hash: str,
        sandbox_profile: SandboxExecutionProfile,
    ) -> DurableToolCatalogSnapshot:
        """Preserve the wrapped router's admission catalog boundary."""
        if not isinstance(self._inner, DurableToolCatalogPort):
            raise ValueError("durable tool catalog provider is unavailable")
        return await self._inner.freeze_catalog(
            policy_hash=policy_hash,
            sandbox_profile=sandbox_profile,
        )

    async def dispatch(self, request: ToolRequestV1) -> ToolResultV1:
        if request.tool_name != COMPILE_SEGMENT_DAG_TOOL_NAME:
            return await self._inner.dispatch(request)
        return await self._execute_dag(request)

    async def cleanup(
        self,
        *,
        run_id: str,
        session_id: str,
        sandbox_profile: SandboxExecutionProfile,
        fault_profile: DurableFaultProfile,
    ) -> None:
        if isinstance(self._inner, DurableToolCleanupPort):
            await self._inner.cleanup(
                run_id=run_id,
                session_id=session_id,
                sandbox_profile=sandbox_profile,
                fault_profile=fault_profile,
            )

    async def inspect_retained_sandbox(
        self,
        *,
        run_id: str,
        session_id: str,
    ) -> DurableRetainedSandboxInspection | None:
        if isinstance(self._inner, DurableRetainedSandboxInspectionPort):
            return await self._inner.inspect_retained_sandbox(
                run_id=run_id,
                session_id=session_id,
            )
        return None

    async def _execute_dag(self, request: ToolRequestV1) -> ToolResultV1:
        catalog = (
            self._catalog_provider() if self._catalog_provider is not None else None
        )
        return await execute_dag_waves(
            request,
            dispatch_node=self._inner.dispatch,
            catalog=catalog,
            content=self._content,
            max_parallel_reads=self._max_parallel_reads,
        )


async def execute_dag_waves(
    request: ToolRequestV1,
    *,
    dispatch_node: Callable[[ToolRequestV1], Awaitable[ToolResultV1]],
    catalog: FrozenToolCatalogV1 | None = None,
    content: DurableContentStore | None = None,
    max_parallel_reads: int = 4,
    is_cancelled: Callable[[], Awaitable[bool]] | None = None,
) -> ToolResultV1:
    """Execute topological DAG waves over a per-node dispatch callable."""
    started_at = time.perf_counter()
    by_name: dict[str, FrozenToolDescriptorV1] = (
        catalog.by_name() if catalog is not None else {}
    )
    raw_tasks = _task_dicts(request.arguments)
    try:
        plan = validate_plan(
            {"version": 1, "tasks": raw_tasks},
            policy=WorkflowPlanPolicy(
                allowed_tools=_allowed_dag_tools(by_name, raw_tasks),
                allowed_subagents=frozenset(),
            ),
        )
    except PlanValidationError as exc:
        return _dag_failure_result(
            request,
            code="dag_plan_validation_failed",
            elapsed_ms=_elapsed_ms(started_at),
            detail=str(exc),
        )

    waves = _topological_waves(plan)
    node_results: dict[str, object] = {}
    executed_nodes: list[dict[str, object]] = []
    current_workspace_ref = request.workspace_ref
    ordinal_counter = 0

    for wave_index, wave in enumerate(waves):
        if is_cancelled is not None and await is_cancelled():
            return _dag_cancelled_result(
                request,
                elapsed_ms=_elapsed_ms(started_at),
            )
        resolved_wave, error_result = _resolve_wave_tasks(
            request=request,
            wave=wave,
            node_results=node_results,
            by_name=by_name,
            started_at=started_at,
        )
        if error_result is not None:
            return error_result
        ordinal_counter, current_workspace_ref, wave_error = (
            await _dispatch_wave_nodes(
                request=request,
                wave_index=wave_index,
                resolved_wave=resolved_wave,
                ordinal_counter=ordinal_counter,
                current_workspace_ref=current_workspace_ref,
                node_results=node_results,
                executed_nodes=executed_nodes,
                started_at=started_at,
                dispatch_node=dispatch_node,
                max_parallel_reads=max(1, max_parallel_reads),
            )
        )
        if wave_error is not None:
            return wave_error

    depended_on = {dep for task in plan.tasks for dep in task.depends_on}
    leaf_ids = [task.id for task in plan.tasks if task.id not in depended_on]
    intermediate_refs, _ = await _persist_intermediate_node_results(
        content=content,
        plan=plan,
        leaf_ids=leaf_ids,
        node_results=node_results,
    )
    collapsed_value: dict[str, object] = {
        "dag_status": "succeeded",
        "executed_nodes": executed_nodes,
        "intermediate_node_refs": intermediate_refs,
        "leaf_results": {node_id: node_results[node_id] for node_id in leaf_ids},
        "node_count": len(plan.tasks),
        "wave_count": len(waves),
    }
    return ToolResultV1(
        run_id=request.run_id,
        step_index=request.step_index,
        call_ordinal=request.call_ordinal,
        provider_call_id=request.provider_call_id,
        call_key=request.call_key,
        request_hash=request.request_hash,
        tool_name=request.tool_name,
        status=ToolResultStatus.SUCCEEDED,
        value=collapsed_value,
        elapsed_ms=_elapsed_ms(started_at),
        workspace_ref=current_workspace_ref,
    )


@dataclass(frozen=True, slots=True)
class _ReconstructedWaveState:
    node_results: dict[str, object]
    executed_nodes: list[dict[str, object]]
    current_workspace_ref: ContentRefV1 | None
    ordinal_counter: int
    total_elapsed_ms: float
    executed_tool_calls: int
    early_completion: dict[str, object] | None = None


async def execute_dag_wave_activity(
    *,
    content: DurableContentStore,
    document: DurableRunDocumentV1,
    parent_request: ToolRequestV1,
    wave_index: int,
    wave_result_refs: Sequence[object],
) -> dict[str, object]:
    """Plan the next DAG wave or finalize the collapsed parent tool result."""
    if (
        wave_index == 0
        and len(canonical_json_bytes(parent_request.arguments))
        > parent_request.argument_byte_limit
    ):
        failed = _dag_failure_result(
            parent_request,
            code="tool_arguments_too_large",
            elapsed_ms=0.0,
        )
        return await _complete_dag_wave_activity(
            content=content,
            parent_result=failed,
            executed_tool_calls=1,
        )

    by_name = document.plan.catalog.by_name()
    raw_tasks = _task_dicts(parent_request.arguments)
    try:
        plan = validate_plan(
            {"version": 1, "tasks": raw_tasks},
            policy=WorkflowPlanPolicy(
                allowed_tools=_allowed_dag_tools(by_name, raw_tasks),
                allowed_subagents=frozenset(),
            ),
        )
    except PlanValidationError as exc:
        failed = _dag_failure_result(
            parent_request,
            code="dag_plan_validation_failed",
            elapsed_ms=0.0,
            detail=str(exc),
        )
        return await _complete_dag_wave_activity(
            content=content,
            parent_result=failed,
            executed_tool_calls=1,
        )

    waves = _topological_waves(plan)
    state = await _reconstruct_completed_waves(
        content=content,
        parent_request=parent_request,
        waves=waves,
        wave_index=wave_index,
        wave_result_refs=wave_result_refs,
    )
    if state.early_completion is not None:
        return state.early_completion

    if wave_index < len(waves):
        return await _plan_next_dag_wave(
            content=content,
            document=document,
            parent_request=parent_request,
            wave=waves[wave_index],
            wave_index=wave_index,
            by_name=by_name,
            state=state,
        )

    return await _finalize_completed_dag_waves(
        content=content,
        parent_request=parent_request,
        plan=plan,
        waves=waves,
        state=state,
    )


async def _reconstruct_completed_waves(
    *,
    content: DurableContentStore,
    parent_request: ToolRequestV1,
    waves: Sequence[Sequence[WorkflowTask]],
    wave_index: int,
    wave_result_refs: Sequence[object],
) -> _ReconstructedWaveState:
    parsed_refs = _flatten_wave_result_refs(wave_result_refs)
    node_results: dict[str, object] = {}
    executed_nodes: list[dict[str, object]] = []
    current_workspace_ref = parent_request.workspace_ref
    ordinal_counter = 0
    total_elapsed_ms = 0.0
    executed_tool_calls = 0

    for completed_wave_index in range(min(wave_index, len(waves))):
        for task in waves[completed_wave_index]:
            ordinal_counter += 1
            if ordinal_counter > len(parsed_refs):
                failed = _dag_failure_result(
                    parent_request,
                    code="dag_missing_wave_result",
                    elapsed_ms=total_elapsed_ms,
                )
                early = await _complete_dag_wave_activity(
                    content=content,
                    parent_result=failed,
                    executed_tool_calls=max(1, executed_tool_calls),
                )
                return _ReconstructedWaveState(
                    node_results=node_results,
                    executed_nodes=executed_nodes,
                    current_workspace_ref=current_workspace_ref,
                    ordinal_counter=ordinal_counter,
                    total_elapsed_ms=total_elapsed_ms,
                    executed_tool_calls=executed_tool_calls,
                    early_completion=early,
                )
            ref_item = parsed_refs[ordinal_counter - 1]
            child_result = await get_protocol_model(
                content,
                ref_item.result_ref,
                ToolResultV1,
            )
            executed_tool_calls += 1
            total_elapsed_ms += child_result.elapsed_ms
            if child_result.workspace_ref is not None:
                current_workspace_ref = child_result.workspace_ref
            if child_result.status is not ToolResultStatus.SUCCEEDED:
                failed_parent = _dag_node_error_result(
                    parent_request,
                    child_result,
                    elapsed_ms=total_elapsed_ms,
                )
                if current_workspace_ref is not None:
                    failed_parent = failed_parent.model_copy(
                        update={"workspace_ref": current_workspace_ref}
                    )
                early = await _complete_dag_wave_activity(
                    content=content,
                    parent_result=failed_parent,
                    executed_tool_calls=executed_tool_calls,
                )
                return _ReconstructedWaveState(
                    node_results=node_results,
                    executed_nodes=executed_nodes,
                    current_workspace_ref=current_workspace_ref,
                    ordinal_counter=ordinal_counter,
                    total_elapsed_ms=total_elapsed_ms,
                    executed_tool_calls=executed_tool_calls,
                    early_completion=early,
                )
            node_results[task.id] = child_result.value
            executed_nodes.append(
                {
                    "id": task.id,
                    "tool": task.tool,
                    "wave": completed_wave_index,
                }
            )
    return _ReconstructedWaveState(
        node_results=node_results,
        executed_nodes=executed_nodes,
        current_workspace_ref=current_workspace_ref,
        ordinal_counter=ordinal_counter,
        total_elapsed_ms=total_elapsed_ms,
        executed_tool_calls=executed_tool_calls,
    )


async def _plan_next_dag_wave(
    *,
    content: DurableContentStore,
    document: DurableRunDocumentV1,
    parent_request: ToolRequestV1,
    wave: Sequence[WorkflowTask],
    wave_index: int,
    by_name: Mapping[str, FrozenToolDescriptorV1],
    state: _ReconstructedWaveState,
) -> dict[str, object]:
    resolved_wave, error_result = _resolve_wave_tasks(
        request=parent_request,
        wave=wave,
        node_results=state.node_results,
        by_name=by_name,
        started_at=time.perf_counter(),
    )
    if error_result is not None:
        if state.current_workspace_ref is not None:
            error_result = error_result.model_copy(
                update={"workspace_ref": state.current_workspace_ref}
            )
        return await _complete_dag_wave_activity(
            content=content,
            parent_result=error_result,
            executed_tool_calls=max(1, state.executed_tool_calls),
        )
    tool_calls: list[dict[str, object]] = []
    written_bytes = 0
    ordinal_counter = state.ordinal_counter
    for task, resolved_args, descriptor in resolved_wave:
        ordinal_counter += 1
        child_req = _build_dag_child_request(
            parent=parent_request,
            task_id=task.id,
            ordinal=ordinal_counter,
            descriptor=descriptor,
            arguments=resolved_args,
            workspace_ref=state.current_workspace_ref,
        )
        if document.plan.ui is not None:
            child_req = child_req.model_copy(update={"chat_ui": True})
        req_ref = await put_protocol_model(
            content,
            kind="tool-request",
            model=child_req,
        )
        written_bytes += req_ref.byte_length
        dispatch_ref = ToolDispatchRefV1(
            request_ref=req_ref,
            call_ordinal=child_req.call_ordinal,
            call_key=child_req.call_key,
            request_hash=child_req.request_hash,
            tool_name=child_req.tool_name,
            provenance=child_req.provenance,
            behavior=child_req.behavior,
            parallel_safe=bool(
                descriptor.behavior is ToolBehavior.READ_ONLY
                and descriptor.parallel_safe
            ),
        )
        tool_calls.append(dispatch_ref.model_dump(mode="json"))
    return {
        "status": "wave_ready",
        "tool_calls": tool_calls,
        "wave_index": wave_index,
        "written_bytes": written_bytes,
    }


async def _finalize_completed_dag_waves(
    *,
    content: DurableContentStore,
    parent_request: ToolRequestV1,
    plan: WorkflowPlan,
    waves: Sequence[Sequence[WorkflowTask]],
    state: _ReconstructedWaveState,
) -> dict[str, object]:
    depended_on = {dep for task in plan.tasks for dep in task.depends_on}
    leaf_ids = [task.id for task in plan.tasks if task.id not in depended_on]
    intermediate_refs, extra_written = await _persist_intermediate_node_results(
        content=content,
        plan=plan,
        leaf_ids=leaf_ids,
        node_results=state.node_results,
    )
    collapsed_value: dict[str, object] = {
        "dag_status": "succeeded",
        "executed_nodes": state.executed_nodes,
        "intermediate_node_refs": intermediate_refs,
        "leaf_results": {node_id: state.node_results[node_id] for node_id in leaf_ids},
        "node_count": len(plan.tasks),
        "wave_count": len(waves),
    }
    parent_result = ToolResultV1(
        run_id=parent_request.run_id,
        step_index=parent_request.step_index,
        call_ordinal=parent_request.call_ordinal,
        provider_call_id=parent_request.provider_call_id,
        call_key=parent_request.call_key,
        request_hash=parent_request.request_hash,
        tool_name=parent_request.tool_name,
        status=ToolResultStatus.SUCCEEDED,
        value=collapsed_value,
        elapsed_ms=state.total_elapsed_ms,
        workspace_ref=state.current_workspace_ref,
    )
    if (
        len(canonical_json_bytes(parent_result.value))
        > parent_request.result_byte_limit
    ):
        parent_result = _dag_failure_result(
            parent_request,
            code="tool_result_too_large",
            elapsed_ms=state.total_elapsed_ms,
        )
    return await _complete_dag_wave_activity(
        content=content,
        parent_result=parent_result,
        executed_tool_calls=len(plan.tasks),
        extra_written_bytes=extra_written,
    )


async def _dispatch_wave_nodes(
    *,
    request: ToolRequestV1,
    wave_index: int,
    resolved_wave: Sequence[
        tuple[WorkflowTask, dict[str, object], FrozenToolDescriptorV1]
    ],
    ordinal_counter: int,
    current_workspace_ref: ContentRefV1 | None,
    node_results: dict[str, object],
    executed_nodes: list[dict[str, object]],
    started_at: float,
    dispatch_node: Callable[[ToolRequestV1], Awaitable[ToolResultV1]],
    max_parallel_reads: int,
) -> tuple[int, ContentRefV1 | None, ToolResultV1 | None]:
    all_parallel_safe = len(resolved_wave) > 1 and all(
        descriptor.behavior is ToolBehavior.READ_ONLY and descriptor.parallel_safe
        for _, _, descriptor in resolved_wave
    )
    if all_parallel_safe:
        child_requests: list[tuple[WorkflowTask, ToolRequestV1]] = []
        for task, resolved_args, descriptor in resolved_wave:
            ordinal_counter += 1
            child_requests.append(
                (
                    task,
                    _build_dag_child_request(
                        parent=request,
                        task_id=task.id,
                        ordinal=ordinal_counter,
                        descriptor=descriptor,
                        arguments=resolved_args,
                        workspace_ref=current_workspace_ref,
                    ),
                )
            )
        for start in range(0, len(child_requests), max_parallel_reads):
            chunk = child_requests[start : start + max_parallel_reads]
            batch_results = await asyncio.gather(
                *(dispatch_node(child_req) for _, child_req in chunk)
            )
            for (task, _), child_result in zip(chunk, batch_results, strict=True):
                if child_result.status is not ToolResultStatus.SUCCEEDED:
                    return (
                        ordinal_counter,
                        current_workspace_ref,
                        _dag_node_error_result(
                            request,
                            child_result,
                            elapsed_ms=_elapsed_ms(started_at),
                        ),
                    )
                node_results[task.id] = child_result.value
                executed_nodes.append(
                    {"id": task.id, "tool": task.tool, "wave": wave_index}
                )
                if child_result.workspace_ref is not None:
                    current_workspace_ref = child_result.workspace_ref
        return ordinal_counter, current_workspace_ref, None

    for task, resolved_args, descriptor in resolved_wave:
        ordinal_counter += 1
        child_req = _build_dag_child_request(
            parent=request,
            task_id=task.id,
            ordinal=ordinal_counter,
            descriptor=descriptor,
            arguments=resolved_args,
            workspace_ref=current_workspace_ref,
        )
        child_result = await dispatch_node(child_req)
        if child_result.status is not ToolResultStatus.SUCCEEDED:
            return (
                ordinal_counter,
                current_workspace_ref,
                _dag_node_error_result(
                    request,
                    child_result,
                    elapsed_ms=_elapsed_ms(started_at),
                ),
            )
        node_results[task.id] = child_result.value
        executed_nodes.append(
            {"id": task.id, "tool": task.tool, "wave": wave_index}
        )
        if child_result.workspace_ref is not None:
            current_workspace_ref = child_result.workspace_ref
    return ordinal_counter, current_workspace_ref, None


async def _persist_intermediate_node_results(
    *,
    content: DurableContentStore | None,
    plan: WorkflowPlan,
    leaf_ids: Sequence[str],
    node_results: Mapping[str, object],
) -> tuple[dict[str, str], int]:
    intermediate_refs: dict[str, str] = {}
    written_bytes = 0
    for task in plan.tasks:
        if task.id in leaf_ids:
            continue
        if content is not None:
            ref = await content.put_bytes(
                kind="dag-node-result",
                payload=canonical_json_bytes(node_results[task.id]),
                media_type="application/json",
                retention_class="run",
            )
            intermediate_refs[task.id] = ref.object_id
            written_bytes += ref.byte_length
        else:
            intermediate_refs[task.id] = canonical_hash(node_results[task.id])
    return intermediate_refs, written_bytes


async def _complete_dag_wave_activity(
    *,
    content: DurableContentStore,
    parent_result: ToolResultV1,
    executed_tool_calls: int,
    extra_written_bytes: int = 0,
) -> dict[str, object]:
    result_ref = await put_protocol_model(
        content,
        kind="tool-result",
        model=parent_result,
    )
    total_written = result_ref.byte_length + extra_written_bytes
    tool_result_ref = ToolResultRefV1(
        result_ref=result_ref,
        call_ordinal=parent_result.call_ordinal,
        call_key=parent_result.call_key,
        request_hash=parent_result.request_hash,
        tool_name=parent_result.tool_name,
        status=parent_result.status,
        written_bytes=total_written,
    )
    return {
        "executed_tool_calls": executed_tool_calls,
        "result_ref": tool_result_ref.model_dump(mode="json"),
        "status": "completed",
        "written_bytes": total_written,
    }


def _flatten_wave_result_refs(
    wave_result_refs: Sequence[object],
) -> list[ToolResultRefV1]:
    parsed: list[ToolResultRefV1] = []
    for entry in wave_result_refs:
        if isinstance(entry, list | tuple):
            for nested in entry:
                parsed.append(
                    ToolResultRefV1.model_validate_json(
                        canonical_json_bytes(nested)
                    )
                )
        else:
            parsed.append(
                ToolResultRefV1.model_validate_json(canonical_json_bytes(entry))
            )
    return parsed


def _compute_tier_cost_microunits(
    *,
    model: str,
    tier: ModelTier,
    warm_input_tokens: int,
    cold_input_tokens: int,
    output_tokens: int,
) -> int:
    rates = model_token_rates(model, tier=tier)
    raw = (
        warm_input_tokens * rates.cached_input_microunits
        + cold_input_tokens * rates.input_microunits
        + output_tokens * rates.output_microunits
    )
    return math.ceil(raw / 1_000_000)


def model_token_rates(model: str, *, tier: ModelTier) -> ModelTokenRates:
    """Return verified exact-model rates or the compatible assumed tier default."""
    verified = _VERIFIED_MODEL_RATES.get(model)
    if verified is not None:
        return verified
    if tier is ModelTier.FAST:
        return ModelTokenRates(
            input_microunits=_ASSUMED_FAST_INPUT_RATE_MICROUNITS,
            cached_input_microunits=_ASSUMED_FAST_CACHED_INPUT_RATE_MICROUNITS,
            output_microunits=_ASSUMED_FAST_OUTPUT_RATE_MICROUNITS,
            source=ModelRateSource.ASSUMED_TIER_DEFAULT,
        )
    return ModelTokenRates(
        input_microunits=_ASSUMED_FRONTIER_INPUT_RATE_MICROUNITS,
        cached_input_microunits=_ASSUMED_FRONTIER_CACHED_INPUT_RATE_MICROUNITS,
        output_microunits=_ASSUMED_FRONTIER_OUTPUT_RATE_MICROUNITS,
        source=ModelRateSource.ASSUMED_TIER_DEFAULT,
    )


def _parse_tier_choice(entry: object) -> tuple[ModelTier, float]:
    if not isinstance(entry, Mapping):
        return ModelTier.FRONTIER, 0.0
    selected = str(entry.get("choice", entry.get("selected", ModelTier.FRONTIER.value))).lower()
    probs = entry.get("probabilities")
    fast_prob = 0.0
    if isinstance(probs, Mapping):
        raw_fast = probs.get(ModelTier.FAST.value, 0.0)
        if isinstance(raw_fast, int | float) and not isinstance(raw_fast, bool):
            fast_prob = float(max(0.0, min(1.0, raw_fast)))
    elif selected == ModelTier.FAST.value:
        fast_prob = 1.0
    tier = ModelTier.FAST if selected == ModelTier.FAST.value else ModelTier.FRONTIER
    return tier, fast_prob


def _extract_user_goal(
    messages: Sequence[dict[str, object]],
) -> tuple[str, bool]:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        text = _message_text(message)
        if text:
            return _bounded_safe_text(
                text,
                maximum_chars=_DECISION_USER_GOAL_MAX_CHARS,
            )
    return "", False


def _build_decision_state(
    *,
    request: OneStepModelRequest,
    messages: Sequence[dict[str, object]],
    executable_tools: frozenset[str],
    completed_tools: Sequence[str],
    last_tool_status: str,
    fast_model: str,
    frontier_model: str,
    cost_estimate: StepCostEstimate,
) -> dict[str, object]:
    tool_catalog, omitted_tool_count, truncated_tool_description_count = (
        _project_tool_catalog(
            request.catalog,
            executable_tools=executable_tools,
        )
    )
    (
        recent_conversation,
        omitted_message_count,
        truncated_message_count,
    ) = _project_recent_conversation(messages)
    user_goal, user_goal_truncated = _extract_user_goal(messages)
    agent_instructions, agent_instructions_truncated = _bounded_safe_text(
        request.instructions,
        maximum_chars=_DECISION_AGENT_INSTRUCTIONS_MAX_CHARS,
    )
    bounded_completed_tools = list(completed_tools[-_DECISION_COMPLETED_TOOLS_MAX_COUNT:])
    return {
        "step_index": request.step_index,
        "user_goal": user_goal,
        "user_goal_truncated": user_goal_truncated,
        "agent_instructions": agent_instructions,
        "agent_instructions_truncated": agent_instructions_truncated,
        "recent_conversation": recent_conversation,
        "omitted_conversation_message_count": omitted_message_count,
        "truncated_conversation_message_count": truncated_message_count,
        "completed_tools": bounded_completed_tools,
        "omitted_completed_tool_count": max(
            0,
            len(completed_tools) - len(bounded_completed_tools),
        ),
        "last_tool_status": last_tool_status,
        "available_tools": tool_catalog,
        "omitted_available_tool_count": omitted_tool_count,
        "truncated_tool_description_count": truncated_tool_description_count,
        "configured_tiers": {
            ModelTier.FAST.value: {"model": fast_model},
            ModelTier.FRONTIER.value: {"model": frontier_model},
        },
        "estimated_input_tokens": cost_estimate.estimated_input_tokens,
        "frontier_cache_warm": cost_estimate.frontier_cache_warm,
    }


def _project_recent_conversation(
    messages: Sequence[dict[str, object]],
) -> tuple[list[dict[str, object]], int, int]:
    projected_reversed: list[dict[str, object]] = []
    eligible_count = 0
    truncated_count = 0
    remaining_chars = _DECISION_RECENT_CONVERSATION_MAX_CHARS
    for message in reversed(messages):
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        text = _message_text(message)
        if not text:
            continue
        eligible_count += 1
        if (
            len(projected_reversed) >= _DECISION_RECENT_CONVERSATION_MAX_MESSAGES
            or remaining_chars <= 0
        ):
            continue
        bounded, truncated = _bounded_safe_text(
            text,
            maximum_chars=min(_DECISION_RECENT_MESSAGE_MAX_CHARS, remaining_chars),
        )
        if not bounded:
            continue
        projected_reversed.append({"role": str(role), "text": bounded})
        truncated_count += int(truncated)
        remaining_chars -= len(bounded)
    projected = list(reversed(projected_reversed))
    return (
        projected,
        max(0, eligible_count - len(projected)),
        truncated_count,
    )


def _project_tool_catalog(
    catalog: FrozenToolCatalogV1,
    *,
    executable_tools: frozenset[str],
) -> tuple[list[dict[str, object]], int, int]:
    projected: list[dict[str, object]] = []
    eligible_count = 0
    truncated_description_count = 0
    remaining_chars = _DECISION_TOOL_CATALOG_MAX_CHARS
    for tool in catalog.tools:
        if tool.name not in executable_tools:
            continue
        eligible_count += 1
        if (
            len(projected) >= _DECISION_TOOL_CATALOG_MAX_TOOLS
            or remaining_chars <= 0
        ):
            continue
        description, description_truncated = _bounded_safe_text(
            tool.description,
            maximum_chars=min(_DECISION_TOOL_DESCRIPTION_MAX_CHARS, remaining_chars),
        )
        projected.append(
            {
                "name": tool.name,
                "description": description,
                "behavior": tool.behavior.value,
                "parallel_safe": tool.parallel_safe,
            }
        )
        truncated_description_count += int(description_truncated)
        remaining_chars -= len(description)
    return (
        projected,
        max(0, eligible_count - len(projected)),
        truncated_description_count,
    )


def _message_text(message: Mapping[str, object]) -> str:
    contents = message.get("contents")
    if not isinstance(contents, list | tuple):
        return ""
    text_parts = [
        text.strip()
        for item in contents
        if isinstance(item, Mapping) and item.get("type") == "text"
        if isinstance((text := item.get("text")), str) and text.strip()
    ]
    return "\n".join(text_parts)


def _bounded_safe_text(
    text: str,
    *,
    maximum_chars: int,
) -> tuple[str, bool]:
    redacted = _BEARER_TOKEN_PATTERN.sub("Bearer <redacted>", text.strip())
    redacted = _CREDENTIAL_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        redacted,
    )
    if len(redacted) <= maximum_chars:
        return redacted, False
    if maximum_chars <= len(_TRUNCATION_MARKER):
        return _TRUNCATION_MARKER[:maximum_chars], True
    retained_chars = maximum_chars - len(_TRUNCATION_MARKER)
    prefix_chars = (retained_chars + 1) // 2
    suffix_chars = retained_chars - prefix_chars
    suffix = redacted[-suffix_chars:] if suffix_chars else ""
    return (
        redacted[:prefix_chars]
        + _TRUNCATION_MARKER
        + suffix,
        True,
    )


def _inspect_tool_history(
    messages: Sequence[dict[str, object]],
) -> tuple[list[str], str]:
    completed: list[str] = []
    last_status = "none"
    for message in messages:
        if message.get("role") != "tool":
            continue
        contents = message.get("contents")
        if not isinstance(contents, list | tuple):
            continue
        for item in contents:
            if not isinstance(item, Mapping) or item.get("type") != "function_result":
                continue
            name = str(item.get("name") or "unknown")
            completed.append(name)
            result = item.get("result")
            if isinstance(result, Mapping) and result.get("status") in {
                "failed",
                "timed_out",
                "ambiguous",
            }:
                last_status = "failed"
            else:
                last_status = "succeeded"
    return completed, last_status


def _strip_foreign_reasoning(
    messages: Sequence[dict[str, object]],
) -> tuple[dict[str, object], ...]:
    cleaned: list[dict[str, object]] = []
    for message in messages:
        contents = message.get("contents")
        if not isinstance(contents, list | tuple):
            cleaned.append(dict(message))
            continue
        kept = [
            item
            for item in contents
            if not (isinstance(item, Mapping) and item.get("type") == "reasoning")
        ]
        if not kept and message.get("role") == "assistant":
            continue
        cleaned.append({**message, "contents": kept})
    return tuple(cleaned)


def _dag_only_catalog(catalog: FrozenToolCatalogV1) -> FrozenToolCatalogV1:
    return FrozenToolCatalogV1.create(
        tools=(compile_segment_dag_tool_descriptor(),),
        policy_hash=catalog.policy_hash,
        package_hash=catalog.package_hash,
    )


def _build_dag_planning_addendum(catalog: FrozenToolCatalogV1) -> str:
    tool_lines = [
        (
            f"- {tool.name}: {tool.description} | "
            f"parameters={canonical_json_bytes(tool.parameters).decode('utf-8')}"
        )
        for tool in catalog.tools
        if tool.provenance is not ToolProvenance.RUNTIME
        and tool.name != COMPILE_SEGMENT_DAG_TOOL_NAME
    ]
    if not tool_lines:
        return _DAG_PLANNING_ADDENDUM
    return (
        f"{_DAG_PLANNING_ADDENDUM}\n"
        "Available DAG node tools:\n"
        + "\n".join(tool_lines)
    )


def _allowed_dag_tools(
    by_name: Mapping[str, FrozenToolDescriptorV1],
    raw_tasks: Sequence[Mapping[str, object]],
) -> frozenset[str]:
    if by_name:
        return frozenset(
            name
            for name, desc in by_name.items()
            if desc.provenance is not ToolProvenance.RUNTIME
            and name != COMPILE_SEGMENT_DAG_TOOL_NAME
        )
    return frozenset(
        str(item.get("tool"))
        for item in raw_tasks
        if isinstance(item.get("tool"), str)
        and item.get("tool") not in {REQUEST_HUMAN_INPUT_TOOL_NAME, COMPILE_SEGMENT_DAG_TOOL_NAME}
    )


def _resolve_wave_tasks(
    *,
    request: ToolRequestV1,
    wave: Sequence[WorkflowTask],
    node_results: Mapping[str, object],
    by_name: Mapping[str, FrozenToolDescriptorV1],
    started_at: float,
) -> tuple[list[tuple[WorkflowTask, dict[str, object], FrozenToolDescriptorV1]], ToolResultV1 | None]:
    resolved_wave: list[tuple[WorkflowTask, dict[str, object], FrozenToolDescriptorV1]] = []
    for task in wave:
        assert task.tool is not None
        try:
            resolved_args = resolve_template_value(task.args, dict(node_results))
        except TemplateResolutionError as exc:
            return [], _dag_failure_result(
                request,
                code="dag_template_resolution_failed",
                elapsed_ms=_elapsed_ms(started_at),
                detail=str(exc),
            )
        if not isinstance(resolved_args, dict):
            return [], _dag_failure_result(
                request,
                code="dag_invalid_task_arguments",
                elapsed_ms=_elapsed_ms(started_at),
            )
        descriptor = by_name.get(task.tool) or _default_dag_tool_descriptor(task.tool)
        resolved_wave.append((task, resolved_args, descriptor))
    return resolved_wave, None


def _task_dicts(arguments: Mapping[str, object]) -> list[dict[str, object]]:
    raw = arguments.get("tasks")
    if not isinstance(raw, list):
        return []
    result: list[dict[str, object]] = []
    for item in raw:
        if isinstance(item, Mapping):
            entry = dict(item)
            entry.setdefault("type", "tool")
            result.append(entry)
    return result


def _topological_waves(plan: WorkflowPlan) -> list[list[WorkflowTask]]:
    remaining = list(plan.tasks)
    completed: set[str] = set()
    waves: list[list[WorkflowTask]] = []
    while remaining:
        ready = [
            task
            for task in remaining
            if all(dep in completed for dep in task.depends_on)
        ]
        if not ready:
            raise RuntimeError("validated DAG made no topological progress")
        waves.append(ready)
        ready_ids = {task.id for task in ready}
        completed.update(ready_ids)
        remaining = [task for task in remaining if task.id not in ready_ids]
    return waves


def _default_dag_tool_descriptor(name: str) -> FrozenToolDescriptorV1:
    return FrozenToolDescriptorV1(
        name=name,
        description=f"DAG node tool {name}",
        parameters={"additionalProperties": True, "type": "object"},
        provenance=ToolProvenance.LOCAL,
        behavior=ToolBehavior.READ_ONLY,
        parallel_safe=True,
    )


def _build_dag_child_request(
    *,
    parent: ToolRequestV1,
    task_id: str,
    ordinal: int,
    descriptor: FrozenToolDescriptorV1,
    arguments: dict[str, object],
    workspace_ref: ContentRefV1 | None,
) -> ToolRequestV1:
    req_hash = tool_request_hash(
        tool_name=descriptor.name,
        arguments=arguments,
        behavior=descriptor.behavior,
        provenance=descriptor.provenance,
        argument_byte_limit=parent.argument_byte_limit,
        result_byte_limit=parent.result_byte_limit,
        policy_hash=parent.policy_hash,
        catalog_hash=parent.catalog_hash,
        package_hash=parent.package_hash,
        workspace_ref=workspace_ref,
        sandbox_profile=parent.sandbox_profile,
        fault_profile=parent.fault_profile,
        owner_hash=parent.owner_hash,
    )
    call_key = deterministic_call_key(
        run_id=parent.run_id,
        step_index=parent.step_index,
        call_ordinal=ordinal,
        decision_hash=canonical_hash({"parent_call_key": parent.call_key, "task_id": task_id}),
        tool_name=descriptor.name,
    )
    return ToolRequestV1(
        run_id=parent.run_id,
        session_id=parent.session_id,
        owner_hash=parent.owner_hash,
        step_index=parent.step_index,
        call_ordinal=ordinal,
        provider_call_id=f"{parent.provider_call_id}-{task_id}",
        call_key=call_key,
        tool_name=descriptor.name,
        provenance=descriptor.provenance,
        behavior=descriptor.behavior,
        arguments=arguments,
        argument_byte_limit=parent.argument_byte_limit,
        result_byte_limit=parent.result_byte_limit,
        request_hash=req_hash,
        policy_hash=parent.policy_hash,
        catalog_hash=parent.catalog_hash,
        package_hash=parent.package_hash,
        workspace_ref=workspace_ref,
        sandbox_profile=parent.sandbox_profile,
        fault_profile=parent.fault_profile,
        chat_ui=parent.chat_ui,
        deadline=parent.deadline,
    )


def _dag_failure_result(
    request: ToolRequestV1,
    *,
    code: str,
    elapsed_ms: float,
    detail: str | None = None,
) -> ToolResultV1:
    return ToolResultV1(
        run_id=request.run_id,
        step_index=request.step_index,
        call_ordinal=request.call_ordinal,
        provider_call_id=request.provider_call_id,
        call_key=request.call_key,
        request_hash=request.request_hash,
        tool_name=request.tool_name,
        status=ToolResultStatus.FAILED,
        value={"detail": detail} if detail else None,
        elapsed_ms=elapsed_ms,
        error=ErrorEnvelopeV1(
            code=code,
            classification="protocol",
            retryable=False,
            phase="tool_step",
            step_index=request.step_index,
            call_key=request.call_key,
        ),
    )


def _dag_node_error_result(
    request: ToolRequestV1,
    child_result: ToolResultV1,
    *,
    elapsed_ms: float,
) -> ToolResultV1:
    return ToolResultV1(
        run_id=request.run_id,
        step_index=request.step_index,
        call_ordinal=request.call_ordinal,
        provider_call_id=request.provider_call_id,
        call_key=request.call_key,
        request_hash=request.request_hash,
        tool_name=request.tool_name,
        status=child_result.status,
        elapsed_ms=elapsed_ms,
        error=child_result.error
        or ErrorEnvelopeV1(
            code="dag_node_execution_failed",
            classification="tool",
            retryable=False,
            phase="tool_step",
            step_index=request.step_index,
            call_key=request.call_key,
        ),
    )


def _dag_cancelled_result(
    request: ToolRequestV1,
    *,
    elapsed_ms: float,
) -> ToolResultV1:
    return ToolResultV1(
        run_id=request.run_id,
        step_index=request.step_index,
        call_ordinal=request.call_ordinal,
        provider_call_id=request.provider_call_id,
        call_key=request.call_key,
        request_hash=request.request_hash,
        tool_name=request.tool_name,
        status=ToolResultStatus.CANCELLED,
        elapsed_ms=elapsed_ms,
        error=ErrorEnvelopeV1(
            code="tool_cancelled",
            classification="cancellation",
            retryable=False,
            phase="tool_step",
            step_index=request.step_index,
            call_key=request.call_key,
        ),
    )


def _elapsed_ms(started_at: float) -> float:
    return max(0.0, (time.perf_counter() - started_at) * 1000.0)
