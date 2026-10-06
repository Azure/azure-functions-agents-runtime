"""Selection and capability checks for the bounded local Copilot preview."""

from __future__ import annotations

import re
from collections.abc import Iterable
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING

from ..._tool_descriptor import ToolDescriptor, ToolInput, describe_tools
from ...client_manager import (
    ProviderKind,
    _is_active_client_manager_builtin,
    _resolve_builtin_inference_target,
)
from ...config.env import EnvVar, raw_env_value
from ...config.schema import HTTP_TRIGGER_TYPE, AgentConfiguration, ResolvedAgent
from .._harness_binding import AppHarness, HarnessKind, UnsupportedCapabilityError

if TYPE_CHECKING:
    from ...registration.capabilities import AgentCapabilities

FLAG = EnvVar.ENABLE_COPILOT
SDK_DISTRIBUTION = "github-copilot-sdk"
PROVIDER_ENV = EnvVar.PROVIDER
WORKER_COUNT_ENV = EnvVar.FUNCTIONS_WORKER_PROCESS_COUNT


class CopilotPreviewError(RuntimeError):
    """A safe, actionable preview error without underlying SDK details."""


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


def select_copilot_harness(root: Path) -> AppHarness:
    """Freeze provider and storage settings without acquiring runtime resources."""
    from ._copilot_providers import _PROVIDERS
    from ._copilot_session_identity import resolve_route

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
    copilot_provider = _PROVIDERS[provider].from_environment()
    worker_count = raw_env_value(EnvVar.FUNCTIONS_WORKER_PROCESS_COUNT)
    if (worker_count if worker_count is not None else "1").strip() != "1":
        raise UnsupportedCapabilityError(f"Copilot local preview requires {WORKER_COUNT_ENV}=1.")
    if raw_env_value(EnvVar.WEBSITE_INSTANCE_ID):
        raise UnsupportedCapabilityError(
            "Copilot preview supports local execution only; Azure hosting is not qualified."
        )
    for name in (EnvVar.REASONING_EFFORT, EnvVar.REASONING_SUMMARY):
        if raw_env_value(name):
            raise UnsupportedCapabilityError(f"Copilot preview does not support {name}.")
    route = resolve_route(root)
    return AppHarness(
        HarnessKind.COPILOT,
        root,
        route.local_dir / "copilot-preview",
        target.model,
        copilot_provider,
        session_storage=route,
    )


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
    """Preserve existing schemas and invocation without silently dropping policy."""
    prepared = describe_tools(tools)
    names: set[str] = set()
    for function in prepared:
        if function.policy.approval_mode != "never_require" or function.maf_options:
            raise UnsupportedCapabilityError(
                "Copilot preview supports ordinary runtime @tool declarations only; "
                "MAF-specific options, approval, invocation limits, and result parsers "
                "are not supported."
            )
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", function.name):
            raise UnsupportedCapabilityError("Copilot preview requires OpenAI-compatible tool names.")
        if function.name in names:
            raise UnsupportedCapabilityError("Copilot preview requires unique custom tool names.")
        names.add(function.name)
    return prepared


def validate_copilot_agent(
    harness: AppHarness, resolved: ResolvedAgent, capabilities: AgentCapabilities
) -> None:
    """Reject unsupported effective configuration before registration or execution."""
    from ...registration.capabilities import SANDBOX_TOOL_NAME

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
