"""Shared contracts and policies for harness-prepared inputs."""

from typing import Protocol

from .._tool_descriptor import ToolDescriptor


class PreparedToolInputs(Protocol):
    """Common tool categories prepared consistently by every agent harness."""

    @property
    def user_tools(self) -> tuple[ToolDescriptor, ...]:
        """Tools authored by the application developer."""
        ...

    @property
    def sandbox_tools(self) -> tuple[ToolDescriptor, ...]:
        """Tools that run work in an isolated sandbox environment."""
        ...

    @property
    def web_request_tools(self) -> tuple[ToolDescriptor, ...]:
        """Tools that make runtime-managed web requests."""
        ...

    @property
    def workflow_tools(self) -> tuple[ToolDescriptor, ...]:
        """Tools that let an agent manage its durable workflow."""
        ...

    @property
    def delegate_tools(self) -> tuple[ToolDescriptor, ...]:
        """Tools that delegate work to another specialized agent."""
        ...


def prepared_runtime_tools(
    prepared: PreparedToolInputs,
) -> tuple[ToolDescriptor, ...]:
    """Combine shared tools in the order presented to each agent harness.

    Delegate tools remain separate because each harness combines them with
    provider-specific capabilities differently.
    """
    return (
        *prepared.user_tools,
        *prepared.sandbox_tools,
        *prepared.web_request_tools,
        *prepared.workflow_tools,
    )


def compose_instructions(
    instructions: str | None,
    system_addendum: str | None,
) -> str | None:
    """Combine authored instructions with runtime-provided guidance."""
    effective = instructions.strip() if instructions and instructions.strip() else None
    if system_addendum:
        effective = (effective or "") + system_addendum
    return effective
