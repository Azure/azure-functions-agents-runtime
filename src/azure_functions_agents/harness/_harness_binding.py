"""Private once-per-app harness binding and shared request contracts."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .._logger import logger
from .._tool_descriptor import ToolDescriptor
from ..client_manager import ProviderKind as ProviderKind
from ..config.env import EnvVar, raw_env_value
from ..config.paths import get_app_root
from ..discovery.mcp import MCPServerDescriptor
from ..discovery.skills import SkillDescriptor

if TYPE_CHECKING:
    from ..config.schema import ResolvedAgent
    from ..registration.capabilities import AgentCapabilities
    from ._agent_runner import AgentRunner
    from .copilot_sdk._copilot_providers import CopilotProvider
    from .copilot_sdk._copilot_runtime import CopilotRuntime
    from .copilot_sdk._copilot_session_identity import StorageRoute

FLAG = EnvVar.ENABLE_COPILOT

type ExecutionRole = Literal["primary", "delegate", "workflow_subagent"]


class HarnessKind(StrEnum):
    MAF = "maf"
    COPILOT = "copilot"


class UnsupportedCapabilityError(ValueError):
    """The preview cannot preserve a requested capability's semantics."""


@dataclass
class _HarnessResources:
    guard: threading.Lock = field(default_factory=threading.Lock)
    runner: AgentRunner | None = None
    runtime: CopilotRuntime | None = None


@dataclass(frozen=True, eq=False)
class AppHarness:
    name: HarnessKind
    app_root: Path
    storage_root: Path | None = None
    default_model: str | None = None
    provider: CopilotProvider | None = None
    session_storage: StorageRoute | None = field(default=None, repr=False)
    _resources: _HarnessResources = field(
        default_factory=_HarnessResources, init=False, repr=False, compare=False
    )


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
    if harness.name is HarnessKind.MAF:
        return
    from .copilot_sdk._copilot_preview import validate_copilot_agent

    validate_copilot_agent(harness, resolved, capabilities)


def bind_harness(resolved: ResolvedAgent, capabilities: AgentCapabilities) -> AppHarness:
    """Validate a direct registration once; reuse the binding for every request."""
    if capabilities._harness is not None:
        return capabilities._harness
    harness = get_harness()
    validate_agent(harness, resolved, capabilities)
    return harness
