"""Public SDK configuration and permissions for filtered Copilot capabilities."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from ._harness import CopilotPreviewError
from ._logger import logger
from ._mcp_auth import materialize_mcp_headers

if TYPE_CHECKING:
    from copilot.session import MCPServerConfig, PermissionInvocation, PermissionRequestResult
    from copilot.session_events import PermissionRequest

    from ._harness import HarnessRequest
    from ._skill_policy import SkillPolicy
    from .discovery.mcp import MCPServerDescriptor
    from .discovery.skills import SkillDescriptor


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


def skill_instructions(
    instructions: str | None, skills: tuple[SkillDescriptor, ...]
) -> str:
    """Project the validated catalog when replace mode omits native skill names."""
    if not skills:
        return instructions or ""
    catalog = "\n".join(
        f"- {skill.name}: {' '.join(skill.description.splitlines())}" for skill in skills
    )
    return "\n\n".join(
        part
        for part in (
            instructions or "",
            f"Available skills (load by name with the skill tool):\n{catalog}",
        )
        if part
    )


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
