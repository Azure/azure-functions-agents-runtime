"""Private once-per-app harness binding and shared request contracts."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, TypedDict

from .._logger import logger
from .._observability import (
    RuntimeSpan,
    TelemetryAttributeValue,
)
from .._observability import (
    agent_run_attributes as common_agent_run_attributes,
)
from .._observability import (
    agent_run_span_name as common_agent_run_span_name,
)
from .._observability import (
    set_agent_session_attribute as common_set_agent_session_attribute,
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


class HarnessAdapter(Protocol):
    @property
    def default_model(self) -> str | None:
        """Return the selected harness default model, if one is known."""

    @property
    def auto_record_exceptions(self) -> bool:
        """Return whether span context managers should record exceptions automatically."""

    @property
    def include_raw_tool_calls_in_sensitive_logs(self) -> bool:
        """Return whether sensitive run logs may include full tool-call records."""

    @property
    def session_history_unavailable_message(self) -> str | None:
        """Return the neutral transcript-replay rejection, if unsupported."""

    def create_runner(self, harness: AppHarness) -> AgentRunner:
        """Create the selected harness runner."""

    def validate_agent(
        self, harness: AppHarness, resolved: ResolvedAgent, capabilities: AgentCapabilities
    ) -> None:
        """Validate one resolved agent before registration side effects."""

    def new_binding(self) -> HarnessAdapter:
        """Return adapter state for a distinct app binding."""


class MafHarnessAdapter:
    @property
    def default_model(self) -> str | None:
        return None

    @property
    def auto_record_exceptions(self) -> bool:
        return True

    @property
    def include_raw_tool_calls_in_sensitive_logs(self) -> bool:
        return True

    @property
    def session_history_unavailable_message(self) -> str | None:
        return None

    def create_runner(self, harness: AppHarness) -> AgentRunner:
        from .agent_framework._maf_runner import create_runner

        return create_runner(harness)

    def validate_agent(
        self, harness: AppHarness, resolved: ResolvedAgent, capabilities: AgentCapabilities
    ) -> None:
        del harness, resolved, capabilities

    def new_binding(self) -> HarnessAdapter:
        return MafHarnessAdapter()


@dataclass
class _HarnessResources:
    guard: threading.Lock = field(default_factory=threading.Lock)
    runner: AgentRunner | None = None


@dataclass(frozen=True, eq=False)
class AppHarness:
    name: HarnessKind
    app_root: Path
    adapter: HarnessAdapter = field(default_factory=MafHarnessAdapter, repr=False, compare=False)
    _resources: _HarnessResources = field(
        default_factory=_HarnessResources, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "adapter", self.adapter.new_binding())

    @property
    def default_model(self) -> str | None:
        """Return the selected harness default model, if one is known."""
        return self.adapter.default_model


@dataclass(frozen=True)
class HarnessRequest:
    """SDK-free capability and execution inputs for direct and streaming turns."""

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


def agent_run_span_name_for_harness(harness: AppHarness, *, agent_name: str | None) -> str:
    """Return the runtime-owned invocation span name for the selected harness."""
    del harness
    return common_agent_run_span_name(agent_name=agent_name)


def agent_run_attributes_for_harness(
    harness: AppHarness,
    *,
    agent_name: str | None,
    display_name: str | None,
    trigger_type: str | None,
    session_id: str | None,
    model: str | None,
) -> dict[str, TelemetryAttributeValue]:
    """Return runtime-owned invocation attributes for the selected harness."""
    return common_agent_run_attributes(
        agent_name=agent_name,
        display_name=display_name,
        trigger_type=trigger_type,
        session_id=session_id,
        model=model,
        harness=harness.name.value,
    )


def set_agent_session_attribute_for_harness(
    harness: AppHarness,
    span: RuntimeSpan,
    *,
    session_id: str | None,
) -> None:
    """Set the runtime-owned session/conversation attribute for one invocation span."""
    del harness
    common_set_agent_session_attribute(span, session_id=session_id)


def auto_record_exceptions_for_harness(harness: AppHarness) -> bool:
    """Return whether `start_span()` should rely on automatic exception capture."""
    return harness.adapter.auto_record_exceptions


def include_raw_tool_calls_in_sensitive_logs(harness: AppHarness) -> bool:
    """Return whether run logs may include full tool calls when content capture is enabled."""
    return harness.adapter.include_raw_tool_calls_in_sensitive_logs


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
            selected = AppHarness(HarnessKind.MAF, root, MafHarnessAdapter())
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
    harness.adapter.validate_agent(harness, resolved, capabilities)


def bind_harness(resolved: ResolvedAgent, capabilities: AgentCapabilities) -> AppHarness:
    """Validate a direct registration once; reuse the binding for every request."""
    if capabilities._harness is not None:
        return capabilities._harness
    harness = get_harness()
    validate_agent(harness, resolved, capabilities)
    return harness
