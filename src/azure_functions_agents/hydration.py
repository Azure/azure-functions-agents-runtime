"""Blueprint hydration for smart agent bindings."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any

from agent_framework import Agent, AgentSession

from ._observability import FaultDomain, LifecycleStage, start_span
from .client_manager import get_client_manager
from .composition import BindingAgentEntry
from .config.env import runtime_env_value
from .config.merge import DEFAULT_TIMEOUT
from .config.schema import WebRequestConfig
from .runner import (
    _build_chat_options_from_environment,
    _build_history_provider,
    _build_role_agent,
    _session_lock_bounded_by,
    _validate_session_id,
)
from .system_tools.sandbox import create_sandbox_tools
from .system_tools.web_request import create_web_request_tools


@dataclass(frozen=True)
class InvocationMetadata:
    function_name: str | None = None
    invocation_id: str | None = None
    durable_instance_id: str | None = None


@dataclass(frozen=True)
class AgentBlueprint:
    """Immutable app-owned recipe for constructing invocation-scoped Agents."""

    entry: BindingAgentEntry

    @property
    def slug(self) -> str:
        return self.entry.definition.slug

    @property
    def timeout(self) -> float:
        if self.entry.config.timeout is not None:
            return self.entry.config.timeout
        raw_timeout = runtime_env_value("AZURE_FUNCTIONS_AGENTS_TIMEOUT_SECONDS")
        if raw_timeout:
            try:
                return float(raw_timeout)
            except ValueError:
                pass
        return DEFAULT_TIMEOUT

    def build(
        self,
        invocation: InvocationMetadata | None = None,
        *,
        enable_persistent_history: bool = True,
    ) -> Agent[Any]:
        config = self.entry.config
        chat_client, _ = get_client_manager().build_chat_client_with_target(config.model)
        excluded = set(config.tools.exclude) if config.tools is not None else set()
        user_tools = [
            tool
            for tool in self.entry.discovery.user_tools
            if str(getattr(tool, "name", "") or "") not in excluded
        ]
        web_request_config = config.system_tools.web_request if config.system_tools else None
        web_request_tools: list[Any] = []
        if web_request_config is not False:
            web_request_tools = create_web_request_tools(
                web_request_config
                if isinstance(web_request_config, WebRequestConfig)
                else WebRequestConfig()
            )

        sandbox_tools: list[Any] = []
        sandbox = (
            config.system_tools.dynamic_sessions_code_interpreter
            if config.system_tools
            else None
        )
        if sandbox is not None:
            sandbox_tools = create_sandbox_tools(
                sandbox.model_dump(),
                fallback_session_id=invocation.invocation_id if invocation else None,
            )

        return _build_role_agent(
            chat_client,
            instructions=self.entry.definition.instructions,
            tools=user_tools,
            mcp_tools=[
                definition.build_tool()
                for _, definition in self.entry.discovery.mcp_servers
            ],
            skill_paths=[path for _, path in self.entry.discovery.skills],
            sandbox_tools=sandbox_tools,
            web_request_tools=web_request_tools,
            system_addendum=None,
            workflow_enabled=False,
            workflow_durable_client=None,
            agent_name=self.slug,
            resolved_id=invocation.invocation_id if invocation else None,
            history_provider=(
                _build_history_provider() if enable_persistent_history else None
            ),
            delegate_tools=None,
        )


async def _enter_agent(owner: Agent[Any]) -> Agent[Any]:
    try:
        return await owner.__aenter__()
    except BaseException as exc:
        with suppress(Exception):
            await owner.__aexit__(type(exc), exc, exc.__traceback__)
        raise


@asynccontextmanager
async def open_agent(
    blueprint: AgentBlueprint,
    invocation: InvocationMetadata | None = None,
    *,
    enable_persistent_history: bool = True,
) -> AsyncIterator[Agent[Any]]:
    """Build, enter, yield, and always close one invocation-owned Agent."""
    if enable_persistent_history:
        owner = blueprint.build(invocation)
    else:
        owner = blueprint.build(invocation, enable_persistent_history=False)
    entered = await _enter_agent(owner)
    try:
        yield entered
    except BaseException as exc:
        await owner.__aexit__(type(exc), exc, exc.__traceback__)
        raise
    else:
        await owner.__aexit__(None, None, None)


def _session(session_id: str | None) -> AgentSession:
    validated = _validate_session_id(session_id)
    return AgentSession(session_id=validated) if validated is not None else AgentSession()


async def _run_managed(
    agent: Agent[Any],
    blueprint: AgentBlueprint,
    messages: Any = None,
    *,
    session_id: str | None = None,
    options: Mapping[str, Any] | None = None,
    invocation: InvocationMetadata | None = None,
) -> Any:
    session = _session(session_id)
    resolved_session_id = session.session_id
    timeout = blueprint.timeout
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    attributes = {
        "gen_ai.agent.name": blueprint.slug,
        "gen_ai.request.model": blueprint.entry.config.model,
        "faas.name": invocation.function_name if invocation else None,
        "faas.invocation_id": invocation.invocation_id if invocation else None,
        "durable.instance_id": invocation.durable_instance_id if invocation else None,
    }
    with start_span(
        f"agent.binding.run {blueprint.slug}",
        fault_domain=FaultDomain.RUNTIME,
        lifecycle_stage=LifecycleStage.AGENT_RUN,
        attributes=attributes,
    ) as span:
        try:
            async with _session_lock_bounded_by(resolved_session_id, deadline):
                remaining = max(0.0, deadline - loop.time())
                if remaining <= 0:
                    raise TimeoutError
                response = await asyncio.wait_for(
                    agent.run(
                        messages,
                        session=session,
                        options=options if options is not None else _build_chat_options_from_environment(),
                    ),
                    timeout=remaining,
                )
        except asyncio.CancelledError:
            span.set_attribute("af.binding.outcome", "cancelled")
            raise
        except TimeoutError:
            span.set_attribute("af.binding.outcome", "timeout")
            raise RuntimeError(f"Agent run timed out after {timeout}s") from None
        except BaseException:
            span.set_attribute("af.binding.outcome", "error")
            raise
        span.set_attribute("af.binding.outcome", "success")
        return response


async def run_blueprint(
    blueprint: AgentBlueprint,
    messages: Any = None,
    *,
    session_id: str | None = None,
    options: Mapping[str, Any] | None = None,
    invocation: InvocationMetadata | None = None,
    enable_persistent_history: bool = True,
) -> Any:
    """Hydrate one Agent, perform one runtime-managed call, and close it."""
    async with open_agent(
        blueprint,
        invocation,
        enable_persistent_history=enable_persistent_history,
    ) as agent:
        return await _run_managed(
            agent,
            blueprint,
            messages,
            session_id=session_id,
            options=options,
            invocation=invocation,
        )
