"""Agent-bound inputs for execution backend construction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..config.schema import SubagentRef
    from ..registration.catalog import AgentCatalog


@dataclass(frozen=True)
class AgentBinding:
    """Non-serializable, agent-specific runner inputs held by one backend."""

    instructions: str | None = None
    tools: list[Any] | None = None
    mcp_tools: list[Any] | None = None
    skill_paths: list[Path] | None = None
    model: str | None = None
    sandbox_tools: list[Any] | None = None
    system_addendum: str | None = None
    workflow_enabled: bool = False
    workflow_durable_client: Any | None = None
    agent_name: str | None = None
    display_name: str | None = None
    web_request_tools: list[Any] | None = None
    subagents: list[SubagentRef] | None = None
    catalog: AgentCatalog | None = None

    def runner_kwargs(self, *, stream: bool) -> dict[str, Any]:
        """Return this binding in the runner's current keyword shape."""
        kwargs: dict[str, Any] = {
            "instructions": self.instructions,
            "tools": self.tools,
            "mcp_tools": self.mcp_tools,
            "skill_paths": self.skill_paths,
            "model": self.model,
            "sandbox_tools": self.sandbox_tools,
            "system_addendum": self.system_addendum,
            "workflow_enabled": self.workflow_enabled,
            "workflow_durable_client": self.workflow_durable_client,
            "agent_name": self.agent_name,
            "web_request_tools": self.web_request_tools,
            "subagents": self.subagents,
            "catalog": self.catalog,
        }
        if stream:
            kwargs["display_name"] = self.display_name
        return kwargs
