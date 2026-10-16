"""Selection and capability checks for the bounded local Copilot preview."""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import partial
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING

from ..._tool_descriptor import ToolDescriptor, ToolInput, describe_tools
from ...config.env import EnvVar, raw_env_value
from ...config.schema import AgentConfiguration, ResolvedAgent
from ...registration.capabilities import validate_runtime_tool_name_collisions
from ...system_tools._sandbox_endpoint import sandbox_runtime_tool_enabled
from .._harness_binding import AppHarness, HarnessKind, UnsupportedCapabilityError
from .._provider_config import resolve_inference_target

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


def select_copilot_harness(root: Path) -> AppHarness:
    """Freeze provider and storage settings without acquiring runtime resources."""
    from ._copilot_providers import _PROVIDERS
    from ._copilot_runner import CopilotRunner
    from ._copilot_session_identity import resolve_route

    check_sdk_dependency()
    try:
        target = resolve_inference_target(None)
        provider, model = target.require()
    except RuntimeError:
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
        partial(
            CopilotRunner,
            storage_root=route.local_dir / "copilot-preview",
            default_model=model,
            provider=copilot_provider,
            session_storage=route,
        ),
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
        max_output_tokens=configuration.max_output_tokens is not None,
    )


def prepare_tools(tools: Iterable[ToolInput]) -> tuple[ToolDescriptor, ...]:
    """Preserve existing schemas and invocation without silently dropping policy."""
    prepared = describe_tools(tools)
    names: set[str] = set()
    for function in prepared:
        if function.policy.approval_mode != "never_require":
            raise UnsupportedCapabilityError(
                "Copilot preview supports ordinary runtime @tool declarations only when "
                'approval_mode="never_require".'
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

    validate_configuration(resolved.agent_configuration)
    if not (resolved.model or harness.default_model):
        raise UnsupportedCapabilityError("Copilot preview requires an explicit model.")
    authored_tools = [
        *list(capabilities.filtered_user_tools or []),
        *list(capabilities.web_request_tools or []),
    ]
    builtin_endpoints = resolved.builtin_endpoints
    has_direct_surface = resolved.trigger is not None or bool(
        builtin_endpoints.debug_chat_ui or builtin_endpoints.chat_api or builtin_endpoints.mcp
    )
    if has_direct_surface:
        try:
            validate_runtime_tool_name_collisions(
                resolved,
                authored_tools=capabilities.filtered_user_tools or (),
                workflow_enabled=bool(resolved.workflows is not None and resolved.workflows.enabled),
                sandbox_enabled=sandbox_runtime_tool_enabled(
                    resolved.sandbox_config,
                    tools_disabled=resolved.tools_disabled,
                ),
            )
        except ValueError as exc:
            raise UnsupportedCapabilityError(str(exc)) from exc
    prepare_tools(authored_tools)
