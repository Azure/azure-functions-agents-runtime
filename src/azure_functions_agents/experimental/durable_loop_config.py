"""Private fail-closed configuration for the durable agent-loop experiment."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..config.schema import GlobalConfig, ResolvedAgent

DURABLE_LOOP_ENABLED_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_ENABLED"
)
DURABLE_LOOP_LOCAL_SAMPLE_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_LOCAL_SAMPLE_ENABLED"
)
DURABLE_LOOP_CONTENT_BLOB_URI_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_CONTENT_BLOB_URI"
)
DURABLE_LOOP_CONTENT_CONTAINER_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_CONTENT_CONTAINER"
)
DURABLE_LOOP_CONTENT_CLIENT_ID_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_CONTENT_CLIENT_ID"
)
DURABLE_LOOP_MAX_MODEL_STEPS_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_MAX_MODEL_STEPS"
)
DURABLE_LOOP_MAX_TOOL_CALLS_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_MAX_TOOL_CALLS"
)
DURABLE_LOOP_MAX_TOTAL_TOKENS_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_MAX_TOTAL_TOKENS"
)
DURABLE_LOOP_MAX_COST_MICROUNITS_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_MAX_COST_MICROUNITS"
)
DURABLE_LOOP_INPUT_COST_RATE_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_INPUT_COST_"
    "MICROUNITS_PER_MILLION_TOKENS"
)
DURABLE_LOOP_OUTPUT_COST_RATE_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_OUTPUT_COST_"
    "MICROUNITS_PER_MILLION_TOKENS"
)
DURABLE_LOOP_MAX_ELAPSED_SECONDS_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_MAX_ELAPSED_SECONDS"
)
DURABLE_LOOP_MAX_HUMAN_WAITS_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_MAX_HUMAN_WAITS"
)
DURABLE_LOOP_HUMAN_WAIT_SECONDS_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_HUMAN_WAIT_SECONDS"
)
DURABLE_LOOP_MAX_RUN_WAIT_SECONDS_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_MAX_RUN_WAIT_SECONDS"
)
DURABLE_LOOP_MAX_ARGUMENT_BYTES_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_MAX_ARGUMENT_BYTES"
)
DURABLE_LOOP_MAX_RESULT_BYTES_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_MAX_RESULT_BYTES"
)
DURABLE_LOOP_MAX_EXTERNAL_CONTENT_BYTES_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_MAX_EXTERNAL_CONTENT_BYTES"
)
DURABLE_LOOP_ACTIVITY_TIMEOUT_SECONDS_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_ACTIVITY_TIMEOUT_SECONDS"
)
DURABLE_LOOP_LOCAL_TOOL_TIMEOUT_SECONDS_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_LOCAL_TOOL_TIMEOUT_SECONDS"
)
DURABLE_LOOP_POLL_INITIAL_SECONDS_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_POLL_INITIAL_SECONDS"
)
DURABLE_LOOP_POLL_MAX_SECONDS_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_POLL_MAX_SECONDS"
)
DURABLE_LOOP_CONTEXT_MAX_BYTES_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_CONTEXT_MAX_BYTES"
)
DURABLE_LOOP_CONTEXT_COMPACTION_PERCENT_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_CONTEXT_COMPACTION_PERCENT"
)
DURABLE_LOOP_MAX_PARALLEL_READS_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_MAX_PARALLEL_READS"
)
DURABLE_LOOP_CONTINUE_AS_NEW_CHECKPOINTS_ENV = (
    "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_CONTINUE_AS_NEW_CHECKPOINTS"
)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_DEFAULT_MAX_MODEL_STEPS = 48
_DEFAULT_MAX_TOOL_CALLS = 128
_DEFAULT_MAX_TOTAL_TOKENS = 1_000_000
_DEFAULT_MAX_COST_MICROUNITS = 0
_DEFAULT_MAX_ELAPSED_SECONDS = 4 * 60 * 60
_DEFAULT_MAX_HUMAN_WAITS = 8
_DEFAULT_HUMAN_WAIT_SECONDS = 24 * 60 * 60
_DEFAULT_MAX_RUN_WAIT_SECONDS = 7 * 24 * 60 * 60
_DEFAULT_MAX_ARGUMENT_BYTES = 256 * 1024
_DEFAULT_MAX_RESULT_BYTES = 1024 * 1024
_DEFAULT_MAX_EXTERNAL_CONTENT_BYTES = 32 * 1024 * 1024
_DEFAULT_ACTIVITY_TIMEOUT_SECONDS = 8 * 60
_DEFAULT_LOCAL_TOOL_TIMEOUT_SECONDS = 5 * 60
_DEFAULT_POLL_INITIAL_SECONDS = 2.0
_DEFAULT_POLL_MAX_SECONDS = 30.0
_DEFAULT_CONTEXT_MAX_BYTES = 4 * 1024 * 1024
_DEFAULT_CONTEXT_COMPACTION_PERCENT = 75
_DEFAULT_MAX_PARALLEL_READS = 4
_DEFAULT_CONTINUE_AS_NEW_CHECKPOINTS = 20


class DurableLoopConfigurationError(RuntimeError):
    """The private durable-loop configuration is invalid."""


@dataclass(frozen=True, slots=True)
class DurableLoopSettings:
    """Validated process settings snapshotted into every durable run."""

    max_model_steps: int = _DEFAULT_MAX_MODEL_STEPS
    max_tool_calls: int = _DEFAULT_MAX_TOOL_CALLS
    max_total_tokens: int = _DEFAULT_MAX_TOTAL_TOKENS
    max_cost_microunits: int = _DEFAULT_MAX_COST_MICROUNITS
    input_cost_microunits_per_million_tokens: int = 0
    output_cost_microunits_per_million_tokens: int = 0
    max_elapsed_seconds: int = _DEFAULT_MAX_ELAPSED_SECONDS
    max_human_waits: int = _DEFAULT_MAX_HUMAN_WAITS
    human_wait_seconds: int = _DEFAULT_HUMAN_WAIT_SECONDS
    max_run_wait_seconds: int = _DEFAULT_MAX_RUN_WAIT_SECONDS
    max_argument_bytes: int = _DEFAULT_MAX_ARGUMENT_BYTES
    max_result_bytes: int = _DEFAULT_MAX_RESULT_BYTES
    max_external_content_bytes: int = _DEFAULT_MAX_EXTERNAL_CONTENT_BYTES
    activity_timeout_seconds: int = _DEFAULT_ACTIVITY_TIMEOUT_SECONDS
    local_tool_timeout_seconds: int = _DEFAULT_LOCAL_TOOL_TIMEOUT_SECONDS
    poll_initial_seconds: float = _DEFAULT_POLL_INITIAL_SECONDS
    poll_max_seconds: float = _DEFAULT_POLL_MAX_SECONDS
    context_max_bytes: int = _DEFAULT_CONTEXT_MAX_BYTES
    context_compaction_percent: int = _DEFAULT_CONTEXT_COMPACTION_PERCENT
    max_parallel_reads: int = _DEFAULT_MAX_PARALLEL_READS
    continue_as_new_checkpoints: int = _DEFAULT_CONTINUE_AS_NEW_CHECKPOINTS
    mixed_batch_repair_steps: int = 1

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> DurableLoopSettings | None:
        """Return validated settings only when a private gate is enabled."""
        source = os.environ if environment is None else environment
        enabled = _optional_bool(source, DURABLE_LOOP_ENABLED_ENV)
        sample_enabled = _optional_bool(source, DURABLE_LOOP_LOCAL_SAMPLE_ENV)
        if not enabled and not sample_enabled:
            return None

        settings = cls(
            max_model_steps=_bounded_integer(
                source,
                DURABLE_LOOP_MAX_MODEL_STEPS_ENV,
                _DEFAULT_MAX_MODEL_STEPS,
                minimum=1,
                maximum=256,
            ),
            max_tool_calls=_bounded_integer(
                source,
                DURABLE_LOOP_MAX_TOOL_CALLS_ENV,
                _DEFAULT_MAX_TOOL_CALLS,
                minimum=0,
                maximum=2048,
            ),
            max_total_tokens=_bounded_integer(
                source,
                DURABLE_LOOP_MAX_TOTAL_TOKENS_ENV,
                _DEFAULT_MAX_TOTAL_TOKENS,
                minimum=1,
                maximum=100_000_000,
            ),
            max_cost_microunits=_bounded_integer(
                source,
                DURABLE_LOOP_MAX_COST_MICROUNITS_ENV,
                _DEFAULT_MAX_COST_MICROUNITS,
                minimum=0,
                maximum=10_000_000_000,
            ),
            input_cost_microunits_per_million_tokens=_bounded_integer(
                source,
                DURABLE_LOOP_INPUT_COST_RATE_ENV,
                0,
                minimum=0,
                maximum=10_000_000_000,
            ),
            output_cost_microunits_per_million_tokens=_bounded_integer(
                source,
                DURABLE_LOOP_OUTPUT_COST_RATE_ENV,
                0,
                minimum=0,
                maximum=10_000_000_000,
            ),
            max_elapsed_seconds=_bounded_integer(
                source,
                DURABLE_LOOP_MAX_ELAPSED_SECONDS_ENV,
                _DEFAULT_MAX_ELAPSED_SECONDS,
                minimum=1,
                maximum=7 * 24 * 60 * 60,
            ),
            max_human_waits=_bounded_integer(
                source,
                DURABLE_LOOP_MAX_HUMAN_WAITS_ENV,
                _DEFAULT_MAX_HUMAN_WAITS,
                minimum=0,
                maximum=64,
            ),
            human_wait_seconds=_bounded_integer(
                source,
                DURABLE_LOOP_HUMAN_WAIT_SECONDS_ENV,
                _DEFAULT_HUMAN_WAIT_SECONDS,
                minimum=1,
                maximum=7 * 24 * 60 * 60,
            ),
            max_run_wait_seconds=_bounded_integer(
                source,
                DURABLE_LOOP_MAX_RUN_WAIT_SECONDS_ENV,
                _DEFAULT_MAX_RUN_WAIT_SECONDS,
                minimum=1,
                maximum=30 * 24 * 60 * 60,
            ),
            max_argument_bytes=_bounded_integer(
                source,
                DURABLE_LOOP_MAX_ARGUMENT_BYTES_ENV,
                _DEFAULT_MAX_ARGUMENT_BYTES,
                minimum=1024,
                maximum=1024 * 1024,
            ),
            max_result_bytes=_bounded_integer(
                source,
                DURABLE_LOOP_MAX_RESULT_BYTES_ENV,
                _DEFAULT_MAX_RESULT_BYTES,
                minimum=1024,
                maximum=8 * 1024 * 1024,
            ),
            max_external_content_bytes=_bounded_integer(
                source,
                DURABLE_LOOP_MAX_EXTERNAL_CONTENT_BYTES_ENV,
                _DEFAULT_MAX_EXTERNAL_CONTENT_BYTES,
                minimum=1024,
                maximum=256 * 1024 * 1024,
            ),
            activity_timeout_seconds=_bounded_integer(
                source,
                DURABLE_LOOP_ACTIVITY_TIMEOUT_SECONDS_ENV,
                _DEFAULT_ACTIVITY_TIMEOUT_SECONDS,
                minimum=1,
                maximum=8 * 60,
            ),
            local_tool_timeout_seconds=_bounded_integer(
                source,
                DURABLE_LOOP_LOCAL_TOOL_TIMEOUT_SECONDS_ENV,
                _DEFAULT_LOCAL_TOOL_TIMEOUT_SECONDS,
                minimum=1,
                maximum=5 * 60,
            ),
            poll_initial_seconds=_bounded_float(
                source,
                DURABLE_LOOP_POLL_INITIAL_SECONDS_ENV,
                _DEFAULT_POLL_INITIAL_SECONDS,
                minimum=0.1,
                maximum=30.0,
            ),
            poll_max_seconds=_bounded_float(
                source,
                DURABLE_LOOP_POLL_MAX_SECONDS_ENV,
                _DEFAULT_POLL_MAX_SECONDS,
                minimum=0.1,
                maximum=300.0,
            ),
            context_max_bytes=_bounded_integer(
                source,
                DURABLE_LOOP_CONTEXT_MAX_BYTES_ENV,
                _DEFAULT_CONTEXT_MAX_BYTES,
                minimum=64 * 1024,
                maximum=16 * 1024 * 1024,
            ),
            context_compaction_percent=_bounded_integer(
                source,
                DURABLE_LOOP_CONTEXT_COMPACTION_PERCENT_ENV,
                _DEFAULT_CONTEXT_COMPACTION_PERCENT,
                minimum=25,
                maximum=90,
            ),
            max_parallel_reads=_bounded_integer(
                source,
                DURABLE_LOOP_MAX_PARALLEL_READS_ENV,
                _DEFAULT_MAX_PARALLEL_READS,
                minimum=1,
                maximum=32,
            ),
            continue_as_new_checkpoints=_bounded_integer(
                source,
                DURABLE_LOOP_CONTINUE_AS_NEW_CHECKPOINTS_ENV,
                _DEFAULT_CONTINUE_AS_NEW_CHECKPOINTS,
                minimum=1,
                maximum=100,
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        """Validate relationships between independently bounded settings."""
        if self.local_tool_timeout_seconds > self.activity_timeout_seconds:
            raise DurableLoopConfigurationError(
                f"{DURABLE_LOOP_LOCAL_TOOL_TIMEOUT_SECONDS_ENV} must not exceed "
                f"{DURABLE_LOOP_ACTIVITY_TIMEOUT_SECONDS_ENV}."
            )
        if self.poll_initial_seconds > self.poll_max_seconds:
            raise DurableLoopConfigurationError(
                f"{DURABLE_LOOP_POLL_INITIAL_SECONDS_ENV} must not exceed "
                f"{DURABLE_LOOP_POLL_MAX_SECONDS_ENV}."
            )
        if self.human_wait_seconds > self.max_run_wait_seconds:
            raise DurableLoopConfigurationError(
                f"{DURABLE_LOOP_HUMAN_WAIT_SECONDS_ENV} must not exceed "
                f"{DURABLE_LOOP_MAX_RUN_WAIT_SECONDS_ENV}."
            )
        if self.max_argument_bytes > self.max_external_content_bytes:
            raise DurableLoopConfigurationError(
                f"{DURABLE_LOOP_MAX_ARGUMENT_BYTES_ENV} must not exceed "
                f"{DURABLE_LOOP_MAX_EXTERNAL_CONTENT_BYTES_ENV}."
            )
        if self.max_result_bytes > self.max_external_content_bytes:
            raise DurableLoopConfigurationError(
                f"{DURABLE_LOOP_MAX_RESULT_BYTES_ENV} must not exceed "
                f"{DURABLE_LOOP_MAX_EXTERNAL_CONTENT_BYTES_ENV}."
            )
        if self.max_cost_microunits > 0 and (
            self.input_cost_microunits_per_million_tokens <= 0
            or self.output_cost_microunits_per_million_tokens <= 0
        ):
            raise DurableLoopConfigurationError(
                f"{DURABLE_LOOP_MAX_COST_MICROUNITS_ENV} requires positive "
                f"{DURABLE_LOOP_INPUT_COST_RATE_ENV} and "
                f"{DURABLE_LOOP_OUTPUT_COST_RATE_ENV}."
            )


def durable_loop_enabled(environment: Mapping[str, str] | None = None) -> bool:
    """Return whether either private durable-loop gate is explicitly enabled."""
    return DurableLoopSettings.from_environment(environment) is not None


def validate_durable_loop_application(
    global_config: GlobalConfig,
    resolved_agents: Sequence[ResolvedAgent],
) -> None:
    """Reject surfaces not implemented by the private foundation layer."""
    if not durable_loop_enabled():
        return
    if global_config.session_runtime is not None:
        raise DurableLoopConfigurationError(
            "The durable agent-loop foundation cannot be combined with session_runtime."
        )
    main_agents = [resolved for resolved in resolved_agents if resolved.is_main]
    if len(main_agents) != 1 or not main_agents[0].builtin_endpoints.chat_api:
        raise DurableLoopConfigurationError(
            "The durable agent-loop foundation requires one main agent with "
            "builtin_endpoints.chat_api enabled."
        )
    for resolved in resolved_agents:
        source = Path(resolved.source_file or "<unknown>").name
        if resolved.trigger is not None:
            raise DurableLoopConfigurationError(
                f"{source}: declared triggers are not supported by the durable-loop foundation."
            )
        if resolved.sandbox_config is not None:
            raise DurableLoopConfigurationError(
                f"{source}: Dynamic Sessions are not supported by the durable-loop foundation."
            )
        if resolved.subagents:
            raise DurableLoopConfigurationError(
                f"{source}: subagents are not supported by the durable-loop foundation."
            )
        if resolved.workflows is not None and resolved.workflows.enabled:
            raise DurableLoopConfigurationError(
                f"{source}: Dynamic Workflows and the durable-loop foundation are "
                "separate private engines and cannot be enabled together."
            )
        if resolved.enabled_skills_names:
            raise DurableLoopConfigurationError(
                f"{source}: executable skills are not supported by the durable-loop foundation."
            )


def _optional_text(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "")
    if not isinstance(value, str):
        raise DurableLoopConfigurationError(f"{name} must be a string.")
    return value.strip()


def _optional_bool(environment: Mapping[str, str], name: str) -> bool:
    raw = _optional_text(environment, name)
    if not raw:
        return False
    normalized = raw.casefold()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise DurableLoopConfigurationError(
        f"{name} must be one of: {sorted(_TRUE_VALUES | _FALSE_VALUES)}."
    )


def _bounded_integer(
    environment: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = _optional_text(environment, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise DurableLoopConfigurationError(f"{name} must be an integer.") from exc
    if value < minimum or value > maximum:
        raise DurableLoopConfigurationError(
            f"{name} must be between {minimum} and {maximum}."
        )
    return value


def _bounded_float(
    environment: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = _optional_text(environment, name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise DurableLoopConfigurationError(f"{name} must be a number.") from exc
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise DurableLoopConfigurationError(
            f"{name} must be finite and between {minimum} and {maximum}."
        )
    return value
