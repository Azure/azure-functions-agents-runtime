"""Public SDK configuration and permissions for filtered Copilot capabilities."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from ..._logger import logger
from ..._mcp_auth import materialize_mcp_headers
from ..._tool_descriptor import ApprovalMode, ToolDescriptor, ToolInput, describe_tools
from ...config.env import EnvVar
from ...config.schema import ResolvedAgent
from ...workflows.tools import build_workflow_tools
from .._harness_binding import AppHarness, UnsupportedCapabilityError
from ._copilot_runtime import CopilotPreviewError

if TYPE_CHECKING:
    from copilot.session import MCPServerConfig, PermissionInvocation, PermissionRequestResult
    from copilot.session_events import PermissionRequest

    from ..._skill_policy import SkillPolicy
    from ...discovery.mcp import MCPServerDescriptor
    from ...registration.capabilities import AgentCapabilities
    from .._harness_binding import HarnessRequest


FLAG = EnvVar.ENABLE_COPILOT


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

def prepare_tools(tools: Iterable[ToolInput]) -> tuple[ToolDescriptor, ...]:
    """Preserve existing schemas and invocation without silently dropping policy."""
    prepared = describe_tools(tools)
    names: set[str] = set()
    for function in prepared:
        if function.policy.approval_mode is not ApprovalMode.NEVER_REQUIRE:
            raise UnsupportedCapabilityError(
                "Copilot preview supports ordinary runtime @tool declarations only when "
                f'approval_mode="{ApprovalMode.NEVER_REQUIRE.value}".'
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


_SKILL_TOOLS = ("builtin:skill", "builtin:view", "builtin:bash")


def available_tools(request: HarnessRequest) -> list[str]:
    """Allow only configured sources and the approved native skill helpers."""
    selected = [f"custom:{tool.name}" for tool in request.tools]
    if request.mcp_servers:
        # The SDK selector addresses the MCP source, not a guessed server/name prefix.
        selected.append("mcp:*")
    if request.skills:
        selected.extend(_SKILL_TOOLS)
    return selected


async def mcp_configuration(
    descriptors: tuple[MCPServerDescriptor, ...],
    *,
    protect_headers: Callable[[dict[str, str]], None],
) -> dict[str, MCPServerConfig]:
    """Materialize the entire filtered inventory once before create or resume."""
    from copilot.session import MCPHTTPServerConfig

    configured: dict[str, MCPServerConfig] = {}
    for descriptor in descriptors:
        try:
            headers = await asyncio.to_thread(materialize_mcp_headers, descriptor)
        except Exception:
            raise CopilotPreviewError(
                "Copilot MCP authentication failed to acquire a usable token. "
                "Check the configured scope and Azure credential."
            ) from None
        protect_headers(headers)
        configured[descriptor.name] = MCPHTTPServerConfig(
            type="http",
            url=descriptor.url,
            tools=["*"] if descriptor.tools is None else list(descriptor.tools),
            headers=headers,
        )
    return configured


def permission_handler(
    policy: SkillPolicy,
) -> Callable[[PermissionRequest, PermissionInvocation], PermissionRequestResult]:
    """Approve configured MCP calls or explicitly scoped skill-helper actions."""
    from copilot.rpc import PermissionDecisionApproveOnce, PermissionDecisionDeniedByRules
    from copilot.session import PermissionHandler
    from copilot.session_events import PermissionRequestRead, PermissionRequestShell

    def decide(
        request: PermissionRequest, invocation: PermissionInvocation
    ) -> PermissionRequestResult:
        if request.kind == "mcp":
            return PermissionHandler.approve_all(request, invocation)
        allowed = False
        match request:
            case PermissionRequestRead():
                allowed = not request.request_sandbox_bypass and policy.allows_read(request.path)
            case PermissionRequestShell():
                allowed = (
                    not request.has_write_file_redirection
                    and not request.request_sandbox_bypass
                    and not request.request_sandbox_permissive
                    and policy.allows_shell(request.full_command_text)
                )
        if (
            not allowed
            or invocation.get("managed_settings_enabled", False)
            or request.managed_approval_required
        ):
            logger.debug("Copilot permission denied: kind=%s", request.kind)
            return PermissionDecisionDeniedByRules(rules=[])
        return PermissionDecisionApproveOnce()

    return decide
