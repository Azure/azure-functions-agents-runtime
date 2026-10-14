"""Selection and capability checks for the bounded local Copilot preview."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING

from ..._tool_descriptor import ToolDescriptor, ToolInput, describe_tools
from ...config.env import EnvVar, raw_env_value
from ...config.schema import AgentConfiguration, ResolvedAgent
from ...workflows.tools import build_workflow_tools
from .._harness_binding import AppHarness, HarnessKind, UnsupportedCapabilityError
from .._provider_config import resolve_inference_target
from ._copilot_tool_validation import register_copilot_tool_predicate

if TYPE_CHECKING:
    from ...registration.capabilities import AgentCapabilities
    from .._agent_runner import AgentRunner
    from ._copilot_providers import CopilotProvider
    from ._copilot_runtime import CopilotRuntime
    from ._copilot_session_identity import StorageRoute

FLAG = EnvVar.ENABLE_COPILOT
SDK_DISTRIBUTION = "github-copilot-sdk"
PROVIDER_ENV = EnvVar.PROVIDER
WORKER_COUNT_ENV = EnvVar.FUNCTIONS_WORKER_PROCESS_COUNT


class CopilotPreviewError(RuntimeError):
    """A safe, actionable preview error without underlying SDK details."""


@dataclass
class CopilotHarnessAdapter:
    """Copilot-owned app state behind the neutral harness binding."""

    storage_root: Path
    default_model: str
    provider: CopilotProvider
    session_storage: StorageRoute
    runtime: CopilotRuntime | None = field(default=None, init=False, repr=False)

    @property
    def auto_record_exceptions(self) -> bool:
        return False

    @property
    def include_raw_tool_calls_in_sensitive_logs(self) -> bool:
        return False

    @property
    def session_history_unavailable_message(self) -> str:
        return (
            "Copilot preview does not support transcript replay. Native state is separate "
            "from MAF history."
        )

    def create_runner(self, harness: AppHarness) -> AgentRunner:
        from ._copilot_runner import create_runner

        return create_runner(harness)

    def validate_agent(
        self, harness: AppHarness, resolved: ResolvedAgent, capabilities: AgentCapabilities
    ) -> None:
        validate_copilot_agent(harness, resolved, capabilities)

    def new_binding(self) -> CopilotHarnessAdapter:
        return CopilotHarnessAdapter(
            storage_root=self.storage_root,
            default_model=self.default_model,
            provider=self.provider,
            session_storage=self.session_storage,
        )


def copilot_adapter(harness: AppHarness) -> CopilotHarnessAdapter:
    """Return Copilot-owned state for a selected Copilot harness."""
    if harness.name is not HarnessKind.COPILOT or not isinstance(
        harness.adapter, CopilotHarnessAdapter
    ):
        raise CopilotPreviewError("Copilot was not selected for this app.")
    return harness.adapter


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
    from ._copilot_session_identity import resolve_route

    register_copilot_tool_predicate()
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
        CopilotHarnessAdapter(
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
    from ...registration.capabilities import SANDBOX_TOOL_NAME

    validate_configuration(resolved.agent_configuration)
    if not (resolved.model or harness.default_model):
        raise UnsupportedCapabilityError("Copilot preview requires an explicit model.")
    prepared = prepare_tools(
        [
            *list(capabilities.filtered_user_tools or []),
            *list(capabilities.web_request_tools or []),
        ]
    )
    if resolved.workflows is not None and resolved.workflows.enabled:
        management_names = {function.name for function in build_workflow_tools()}
        if management_names.intersection(function.name for function in prepared):
            raise UnsupportedCapabilityError(
                "Copilot preview workflow management tool names must not collide with authored tools."
            )
    if (
        resolved.sandbox_config is not None
        and not resolved.tools_disabled
        and any(function.name == SANDBOX_TOOL_NAME for function in prepared)
    ):
        raise UnsupportedCapabilityError("Copilot preview requires unique custom tool names.")
