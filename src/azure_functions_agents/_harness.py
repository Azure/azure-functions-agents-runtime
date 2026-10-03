"""Private app-level harness selection and the bounded local preview contract."""

from __future__ import annotations

import re
import threading
from collections.abc import Awaitable, Callable, Iterable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ._logger import logger
from ._tool_descriptor import ToolDescriptor, ToolInput, describe_tools
from .client_manager import ProviderKind as ProviderKind
from .client_manager import (
    _is_active_client_manager_builtin,
    _resolve_builtin_inference_target,
)
from .config.env import EnvVar, raw_env_value
from .config.paths import get_app_root
from .config.schema import HTTP_TRIGGER_TYPE, AgentConfiguration, ResolvedAgent
from .discovery.mcp import MCPServerDescriptor
from .discovery.skills import SkillDescriptor

if TYPE_CHECKING:
    from ._copilot_providers import CopilotProvider
    from ._native_session_identity import StorageRoute
    from .registration.capabilities import AgentCapabilities

FLAG = EnvVar.ENABLE_COPILOT
SDK_DISTRIBUTION = "github-copilot-sdk"
PROVIDER_ENV = EnvVar.PROVIDER
WORKER_COUNT_ENV = EnvVar.FUNCTIONS_WORKER_PROCESS_COUNT

type ExecutionRole = Literal["primary", "delegate", "workflow_subagent"]


class HarnessKind(StrEnum):
    MAF = "maf"
    COPILOT = "copilot"


class CopilotPreviewError(RuntimeError):
    """A safe, actionable preview error; never includes underlying SDK details."""


class UnsupportedCapabilityError(ValueError):
    """The preview cannot preserve a requested capability's semantics."""


@dataclass(frozen=True, eq=False)
class AppHarness:
    name: HarnessKind
    app_root: Path
    storage_root: Path | None = None
    default_model: str | None = None
    provider: CopilotProvider | None = None
    session_storage: StorageRoute | None = field(default=None, repr=False)


@dataclass(frozen=True)
class HarnessRequest:
    """Normalized inputs for the supported primary, non-streaming adapter."""

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
    skill_source_paths: tuple[Path, ...] | None = None


_HARNESSES: dict[Path, AppHarness] = {}
_SELECTION_LOCK = threading.Lock()
_SHUTDOWN_CALLBACKS: set[Callable[[], Awaitable[None]]] = set()


def _register_shutdown(callback: Callable[[], Awaitable[None]]) -> None:
    _SHUTDOWN_CALLBACKS.add(callback)


def _unregister_shutdown(callback: Callable[[], Awaitable[None]]) -> None:
    _SHUTDOWN_CALLBACKS.discard(callback)


async def _shutdown_harnesses() -> None:
    """Close only resource owners acquired by selected app paths."""
    async with AsyncExitStack() as cleanup:
        for callback in tuple(_SHUTDOWN_CALLBACKS):
            cleanup.push_async_callback(callback)


def _flag_enabled(value: str | None) -> bool:
    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized in {"false", "0"}:
        return False
    if normalized in {"true", "1"}:
        return True
    raise ValueError(f"{FLAG} must be true, false, 1, or 0 (or unset).")


def check_sdk_dependency() -> None:
    try:
        version(SDK_DISTRIBUTION)
    except PackageNotFoundError:
        raise CopilotPreviewError(
            "Copilot preview requires azurefunctions-agents-runtime[copilot]. "
            f"Install {SDK_DISTRIBUTION} through the optional extra."
        ) from None


def validate_copilot_client_manager() -> None:
    if not _is_active_client_manager_builtin():
        raise UnsupportedCapabilityError(
            "Copilot preview accepts only the runtime-created MAFClientManager singleton. "
            "An explicitly installed ClientManager, including MAFClientManager(), is a MAF-only "
            f"replacement; restart with {FLAG}=false to use it through MAF. "
            "No client or fallback was constructed."
        )


def get_harness(app_root: Path | None = None, *, new_app: bool = False) -> AppHarness:
    """Capture each app independently; standalone calls retain a first-use root default."""
    root = (app_root or get_app_root()).resolve()
    with _SELECTION_LOCK:
        existing = _HARNESSES.get(root)
        if existing is not None and not new_app:
            return existing
        if not _flag_enabled(raw_env_value(EnvVar.ENABLE_COPILOT)):
            selected = AppHarness(HarnessKind.MAF, root)
        else:
            from ._native_session_identity import resolve_route

            validate_copilot_client_manager()
            check_sdk_dependency()
            try:
                target = _resolve_builtin_inference_target(None)
                if target.provider is None:
                    raise ValueError
                provider = ProviderKind(target.provider)
            except (RuntimeError, ValueError):
                raise UnsupportedCapabilityError(
                    f"Copilot preview supports {PROVIDER_ENV}=openai, azure_openai, or foundry "
                    "with the existing explicit-or-autodetected provider settings."
                ) from None
            from ._copilot_providers import _PROVIDERS

            copilot_provider = _PROVIDERS[provider].from_environment()
            worker_count = raw_env_value(EnvVar.FUNCTIONS_WORKER_PROCESS_COUNT)
            if (worker_count if worker_count is not None else "1").strip() != "1":
                raise UnsupportedCapabilityError(
                    f"Copilot local preview requires {WORKER_COUNT_ENV}=1."
                )
            if raw_env_value(EnvVar.WEBSITE_INSTANCE_ID):
                raise UnsupportedCapabilityError(
                    "Copilot preview supports local execution only; Azure hosting is not qualified."
                )
            for name in (EnvVar.REASONING_EFFORT, EnvVar.REASONING_SUMMARY):
                if raw_env_value(name):
                    raise UnsupportedCapabilityError(f"Copilot preview does not support {name}.")
            route = resolve_route(root)
            storage_root = route.local_dir / "copilot-preview"
            selected = AppHarness(
                HarnessKind.COPILOT,
                root,
                storage_root,
                target.model,
                copilot_provider,
                session_storage=route,
            )
        if not new_app:
            _HARNESSES[root] = selected
        logger.info("Agent harness selected: harness=%s", selected.name)
        return selected


def reject_unsupported(**capabilities: bool) -> None:
    unsupported = [name for name, enabled in capabilities.items() if enabled]
    if unsupported:
        raise UnsupportedCapabilityError(
            "Copilot preview does not support: "
            + ", ".join(unsupported)
            + ". Disable these explicitly or restart with "
            + FLAG
            + "=false to use MAF. No fallback was attempted."
        )


def validate_configuration(configuration: AgentConfiguration) -> None:
    reject_unsupported(
        agent_framework=(
            configuration.agent_framework is not None
            and configuration.agent_framework.compaction is not None
            and configuration.agent_framework.compaction.max_context_window_tokens is not None
        ),
        max_output_tokens=configuration.max_output_tokens is not None,
    )


def prepare_tools(tools: Iterable[ToolInput]) -> tuple[ToolDescriptor, ...]:
    """Preserve existing schemas/invocation, rejecting unsupported policy instead of dropping it."""
    prepared = describe_tools(tools)
    names: set[str] = set()
    for function in prepared:
        if function.policy.approval_mode != "never_require" or function.policy.maf_only_options:
            raise UnsupportedCapabilityError(
                "Copilot preview supports simple FunctionTool callables only; custom tool "
                "classes, approval, invocation limits, declaration-only tools, result parsers, "
                "and injected invocation context are not supported."
            )
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", function.name):
            raise UnsupportedCapabilityError("Copilot preview requires OpenAI-compatible tool names.")
        if function.name in names:
            raise UnsupportedCapabilityError("Copilot preview requires unique custom tool names.")
        names.add(function.name)
    return prepared


def validate_agent(
    harness: AppHarness, resolved: ResolvedAgent, capabilities: AgentCapabilities
) -> None:
    """Fail before FunctionApp mutation, native startup, or provider/tool execution."""
    from .registration.capabilities import SANDBOX_TOOL_NAME

    if harness.name is HarnessKind.MAF:
        return
    validate_copilot_client_manager()
    validate_configuration(resolved.agent_configuration)
    reject_unsupported(
        non_http_trigger=resolved.trigger is not None and resolved.trigger.type != HTTP_TRIGGER_TYPE,
        debug_chat_ui=resolved.builtin_endpoints.debug_chat_ui,
        mcp_endpoint=resolved.builtin_endpoints.mcp,
        subagents=bool(resolved.subagents),
        workflows=resolved.workflows is not None and resolved.workflows.enabled,
    )
    if not (resolved.model or harness.default_model):
        raise UnsupportedCapabilityError("Copilot preview requires an explicit model.")
    prepared = prepare_tools(
        [
            *list(capabilities.filtered_user_tools or []),
            *list(capabilities.web_request_tools or []),
        ]
    )
    if (
        resolved.sandbox_config is not None
        and not resolved.tools_disabled
        and any(function.name == SANDBOX_TOOL_NAME for function in prepared)
    ):
        raise UnsupportedCapabilityError("Copilot preview requires unique custom tool names.")


def bind_harness(resolved: ResolvedAgent, capabilities: AgentCapabilities) -> AppHarness:
    """Validate a direct registration once; reuse the binding for every request."""
    if capabilities._harness is not None:
        return capabilities._harness
    harness = get_harness()
    validate_agent(harness, resolved, capabilities)
    return harness
