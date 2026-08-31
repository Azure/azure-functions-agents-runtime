"""Workflow plan schema (M1 step 3b — DAG + templating + wait tasks).

Scope at 3b: arbitrary-DAG plans whose nodes are ``tool`` or ``wait``
tasks, with cycle detection, ``depends_on`` validation, result-templating
refs validated against the upstream closure, and ISO-8601 ``duration`` /
``until`` parsing for ``wait`` tasks. Cooperative cancel is implemented in
the engine; the validator only ensures wait specifications are well-
formed.

Templating syntax:
    ``${node_id.result}``                  — entire prior result
    ``${node_id.result.path.to.field}``    — dotted path traversal

References may appear anywhere in a string ``args`` value. A full-string
match preserves the referenced value's native type (dict/list/number);
embedded matches inside a larger string are stringified (JSON for non-
strings, raw for strings). Path traversal happens at orchestrator-run
time against JSON-normalized prior outputs; an unresolved path is a
deterministic runtime failure (see :func:`resolve_template_value`).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Literal, NotRequired, TypedDict, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


class PlanValidationError(ValueError):
    """Raised when a plan fails structural or semantic validation.

    Message is intended to be surfaced to the LLM caller so it can
    self-correct and resubmit.
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str | None = None,
        node_id: str | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.node_id = node_id
        self.path = path


class TemplateResolutionError(ValueError):
    """Raised at orchestration time when a template path cannot be resolved.

    ``error_code`` lets the orchestrator map a runtime resolution failure to
    one of the stable controlled failure codes without inspecting the message
    text. Unresolved references default to ``workflow_reference_unresolved``;
    :func:`evaluate_condition` raises ``workflow_condition_invalid`` when a
    predicate resolves to a non-scalar value.
    """

    def __init__(
        self, message: str, *, error_code: str = "workflow_reference_unresolved"
    ) -> None:
        super().__init__(message)
        self.error_code = error_code


_PUBLIC_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_WHITESPACE_RE = re.compile(r"\s+")
_DEFAULT_EXECUTION_ERROR_MESSAGE = "Task execution failed."


def _normalize_execution_error_message(message: str) -> str:
    normalized = _CONTROL_CHARACTER_RE.sub(" ", str(message))
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    if not normalized:
        return _DEFAULT_EXECUTION_ERROR_MESSAGE
    return normalized[:256]


class _WorkflowExecutionError(Exception):
    """Base for application-classified, sanitized workflow task failures."""

    error_code: str
    message: str

    def __init__(self, error_code: str, message: str) -> None:
        if (
            not isinstance(error_code, str)
            or _PUBLIC_ERROR_CODE_RE.fullmatch(error_code) is None
            or error_code.startswith("workflow_")
        ):
            raise ValueError(
                "error_code must match ^[a-z][a-z0-9_]{0,63}$ and must not "
                "start with 'workflow_'"
            )
        self.error_code = error_code
        self.message = _normalize_execution_error_message(message)
        super().__init__(self.message)


class WorkflowRetryableError(_WorkflowExecutionError):
    """Signal an application failure that may be retried by task policy."""


class WorkflowTerminalError(_WorkflowExecutionError):
    """Signal a non-retryable application failure that may be continued."""


TOOL_TASK_TYPE: Literal["tool"] = "tool"
WAIT_TASK_TYPE: Literal["wait"] = "wait"
SUB_AGENT_TASK_TYPE: Literal["sub_agent"] = "sub_agent"
SUPPORTED_TASK_TYPES: frozenset[str] = frozenset({
    TOOL_TASK_TYPE,
    WAIT_TASK_TYPE,
    SUB_AGENT_TASK_TYPE,
})


@dataclass(frozen=True)
class WorkflowToolExecutionPolicy:
    """Immutable decorator declarations used during start-time resolution."""

    timeout: str | None = None
    retry: WorkflowRetryPolicy | None = None


def _empty_tool_execution_policies() -> Mapping[str, WorkflowToolExecutionPolicy]:
    return MappingProxyType({})


def _empty_subagent_timeouts() -> Mapping[str, int]:
    return MappingProxyType({})


@dataclass(frozen=True)
class WorkflowPlanPolicy:
    """Immutable per-agent authorization boundary for workflow plans."""

    allowed_tools: frozenset[str]
    allowed_subagents: frozenset[str] = frozenset()
    subagent_guidance: tuple[tuple[str, str], ...] = ()
    tool_execution: Mapping[str, WorkflowToolExecutionPolicy] = field(
        default_factory=_empty_tool_execution_policies,
    )
    subagent_timeout_ms: Mapping[str, int] = field(
        default_factory=_empty_subagent_timeouts,
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "tool_execution",
            MappingProxyType(dict(self.tool_execution)),
        )
        object.__setattr__(
            self,
            "subagent_timeout_ms",
            MappingProxyType(dict(self.subagent_timeout_ms)),
        )


type JsonScalar = str | int | float | bool | None

MAX_POLICY_ATTEMPTS = 5
MIN_POLICY_TIMEOUT_MS = 1_000
MAX_POLICY_TIMEOUT_MS = 10 * 60 * 1_000
MAX_INITIAL_BACKOFF_MS = 5 * 60 * 1_000
MAX_BACKOFF_MS = 15 * 60 * 1_000
MAX_POLICY_ELAPSED_MS = 60 * 60 * 1_000


def _policy_duration_ms(value: str, *, field_name: str) -> int:
    """Parse an authored policy duration without floating-point conversion."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty ISO-8601 duration")
    if len(value) > 64:
        raise ValueError(f"{field_name} is too large")
    match = _DURATION_RE.match(value)
    if match is None:
        raise ValueError(
            f"{field_name} must be an ISO-8601 duration like 'PT30S', 'PT5M', or 'PT1H30M'"
        )
    if all(component is None for component in match.groupdict().values()):
        raise ValueError(f"{field_name} duration has no components")
    if "T" in value and all(
        match.group(name) is None for name in ("hours", "minutes", "seconds")
    ):
        raise ValueError(f"{field_name} duration time section has no components")
    try:
        total_ms = (
            Decimal(match.group("days") or 0) * Decimal(86_400_000)
            + Decimal(match.group("hours") or 0) * Decimal(3_600_000)
            + Decimal(match.group("minutes") or 0) * Decimal(60_000)
            + Decimal(match.group("seconds") or 0) * Decimal(1_000)
        )
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} is not a finite duration") from exc
    integral_ms = total_ms.to_integral_value()
    if total_ms != integral_ms:
        raise ValueError(f"{field_name} must resolve to whole milliseconds")
    try:
        return int(integral_ms)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field_name} is too large") from exc


class WorkflowRetryBackoff(BaseModel):
    """Bounded deterministic retry backoff authored on a task or workflow tool."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    initial: str
    multiplier: float
    max: str

    @model_validator(mode="after")
    def validate_bounds(self) -> WorkflowRetryBackoff:
        initial_ms = _policy_duration_ms(self.initial, field_name="backoff.initial")
        maximum_ms = _policy_duration_ms(self.max, field_name="backoff.max")
        if initial_ms < 0 or initial_ms > MAX_INITIAL_BACKOFF_MS:
            raise ValueError("backoff.initial must be between PT0S and PT5M")
        if maximum_ms < 0 or maximum_ms > MAX_BACKOFF_MS:
            raise ValueError("backoff.max must be between PT0S and PT15M")
        if maximum_ms < initial_ms:
            raise ValueError("backoff.max must not be less than backoff.initial")
        if not math.isfinite(self.multiplier) or not 1.0 <= self.multiplier <= 10.0:
            raise ValueError("backoff.multiplier must be a finite number from 1.0 through 10.0")
        return self


class WorkflowRetryPolicy(BaseModel):
    """Bounded retry declaration; ``max_attempts`` includes the first attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    max_attempts: int = Field(default=1, ge=1, le=MAX_POLICY_ATTEMPTS)
    backoff: WorkflowRetryBackoff | None = None

    @model_validator(mode="after")
    def validate_backoff_presence(self) -> WorkflowRetryPolicy:
        if self.max_attempts > 1 and self.backoff is None:
            raise ValueError("backoff is required when max_attempts is greater than 1")
        return self


class WorkflowTaskExecution(BaseModel):
    """Authored bounded execution policy for a tool or Sub Agent task."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    timeout: str | None = None
    retry: WorkflowRetryPolicy | None = None
    continue_on_error: bool = False

    @field_validator("timeout")
    @classmethod
    def validate_timeout(cls, value: str | None) -> str | None:
        if value is None:
            return value
        timeout_ms = _policy_duration_ms(value, field_name="execution.timeout")
        if not MIN_POLICY_TIMEOUT_MS <= timeout_ms <= MAX_POLICY_TIMEOUT_MS:
            raise ValueError("execution.timeout must be between PT1S and PT10M")
        return value


class DurableRetryPolicyInput(TypedDict):
    """JSON-safe Durable Python 2.x retry policy persisted for new histories."""

    first_retry_interval_ms: int
    max_number_of_attempts: int
    backoff_coefficient: float
    max_retry_interval_ms: int
    retry_timeout_ms: int


class EffectiveWorkflowTaskExecution(TypedDict):
    """JSON-safe policy frozen before Durable orchestration starts."""

    timeout_ms: int
    max_attempts: int
    retry_delays_ms: list[int]
    continue_on_error: bool
    timeout_source: Literal["decorator", "task", "runtime_default"]
    retry_source: Literal["decorator", "task", "runtime_default"]
    durable_retry_policy: NotRequired[DurableRetryPolicyInput]


def precompute_retry_delays_ms(retry: WorkflowRetryPolicy) -> list[int]:
    """Return the replay-safe retry delays using Decimal ``ROUND_FLOOR``."""
    if retry.backoff is None:
        return []
    initial_ms = _policy_duration_ms(retry.backoff.initial, field_name="backoff.initial")
    maximum_ms = _policy_duration_ms(retry.backoff.max, field_name="backoff.max")
    multiplier = Decimal(str(retry.backoff.multiplier))
    return [
        min(
            maximum_ms,
            int(
                (Decimal(initial_ms) * multiplier**index).to_integral_value(
                    rounding=ROUND_FLOOR
                )
            ),
        )
        for index in range(retry.max_attempts - 1)
    ]


def durable_retry_policy_input(retry: WorkflowRetryPolicy) -> DurableRetryPolicyInput:
    """Preserve the authored exponential shape for Durable native retry."""
    if retry.backoff is None:
        initial_ms = maximum_ms = 0
        coefficient = 1.0
    else:
        initial_ms = _policy_duration_ms(
            retry.backoff.initial,
            field_name="backoff.initial",
        )
        maximum_ms = _policy_duration_ms(
            retry.backoff.max,
            field_name="backoff.max",
        )
        coefficient = retry.backoff.multiplier
    return {
        "first_retry_interval_ms": initial_ms,
        "max_number_of_attempts": retry.max_attempts,
        "backoff_coefficient": coefficient,
        "max_retry_interval_ms": maximum_ms,
        "retry_timeout_ms": MAX_POLICY_ELAPSED_MS,
    }


def _native_retry_delays_ceiling_ms(retry: WorkflowRetryPolicy) -> list[int]:
    """Conservatively bound Durable's floating-point exponential delays."""
    if retry.backoff is None:
        return []
    initial_ms = _policy_duration_ms(retry.backoff.initial, field_name="backoff.initial")
    maximum_ms = _policy_duration_ms(retry.backoff.max, field_name="backoff.max")
    multiplier = Decimal(str(retry.backoff.multiplier))
    return [
        min(
            maximum_ms,
            int(
                (Decimal(initial_ms) * multiplier**index).to_integral_value(
                    rounding=ROUND_CEILING
                )
            ),
        )
        for index in range(retry.max_attempts - 1)
    ]


class WorkflowConditionInput(TypedDict):
    ref: str
    operator: Literal["equals", "not_equals"]
    value: JsonScalar


class _WorkflowTaskInputBase(TypedDict):
    id: str
    depends_on: list[str]


class _DynamicWorkflowTaskInput(TypedDict, total=False):
    when: WorkflowConditionInput
    for_each: str


class ToolWorkflowTaskInput(_WorkflowTaskInputBase, _DynamicWorkflowTaskInput):
    type: Literal["tool"]
    tool: str
    args: dict[str, Any]
    execution: NotRequired[EffectiveWorkflowTaskExecution]


class WaitWorkflowTaskInput(_WorkflowTaskInputBase, _DynamicWorkflowTaskInput):
    type: Literal["wait"]
    duration: NotRequired[str]
    until: NotRequired[str]


class SubAgentWorkflowTaskInput(_WorkflowTaskInputBase, _DynamicWorkflowTaskInput):
    type: Literal["sub_agent"]
    agent: str
    task: str
    execution: NotRequired[EffectiveWorkflowTaskExecution]


type WorkflowTaskInput = (
    ToolWorkflowTaskInput | WaitWorkflowTaskInput | SubAgentWorkflowTaskInput
)


class WorkflowPolicyInput(TypedDict):
    allowed_tools: list[str]
    allowed_subagents: list[str]


class WorkflowPayload(TypedDict):
    tasks: list[WorkflowTaskInput]
    workflow_agent_slug: str
    policy: NotRequired[WorkflowPolicyInput]
    workflow_agent: NotRequired[dict[str, str]]


class PlanValidationMetadata(TypedDict, total=False):
    error_code: str
    node_id: str
    path: str


def _is_json_scalar(value: object) -> bool:
    """Return whether ``value`` is a JSON scalar with a finite numeric value."""
    return value is None or type(value) in (str, int, bool) or (
        type(value) is float and math.isfinite(value)
    )


class WorkflowCondition(BaseModel):
    """A deliberately small, replay-safe predicate for a workflow task."""

    model_config = ConfigDict(extra="forbid")

    ref: str
    operator: Literal["equals", "not_equals"]
    value: JsonScalar

    @field_validator("value", mode="before")
    @classmethod
    def validate_scalar_value(cls, value: object) -> object:
        if not _is_json_scalar(value):
            raise ValueError("condition value must be a JSON scalar")
        return value


class WorkflowTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    type: str = Field(default=TOOL_TASK_TYPE)
    # ``tool`` is required for type=tool, must be omitted for type=wait.
    tool: str | None = Field(default=None)
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    # Wait-task fields. Exactly one of ``duration`` (ISO-8601 like
    # ``"PT30S"``) or ``until`` (ISO-8601 datetime) must be set when
    # ``type == "wait"``; both must be omitted otherwise.
    duration: str | None = Field(default=None)
    until: str | None = Field(default=None)
    agent: str | None = Field(default=None)
    task: str | None = Field(default=None)
    when: WorkflowCondition | None = Field(default=None, exclude_if=lambda value: value is None)
    for_each: str | None = Field(default=None, exclude_if=lambda value: value is None)
    execution: WorkflowTaskExecution | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


def resolve_workflow_task_execution(
    task: WorkflowTask,
    *,
    decorator_timeout: str | None = None,
    decorator_retry: WorkflowRetryPolicy | None = None,
    subagent_timeout_ms: int | None = None,
) -> EffectiveWorkflowTaskExecution | None:
    """Resolve authoritative metadata and task policy at submission time.

    Callers pass decorator declarations for tool tasks and the resolved agent
    timeout for Sub Agent tasks. ``None`` means the task is policy-free and no
    execution payload may be persisted.
    """
    authored = "execution" in task.model_fields_set
    policy_aware = authored or decorator_timeout is not None or decorator_retry is not None
    if not policy_aware:
        return None
    execution = task.execution or WorkflowTaskExecution()
    if task.type != TOOL_TASK_TYPE and (
        decorator_timeout is not None or decorator_retry is not None
    ):
        raise ValueError("workflow tool decorator policy is only valid on type=tool tasks")
    if task.type == WAIT_TASK_TYPE:
        raise ValueError("execution is not valid on type=wait tasks")

    timeout_source: Literal["decorator", "task", "runtime_default"]
    if decorator_timeout is not None:
        timeout_text = WorkflowTaskExecution(timeout=decorator_timeout).timeout
        timeout_source = "decorator"
    elif execution.timeout is not None:
        timeout_text = execution.timeout
        timeout_source = "task"
    else:
        timeout_text = None
        timeout_source = "runtime_default"

    if task.type == SUB_AGENT_TASK_TYPE:
        if subagent_timeout_ms is None or subagent_timeout_ms <= 0:
            raise ValueError("a positive resolved Sub Agent timeout is required")
        if subagent_timeout_ms < MIN_POLICY_TIMEOUT_MS:
            raise ValueError("resolved Sub Agent timeout must be at least PT1S")
        bounded_subagent_ms = min(subagent_timeout_ms, MAX_POLICY_TIMEOUT_MS)
        if timeout_text is None:
            timeout_ms = bounded_subagent_ms
        else:
            timeout_ms = _policy_duration_ms(timeout_text, field_name="execution.timeout")
            if timeout_ms > bounded_subagent_ms:
                raise ValueError(
                    "Sub Agent execution.timeout must not exceed its resolved agent "
                    "timeout or PT10M"
                )
    else:
        timeout_ms = (
            MAX_POLICY_TIMEOUT_MS
            if timeout_text is None
            else _policy_duration_ms(timeout_text, field_name="execution.timeout")
        )

    if decorator_retry is not None:
        retry = decorator_retry
        retry_source: Literal["decorator", "task", "runtime_default"] = "decorator"
    elif execution.retry is not None:
        retry = execution.retry
        retry_source = "task"
    else:
        retry = WorkflowRetryPolicy()
        retry_source = "runtime_default"
    retry_delays_ms = precompute_retry_delays_ms(retry)
    native_retry_delays_ms = _native_retry_delays_ceiling_ms(retry)
    elapsed_ms = retry.max_attempts * timeout_ms + sum(native_retry_delays_ms)
    if elapsed_ms > MAX_POLICY_ELAPSED_MS:
        raise ValueError("configured attempt deadlines and retry delays must not exceed PT1H")
    return EffectiveWorkflowTaskExecution(
        timeout_ms=timeout_ms,
        max_attempts=retry.max_attempts,
        retry_delays_ms=retry_delays_ms,
        continue_on_error=execution.continue_on_error,
        timeout_source=timeout_source,
        retry_source=retry_source,
        durable_retry_policy=durable_retry_policy_input(retry),
    )


class WorkflowPlan(BaseModel):
    version: int = Field(default=1)
    tasks: list[WorkflowTask] = Field(..., min_length=1)


ECHO_TOOL_NAME: str = "__echo"

# Hard caps. M1 defaults; configurable from frontmatter lands in M5.
MAX_NODES: int = 50
MAX_PARALLELISM: int = 10
MAX_WAIT_DURATION: timedelta = timedelta(hours=24)

# Template ref grammar:
#   ${id.result}           — entire result
#   ${id.result.a.b.c}     — dotted path into a JSON-shaped result
# id matches task-id syntax (alnum / underscore / hyphen).
_TEMPLATE_RE = re.compile(
    r"\$\{([A-Za-z0-9_\-]+)\.result"
    r"(?:\.([A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*))?\}"
)
# Catches malformed ``${...}`` that doesn't conform to _TEMPLATE_RE so we
# can fail loudly rather than silently leaving the literal in the args.
_TEMPLATE_LIKE_RE = re.compile(r"\$\{[^}]*\}")
# Detects an unclosed ``${`` — a `${` that is not followed by a balanced
# ``}`` before end-of-string. We check this separately because
# _TEMPLATE_LIKE_RE requires the closing brace and would silently miss
# unterminated refs like ``"${a.result"``.
_TEMPLATE_UNCLOSED_RE = re.compile(r"\$\{[^}]*\Z")
_ITERATION_TEMPLATE_RE = re.compile(
    r"\$\{(?:(item)(?:\.([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*))?|(index))\}"
)
_ITERATION_LOCAL_NAMES = frozenset({"item", "index"})
_ITERATION_UNBOUND = object()


def validate_plan(
    raw: dict[str, Any],
    *,
    policy: WorkflowPlanPolicy | None = None,
    allowed_tools: Collection[str] | None = None,
) -> WorkflowPlan:
    """Validate and normalize a plan dict.

    ``policy`` is the immutable agent-specific authorization boundary used
    by both prompt guidance and runtime validation. ``allowed_tools`` remains
    as a compatibility-only input for callers predating sub-agent nodes.

    Raises :class:`PlanValidationError` with a caller-friendly message on
    any structural or semantic problem.
    """
    if policy is not None and allowed_tools is not None:
        raise TypeError("pass either policy or allowed_tools, not both")
    if policy is None:
        if allowed_tools is None:
            raise TypeError("validate_plan requires an explicit WorkflowPlanPolicy")
        policy = WorkflowPlanPolicy(
            allowed_tools=frozenset(allowed_tools),
            allowed_subagents=frozenset(),
        )
    try:
        plan = WorkflowPlan.model_validate(raw)
    except ValidationError as exc:
        metadata = _schema_validation_metadata(raw, exc)
        raise PlanValidationError(
            f"plan does not match schema: {exc}",
            **metadata,
        ) from exc

    if len(plan.tasks) > MAX_NODES:
        raise PlanValidationError(
            f"plan has {len(plan.tasks)} tasks but the per-plan limit is "
            f"{MAX_NODES}. Break the work into smaller workflows or reduce "
            "the number of nodes.",
            error_code="workflow_node_limit_exceeded",
            path="tasks",
        )

    seen: set[str] = set()
    for task in plan.tasks:
        if task.id in _ITERATION_LOCAL_NAMES:
            raise PlanValidationError(
                f"task id {task.id!r} is reserved for for_each iteration locals",
                error_code="workflow_task_id_reserved",
                node_id=task.id,
                path="id",
            )
        if task.id in seen:
            raise PlanValidationError(f"duplicate task id: {task.id!r}")
        seen.add(task.id)

        if task.type not in SUPPORTED_TASK_TYPES:
            raise PlanValidationError(
                f"task {task.id!r}: type {task.type!r} is not supported. "
                f"Supported types: {sorted(SUPPORTED_TASK_TYPES)}"
            )

        if task.type == TOOL_TASK_TYPE:
            if not task.tool:
                raise PlanValidationError(
                    f"task {task.id!r}: 'tool' field is required for "
                    "type=tool tasks"
                )
            _validate_static_target(task, task.tool, "tool")
            if task.tool not in policy.allowed_tools:
                raise PlanValidationError(
                    f"task {task.id!r}: tool {task.tool!r} is not workflow-safe. "
                    f"Allowed tools: {sorted(policy.allowed_tools)}"
                )
            if task.duration is not None or task.until is not None:
                raise PlanValidationError(
                    f"task {task.id!r}: 'duration' and 'until' are only "
                    "valid on type=wait tasks"
                )
            if "agent" in task.model_fields_set or "task" in task.model_fields_set:
                raise PlanValidationError(
                    f"task {task.id!r}: 'agent' and 'task' are only valid on "
                    "type=sub_agent tasks"
                )
        elif task.type == WAIT_TASK_TYPE:
            if "execution" in task.model_fields_set:
                raise PlanValidationError(
                    f"task {task.id!r}: 'execution' is not valid on type=wait tasks"
                )
            if task.tool is not None:
                raise PlanValidationError(
                    f"task {task.id!r}: 'tool' is not valid on type=wait tasks"
                )
            if task.args:
                raise PlanValidationError(
                    f"task {task.id!r}: 'args' is not valid on type=wait tasks "
                    "(use 'duration' or 'until' instead)"
                )
            if task.for_each is not None:
                raise PlanValidationError(
                    f"task {task.id!r}: 'for_each' is not valid on type=wait tasks",
                    error_code="workflow_reference_unresolved",
                    node_id=task.id,
                    path="for_each",
                )
            if "agent" in task.model_fields_set or "task" in task.model_fields_set:
                raise PlanValidationError(
                    f"task {task.id!r}: 'agent' and 'task' are only valid on "
                    "type=sub_agent tasks"
                )
            has_duration = task.duration is not None
            has_until = task.until is not None
            if has_duration == has_until:
                raise PlanValidationError(
                    f"task {task.id!r}: type=wait tasks must specify exactly "
                    "one of 'duration' (ISO-8601 like 'PT30S') or 'until' "
                    "(ISO-8601 datetime); not both, not neither"
                )
            if has_duration:
                try:
                    assert task.duration is not None
                    delta = parse_iso8601_duration(task.duration)
                except ValueError as exc:
                    raise PlanValidationError(
                        f"task {task.id!r}: invalid duration {task.duration!r}: "
                        f"{exc}"
                    ) from exc
                if delta <= timedelta(0):
                    raise PlanValidationError(
                        f"task {task.id!r}: duration must be positive"
                    )
                if delta > MAX_WAIT_DURATION:
                    raise PlanValidationError(
                        f"task {task.id!r}: duration exceeds the maximum of "
                        f"{MAX_WAIT_DURATION}"
                    )
            else:
                try:
                    assert task.until is not None
                    until_dt = parse_iso8601_datetime(task.until)
                except ValueError as exc:
                    raise PlanValidationError(
                        f"task {task.id!r}: invalid until {task.until!r}: {exc}"
                    ) from exc
                # Cap `until` at the same horizon as `duration`. Using
                # wall-clock at submit time is fine — this is a submit-time
                # admission gate, not a replay-deterministic computation.
                # Defense-in-depth check happens again in the orchestrator
                # against context.current_utc_datetime (see engine.py).
                horizon = datetime.now(UTC) + MAX_WAIT_DURATION
                if until_dt > horizon:
                    raise PlanValidationError(
                        f"task {task.id!r}: until {task.until!r} is more than "
                        f"{MAX_WAIT_DURATION} in the future"
                    )
        elif task.type == SUB_AGENT_TASK_TYPE:
            if not task.agent or not task.agent.strip():
                raise PlanValidationError(
                    f"task {task.id!r}: 'agent' field is required and must be "
                    "non-empty for type=sub_agent tasks"
                )
            _validate_static_target(task, task.agent, "agent")
            if not task.task or not task.task.strip():
                raise PlanValidationError(
                    f"task {task.id!r}: 'task' field is required and must be "
                    "non-empty for type=sub_agent tasks"
                )
            forbidden = [
                field
                for field in ("tool", "args", "duration", "until")
                if field in task.model_fields_set
            ]
            if forbidden:
                listed = ", ".join(repr(field) for field in forbidden)
                raise PlanValidationError(
                    f"task {task.id!r}: {listed} is not valid on type=sub_agent tasks"
                )
            if task.agent not in policy.allowed_subagents:
                raise PlanValidationError(
                    f"task {task.id!r}: Sub Agent {task.agent!r} is not authorized "
                    f"for this workflow-enabled agent. Allowed Sub Agents: "
                    f"{sorted(policy.allowed_subagents)}"
                )

    # Validate ``depends_on`` edges reference known task ids (no self-loops,
    # no duplicates) and detect cycles.
    by_id: dict[str, WorkflowTask] = {t.id: t for t in plan.tasks}
    for task in plan.tasks:
        dep_set: set[str] = set()
        for dep in task.depends_on:
            if dep == task.id:
                raise PlanValidationError(
                    f"task {task.id!r}: depends_on cannot reference itself"
                )
            if dep in dep_set:
                raise PlanValidationError(
                    f"task {task.id!r}: duplicate dependency {dep!r}"
                )
            if dep not in by_id:
                raise PlanValidationError(
                    f"task {task.id!r}: depends_on references unknown task {dep!r}"
                )
            dep_set.add(dep)

    cycle = _detect_cycle(plan)
    if cycle is not None:
        pretty = " -> ".join(cycle)
        raise PlanValidationError(
            f"plan contains a dependency cycle: {pretty}"
        )

    # Validate templating refs against the upstream closure of each task.
    upstream = _upstream_closure(plan)
    for task in plan.tasks:
        _validate_task_templates(task, upstream[task.id], by_id)
        _validate_for_each(task, upstream[task.id], by_id)
        _validate_when(task, upstream[task.id], by_id)

    return plan


def _validate_static_target(task: WorkflowTask, target: str, field: str) -> None:
    if _TEMPLATE_LIKE_RE.search(target) or _TEMPLATE_UNCLOSED_RE.search(target):
        raise PlanValidationError(
            f"task {task.id!r}: '{field}' target must be static and cannot contain "
            "template references",
            error_code="workflow_reference_unresolved",
            node_id=task.id,
            path=field,
        )


def _schema_validation_metadata(
    raw: Mapping[str, object], exc: ValidationError
) -> PlanValidationMetadata:
    """Map newly introduced plan fields to their stable submission error code."""
    for error in exc.errors():
        loc = error["loc"]
        if len(loc) < 3 or loc[0] != "tasks" or not isinstance(loc[1], int):
            continue
        field = loc[2]
        if field not in {"when", "for_each"}:
            continue
        node_id: str | None = None
        tasks = raw.get("tasks")
        if isinstance(tasks, list) and loc[1] < len(tasks):
            candidate = tasks[loc[1]]
            if isinstance(candidate, Mapping):
                candidate_id = candidate.get("id")
                if isinstance(candidate_id, str):
                    node_id = candidate_id
        path = ".".join(str(segment) for segment in loc[2:])
        metadata: PlanValidationMetadata
        if field == "when":
            metadata = {"error_code": "workflow_condition_invalid", "path": path}
        else:
            metadata = {"error_code": "workflow_reference_unresolved", "path": path}
        if node_id is not None:
            metadata["node_id"] = node_id
        return metadata
    return {}


def _detect_cycle(plan: WorkflowPlan) -> list[str] | None:
    """Return a cycle as an ordered task-id list if present, else None."""
    white, gray, black = 0, 1, 2
    color: dict[str, int] = {t.id: white for t in plan.tasks}
    deps: dict[str, list[str]] = {t.id: list(t.depends_on) for t in plan.tasks}
    parent: dict[str, str] = {}

    def dfs(start: str) -> list[str] | None:
        # Iterative DFS so deeply-nested plans don't blow the recursion limit.
        stack: list[tuple[str, int]] = [(start, 0)]
        path: list[str] = []
        while stack:
            node, idx = stack[-1]
            if idx == 0:
                if color[node] == gray:
                    # Cycle: walk back via ``parent`` until we close the loop.
                    cycle = [node]
                    cur = path[-1]
                    while cur != node:
                        cycle.append(cur)
                        cur = parent[cur]
                    cycle.append(node)
                    cycle.reverse()
                    return cycle
                if color[node] == black:
                    stack.pop()
                    continue
                color[node] = gray
                path.append(node)
            children = deps[node]
            if idx < len(children):
                stack[-1] = (node, idx + 1)
                child = children[idx]
                if color.get(child, black) != black:
                    parent[child] = node
                    stack.append((child, 0))
                continue
            color[node] = black
            path.pop()
            stack.pop()
        return None

    for tid in deps:
        if color[tid] == white:
            cycle = dfs(tid)
            if cycle is not None:
                return cycle
    return None


def _upstream_closure(plan: WorkflowPlan) -> dict[str, set[str]]:
    """Return ``{task_id: set of all transitive predecessors}``.

    Cycle detection is expected to have run before this; the
    ``in_progress`` guard is defense-in-depth so a future refactor that
    moved this earlier wouldn't blow the recursion limit on a cycle.
    """
    deps: dict[str, list[str]] = {t.id: list(t.depends_on) for t in plan.tasks}
    closure: dict[str, set[str]] = {}
    in_progress: set[str] = set()

    def compute(tid: str) -> set[str]:
        if tid in closure:
            return closure[tid]
        if tid in in_progress:
            # Should be unreachable — _detect_cycle ran first. Raising here
            # surfaces the invariant violation instead of recursing forever.
            raise PlanValidationError(
                f"internal error: cycle reached _upstream_closure at {tid!r}"
            )
        in_progress.add(tid)
        acc: set[str] = set()
        for d in deps[tid]:
            acc.add(d)
            acc.update(compute(d))
        closure[tid] = acc
        in_progress.discard(tid)
        return acc

    for tid in deps:
        compute(tid)
    return closure


def _validate_task_templates(
    task: WorkflowTask,
    upstream_ids: set[str],
    by_id: dict[str, WorkflowTask],
) -> None:
    """Ensure every ``${...}`` reference in the task's args is well-formed
    and points to an upstream task.
    """
    template_root = "task" if task.type == SUB_AGENT_TASK_TYPE else "args"
    template_value: Any = task.task if task.type == SUB_AGENT_TASK_TYPE else task.args
    for path, value in _walk_strings(template_value, ()):
        _validate_template_string(
            task,
            value,
            root=template_root,
            value_path=_format_value_path(template_root, path),
            upstream_ids=upstream_ids,
            by_id=by_id,
            allow_iteration=task.for_each is not None,
        )


def _validate_for_each(
    task: WorkflowTask,
    upstream_ids: set[str],
    by_id: dict[str, WorkflowTask],
) -> None:
    if task.for_each is None:
        return
    if task.type == WAIT_TASK_TYPE:
        return
    ref_match = _TEMPLATE_RE.fullmatch(task.for_each)
    if ref_match is None:
        raise PlanValidationError(
            f"task {task.id!r}: 'for_each' must be one full upstream "
            "reference like '${node_id.result.items}'",
            error_code="workflow_reference_unresolved",
            node_id=task.id,
            path="for_each",
        )
    _validate_upstream_reference(
        task,
        ref_match.group(1),
        upstream_ids,
        by_id,
        root="for_each",
        value_path="for_each",
    )


def _validate_when(
    task: WorkflowTask,
    upstream_ids: set[str],
    by_id: dict[str, WorkflowTask],
) -> None:
    if task.when is None:
        return
    ref = task.when.ref
    iteration_match = _ITERATION_TEMPLATE_RE.fullmatch(ref)
    if iteration_match is not None and task.for_each is not None:
        return
    upstream_match = _TEMPLATE_RE.fullmatch(ref)
    if upstream_match is not None:
        _validate_upstream_reference(
            task,
            upstream_match.group(1),
            upstream_ids,
            by_id,
            root="when",
            value_path="when.ref",
        )
        return
    if iteration_match is not None:
        raise PlanValidationError(
            f"task {task.id!r}: iteration local {ref!r} at when.ref is only "
            "available on for_each tasks",
            error_code="workflow_reference_unresolved",
            node_id=task.id,
            path="when.ref",
        )
    raise PlanValidationError(
        f"task {task.id!r}: 'when.ref' must be one full upstream or iteration "
        "reference",
        error_code="workflow_condition_invalid",
        node_id=task.id,
        path="when.ref",
    )


def _validate_template_string(
    task: WorkflowTask,
    value: str,
    *,
    root: str,
    value_path: str,
    upstream_ids: set[str],
    by_id: dict[str, WorkflowTask],
    allow_iteration: bool,
) -> None:
    if _TEMPLATE_UNCLOSED_RE.search(value):
        raise PlanValidationError(
            f"task {task.id!r}: unterminated template reference at "
            f"{root} path {value_path} — missing closing '}}'",
            error_code="workflow_reference_unresolved",
            node_id=task.id,
            path=value_path,
        )
    for like_match in _TEMPLATE_LIKE_RE.finditer(value):
        literal = like_match.group(0)
        iteration_match = _ITERATION_TEMPLATE_RE.fullmatch(literal)
        if iteration_match is not None and allow_iteration:
            continue
        ref_match = _TEMPLATE_RE.fullmatch(literal)
        if ref_match is not None:
            _validate_upstream_reference(
                task,
                ref_match.group(1),
                upstream_ids,
                by_id,
                root=root,
                value_path=value_path,
            )
            continue
        if iteration_match is not None:
            raise PlanValidationError(
                f"task {task.id!r}: iteration local {literal!r} at "
                f"{root} path {value_path} is only available on for_each tasks",
                error_code="workflow_reference_unresolved",
                node_id=task.id,
                path=value_path,
            )
        raise PlanValidationError(
            f"task {task.id!r}: malformed template "
            f"reference {literal!r} at {root} path {value_path} — expected "
            "${{node_id.result}} or ${{node_id.result.path}}",
            error_code="workflow_reference_unresolved",
            node_id=task.id,
            path=value_path,
        )


def _validate_upstream_reference(
    task: WorkflowTask,
    ref_id: str,
    upstream_ids: set[str],
    by_id: dict[str, WorkflowTask],
    *,
    root: str,
    value_path: str,
) -> None:
    if ref_id not in by_id:
        raise PlanValidationError(
            f"task {task.id!r}: template references unknown task "
            f"{ref_id!r} at {root} path {value_path}",
            error_code="workflow_reference_unresolved",
            node_id=task.id,
            path=value_path,
        )
    if ref_id not in upstream_ids:
        raise PlanValidationError(
            f"task {task.id!r}: template references {ref_id!r} which "
            "is not an upstream dependency. Add it to depends_on or "
            "remove the reference.",
            error_code="workflow_reference_unresolved",
            node_id=task.id,
            path=value_path,
        )


def _walk_strings(
    obj: Any, path: tuple[Any, ...]
) -> list[tuple[tuple[Any, ...], str]]:
    """Yield (path-tuple, string-value) pairs for every string leaf in obj."""
    out: list[tuple[tuple[Any, ...], str]] = []
    if isinstance(obj, str):
        out.append((path, obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_walk_strings(v, (*path, k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_walk_strings(v, (*path, i)))
    return out


def _format_value_path(root: str, path: tuple[Any, ...]) -> str:
    if not path:
        return "<root>"
    parts: list[str] = []
    for p in path:
        parts.append(f"[{p}]" if isinstance(p, int) else f".{p}")
    return root + "".join(parts)


def resolve_template_value(
    value: Any,
    results: dict[str, Any],
    *,
    item: Any = _ITERATION_UNBOUND,
    index: int | None = None,
) -> Any:
    """Substitute template refs in ``value`` against ``results``.

    Used by the orchestrator immediately before scheduling each task. The
    results dict is keyed by task id and holds JSON-normalized outputs of
    completed upstream tasks. Raises :class:`TemplateResolutionError` if a
    referenced node hasn't completed (which should be impossible if the
    plan was validated and the wave scheduler is correct) or a dotted path
    cannot be traversed.
    """
    if isinstance(value, str):
        iteration_full = _ITERATION_TEMPLATE_RE.fullmatch(value)
        iteration_bound = item is not _ITERATION_UNBOUND or index is not None
        full = _TEMPLATE_RE.fullmatch(value)
        if iteration_full is not None and (iteration_bound or full is None):
            return _resolve_iteration_ref(
                iteration_full.group(1),
                iteration_full.group(2),
                iteration_full.group(3),
                item,
                index,
            )
        if full is not None:
            return _resolve_ref(full.group(1), full.group(2), results)
        any_like = _TEMPLATE_LIKE_RE.search(value)
        if any_like is None and not _TEMPLATE_UNCLOSED_RE.search(value):
            return value

        def repl(match: re.Match[str]) -> str:
            literal = match.group(0)
            iteration_match = _ITERATION_TEMPLATE_RE.fullmatch(literal)
            ref_match = _TEMPLATE_RE.fullmatch(literal)
            if iteration_match is not None and (iteration_bound or ref_match is None):
                resolved = _resolve_iteration_ref(
                    iteration_match.group(1),
                    iteration_match.group(2),
                    iteration_match.group(3),
                    item,
                    index,
                )
            else:
                if ref_match is None:
                    return literal
                resolved = _resolve_ref(ref_match.group(1), ref_match.group(2), results)
            if isinstance(resolved, str):
                return resolved
            return json.dumps(resolved, sort_keys=True)

        substituted = _TEMPLATE_LIKE_RE.sub(repl, value)
        # Defense-in-depth: if validation was bypassed, an unmatched
        # ``${...}`` token could survive substitution. Surface that as a
        # deterministic failure rather than passing a half-resolved string
        # to the activity.
        leftover = _TEMPLATE_LIKE_RE.search(substituted) or _TEMPLATE_UNCLOSED_RE.search(
            substituted
        )
        if leftover is not None:
            raise TemplateResolutionError(
                f"unresolved template token {leftover.group(0)!r} survived "
                "substitution (plan validation may have been bypassed)"
            )
        return substituted
    if isinstance(value, dict):
        return {
            k: resolve_template_value(v, results, item=item, index=index)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [resolve_template_value(v, results, item=item, index=index) for v in value]
    return value


def _resolve_ref(node_id: str, dotted_path: str | None, results: dict[str, Any]) -> Any:
    if node_id not in results:
        raise TemplateResolutionError(
            f"template references {node_id!r} but no result is available "
            "(upstream task hasn't completed)"
        )
    cur: Any = results[node_id]
    if not dotted_path:
        return cur
    return _resolve_path(f"{node_id}.result", dotted_path, cur)


def _resolve_iteration_ref(
    item_name: str | None,
    dotted_path: str | None,
    index_name: str | None,
    item: Any,
    index: int | None,
) -> Any:
    if index_name is not None:
        if index is None:
            raise TemplateResolutionError(
                "template references '${index}' but no iteration index is bound"
            )
        return index
    if item_name is None:
        raise TemplateResolutionError("invalid iteration template reference")
    if item is _ITERATION_UNBOUND:
        raise TemplateResolutionError(
            "template references '${item}' but no iteration item is bound"
        )
    if dotted_path is None:
        return item
    return _resolve_path("item", dotted_path, item)


def _resolve_path(root: str, dotted_path: str, value: Any) -> Any:
    cur: Any = value
    parts = dotted_path.split(".")
    for i, part in enumerate(parts):
        if isinstance(cur, dict):
            if part not in cur:
                raise TemplateResolutionError(
                    f"template path ${{{root}.{dotted_path}}} "
                    f"failed at segment {part!r}: key not present"
                )
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                idx = int(part)
            except ValueError as exc:
                raise TemplateResolutionError(
                    f"template path ${{{root}.{dotted_path}}} "
                    f"failed at segment {part!r}: list index must be an integer"
                ) from exc
            if idx < 0 or idx >= len(cur):
                raise TemplateResolutionError(
                    f"template path ${{{root}.{dotted_path}}} "
                    f"failed at segment {part!r}: index out of range"
                )
            cur = cur[idx]
        else:
            traversed = ".".join(parts[:i])
            raise TemplateResolutionError(
                f"template path ${{{root}.{dotted_path}}} "
                f"failed at segment {part!r}: parent value at "
                f"{traversed or '<root>'} is not a dict or list"
            )
    return cur


def evaluate_condition(
    condition: WorkflowCondition,
    results: dict[str, Any],
    *,
    item: Any = _ITERATION_UNBOUND,
    index: int | None = None,
) -> bool:
    """Evaluate a validated condition with type-sensitive equality semantics."""
    resolved = resolve_template_value(condition.ref, results, item=item, index=index)
    if not _is_json_scalar(resolved):
        raise TemplateResolutionError(
            f"condition reference {condition.ref!r} resolved to a non-scalar value",
            error_code="workflow_condition_invalid",
        )
    equals = type(resolved) is type(condition.value) and resolved == condition.value
    return equals if condition.operator == "equals" else not equals


def plan_to_activity_inputs(
    plan: WorkflowPlan,
    effective_policies: Mapping[str, EffectiveWorkflowTaskExecution] | None = None,
) -> list[WorkflowTaskInput]:
    """Flatten a validated plan into the JSON list the orchestrator iterates.

    The orchestrator needs ``depends_on`` to drive wave scheduling, plus
    ``type`` so it knows whether to call an activity or schedule a timer,
    so we keep them on the wire alongside id/tool/args/duration/until.
    """
    out: list[WorkflowTaskInput] = []
    for t in plan.tasks:
        if t.type == TOOL_TASK_TYPE:
            if t.tool is None:
                raise RuntimeError(f"validated tool task {t.id!r} has no tool")
            entry: WorkflowTaskInput = ToolWorkflowTaskInput(
                id=t.id,
                type="tool",
                depends_on=list(t.depends_on),
                tool=t.tool,
                args=dict(t.args),
            )
        elif t.type == WAIT_TASK_TYPE:
            wait_entry = WaitWorkflowTaskInput(
                id=t.id,
                type="wait",
                depends_on=list(t.depends_on),
            )
            if t.duration is not None:
                wait_entry["duration"] = t.duration
            if t.until is not None:
                wait_entry["until"] = t.until
            entry = wait_entry
        else:
            if t.agent is None or t.task is None:
                raise RuntimeError(f"validated Sub Agent task {t.id!r} is incomplete")
            entry = SubAgentWorkflowTaskInput(
                id=t.id,
                type="sub_agent",
                depends_on=list(t.depends_on),
                agent=t.agent,
                task=t.task,
            )
        if t.when is not None:
            entry["when"] = WorkflowConditionInput(
                ref=t.when.ref,
                operator=t.when.operator,
                value=t.when.value,
            )
        if t.for_each is not None:
            entry["for_each"] = t.for_each
        if effective_policies is not None and t.id in effective_policies:
            if t.type == WAIT_TASK_TYPE:
                raise RuntimeError(f"validated wait task {t.id!r} has execution policy")
            capability_entry = cast(
                "ToolWorkflowTaskInput | SubAgentWorkflowTaskInput",
                entry,
            )
            capability_entry["execution"] = effective_policies[t.id]
        out.append(entry)
    return out


# ISO-8601 helpers ---------------------------------------------------------
#
# We accept the ``PnDTnHnMnS`` subset of ISO-8601 durations because that is
# what humans (and LLMs) actually emit, and the full grammar (years/months,
# week form, fractional components in non-final positions) is overkill.
# Acceptance grammar:
#   P[<n>D][T[<n>H][<n>M][<n>(.<n>)?S]]
# At least one component is required. ``until`` accepts ISO-8601 datetimes
# via ``datetime.fromisoformat`` plus the trailing-Z form some emitters use.

_DURATION_RE = re.compile(
    r"^P"
    r"(?:(?P<days>\d+)D)?"
    r"(?:T"
    r"(?:(?P<hours>\d+)H)?"
    r"(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?"
    r")?$"
)


def parse_iso8601_duration(text: str) -> timedelta:
    """Parse an ISO-8601 duration in the ``PnDTnHnMnS`` subset.

    Raises ``ValueError`` with a caller-friendly message on any parse
    problem. Returns a ``timedelta``; callers enforce upper/lower bounds.
    """
    if not isinstance(text, str) or not text:
        raise ValueError("duration must be a non-empty ISO-8601 string")
    m = _DURATION_RE.match(text)
    if not m:
        raise ValueError(
            "expected ISO-8601 duration like 'PT30S', 'PT5M', 'PT1H30M', "
            "or 'P1DT2H'"
        )
    if all(g is None for g in m.groupdict().values()):
        raise ValueError("duration has no components")
    if "T" in text and all(
        m.group(name) is None for name in ("hours", "minutes", "seconds")
    ):
        raise ValueError("duration time section has no components")
    days = int(m.group("days") or 0)
    hours = int(m.group("hours") or 0)
    minutes = int(m.group("minutes") or 0)
    seconds = float(m.group("seconds") or 0.0)
    try:
        return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    except OverflowError as exc:
        # `timedelta(...)` raises OverflowError for oversized inputs (e.g.
        # P1000000000D). Re-raise as ValueError so callers — which only
        # catch ValueError to produce caller-friendly PlanValidationError —
        # see a clean rejection path.
        raise ValueError(f"duration is too large: {exc}") from exc


def parse_iso8601_datetime(text: str) -> datetime:
    """Parse an ISO-8601 datetime; require explicit timezone awareness.

    The trailing-``Z`` form is accepted as UTC for convenience. Naive
    datetimes are rejected — mixing tz-naive and tz-aware datetimes inside
    the orchestrator would surface as confusing TypeErrors at run time.
    """
    if not isinstance(text, str) or not text:
        raise ValueError("until must be a non-empty ISO-8601 string")
    candidate = text
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(
            "expected ISO-8601 datetime like '2026-04-25T17:30:00Z' or "
            "'2026-04-25T10:30:00-07:00'"
        ) from exc
    if dt.tzinfo is None:
        raise ValueError(
            "datetime must include a timezone offset (use trailing 'Z' for "
            "UTC, or an explicit '+HH:MM' offset)"
        )
    return dt.astimezone(UTC)


__all__ = [
    "ECHO_TOOL_NAME",
    "MAX_NODES",
    "MAX_PARALLELISM",
    "MAX_POLICY_ATTEMPTS",
    "MAX_POLICY_ELAPSED_MS",
    "MAX_POLICY_TIMEOUT_MS",
    "MAX_WAIT_DURATION",
    "SUB_AGENT_TASK_TYPE",
    "SUPPORTED_TASK_TYPES",
    "TOOL_TASK_TYPE",
    "WAIT_TASK_TYPE",
    "EffectiveWorkflowTaskExecution",
    "PlanValidationError",
    "TemplateResolutionError",
    "WorkflowCondition",
    "WorkflowPlan",
    "WorkflowPlanPolicy",
    "WorkflowRetryBackoff",
    "WorkflowRetryPolicy",
    "WorkflowRetryableError",
    "WorkflowTask",
    "WorkflowTaskExecution",
    "WorkflowTerminalError",
    "WorkflowToolExecutionPolicy",
    "evaluate_condition",
    "parse_iso8601_datetime",
    "parse_iso8601_duration",
    "plan_to_activity_inputs",
    "precompute_retry_delays_ms",
    "resolve_template_value",
    "resolve_workflow_task_execution",
    "validate_plan",
]
