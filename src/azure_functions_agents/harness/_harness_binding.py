"""Private once-per-app harness binding and shared request contracts."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

from .._agent_run_diagnostics import AgentExecutionSurface
from .._logger import logger
from .._observability import TelemetryAttributeValue
from .._observability import (
    agent_run_attributes as common_agent_run_attributes,
)
from .._tool_descriptor import ToolDescriptor
from ..config.env import EnvVar, raw_env_value
from ..config.paths import get_app_root
from ..discovery.mcp import MCPServerDescriptor
from ..discovery.skills import SkillDescriptor
from ._provider_config import ProviderKind as ProviderKind
from ._stream_events import StreamEvent

if TYPE_CHECKING:
    from ..config.schema import ResolvedAgent
    from ..registration.capabilities import AgentCapabilities
    from ._agent_runner import AgentRunner

FLAG = EnvVar.ENABLE_COPILOT

type ExecutionRole = Literal["primary", "delegate", "workflow_subagent"]


class HistoryMessage(TypedDict):
    role: Literal["user", "assistant"]
    text: str


@dataclass(frozen=True)
class SessionHistory:
    messages: tuple[HistoryMessage, ...] = ()
    truncated: bool = False


@dataclass
class HarnessSessionState:
    resumable: bool = False
    caller_supplied: bool = False


class HarnessKind(StrEnum):
    MAF = "maf"
    COPILOT = "copilot"


class UnsupportedCapabilityError(ValueError):
    """The preview cannot preserve a requested capability's semantics."""


class SessionHistoryUnavailableError(ValueError):
    """The selected harness cannot project a transcript for this session."""

    def __init__(self, message: str, *, status_code: int = 501) -> None:
        super().__init__(message)
        self.status_code = status_code


def _create_default_runner(harness: AppHarness) -> AgentRunner:
    from .agent_framework._maf_runner import MafRunner

    return MafRunner(harness)


@dataclass
class _HarnessResources:
    guard: threading.Lock = field(default_factory=threading.Lock)
    runner: AgentRunner | None = None


@dataclass(frozen=True, eq=False)
class AppHarness:
    name: HarnessKind
    app_root: Path
    _create_runner: Callable[[AppHarness], AgentRunner] = field(
        default=_create_default_runner, repr=False, compare=False
    )
    _resources: _HarnessResources = field(
        default_factory=_HarnessResources, init=False, repr=False, compare=False
    )

    @property
    def default_model(self) -> str | None:
        """Return the selected harness default model, if one is known."""
        from ._agent_runner import get_agent_runner

        return get_agent_runner(self).default_model


@dataclass(frozen=True)
class HarnessRequest:
    """Neutral request with runtime descriptors and selected capabilities."""

    prompt: str
    instructions: str | None
    agent_slug: str
    session_id: str
    new_session: bool
    model: str
    tools: tuple[ToolDescriptor, ...]
    max_output_tokens: int | None
    deadline: float
    mcp_servers: tuple[MCPServerDescriptor, ...] = ()
    skills: tuple[SkillDescriptor, ...] = ()
    skill_catalog: tuple[SkillDescriptor, ...] = ()
    execution_role: ExecutionRole = "primary"
    event_sink: Callable[[StreamEvent], None] | None = None
    session_state: HarnessSessionState = field(default_factory=HarnessSessionState)


def agent_run_attributes_for_harness(
    harness: AppHarness,
    *,
    agent_name: str | None,
    display_name: str | None,
    trigger_type: str | None,
    session_id: str | None,
    model: str | None,
    execution_surface: AgentExecutionSurface | None = None,
) -> dict[str, TelemetryAttributeValue]:
    """Return runtime-owned invocation attributes for the selected harness."""
    attributes = common_agent_run_attributes(
        agent_name=agent_name,
        display_name=display_name,
        trigger_type=trigger_type,
        session_id=session_id,
        model=model,
        harness=harness.name.value,
    )
    attributes["af.agent.execution_surface"] = execution_surface
    return attributes


def auto_record_exceptions_for_harness(harness: AppHarness) -> bool:
    """Return whether `start_span()` should rely on automatic exception capture."""
    from ._agent_runner import get_agent_runner

    return get_agent_runner(harness).auto_record_exceptions


def include_raw_tool_calls_in_sensitive_logs(harness: AppHarness) -> bool:
    """Return whether run logs may include full tool calls when content capture is enabled."""
    from ._agent_runner import get_agent_runner

    return get_agent_runner(harness).include_raw_tool_calls_in_sensitive_logs


_HARNESSES: dict[Path, AppHarness] = {}
_SELECTION_LOCK = threading.Lock()


def _flag_enabled(value: str | None) -> bool:
    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized in {"false", "0"}:
        return False
    if normalized in {"true", "1"}:
        return True
    raise ValueError(f"{FLAG} must be true, false, 1, or 0 (or unset).")


def get_harness(app_root: Path | None = None, *, new_app: bool = False) -> AppHarness:
    """Capture each app independently; standalone calls retain a first-use root default."""
    root = (app_root or get_app_root()).resolve()
    with _SELECTION_LOCK:
        existing = _HARNESSES.get(root)
        if existing is not None and not new_app:
            return existing
        if not _flag_enabled(raw_env_value(FLAG)):
            selected = AppHarness(HarnessKind.MAF, root)
        else:
            from .copilot_sdk._copilot_preview import select_copilot_harness

            selected = select_copilot_harness(root)
        if not new_app:
            _HARNESSES[root] = selected
        logger.info("Agent harness selected: harness=%s", selected.name)
        return selected


def validate_agent(
    harness: AppHarness, resolved: ResolvedAgent, capabilities: AgentCapabilities
) -> None:
    """Fail before FunctionApp mutation, native startup, or provider/tool execution."""
    from ._agent_runner import get_agent_runner

    validator = get_agent_runner(harness).validate_agent
    if validator is not None:
        validator(resolved, capabilities)


def bind_harness(resolved: ResolvedAgent, capabilities: AgentCapabilities) -> AppHarness:
    """Validate a direct registration once; reuse the binding for every request."""
    if capabilities._harness is not None:
        return capabilities._harness
    harness = get_harness()
    validate_agent(harness, resolved, capabilities)
    return harness
