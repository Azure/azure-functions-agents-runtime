"""Catalog-backed facade for invoking a hosted agent from application code."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass

from ._agent_run_diagnostics import _set_run_result_attributes, start_agent_run_span
from ._agent_validation import validate_prompt, validate_session_id
from ._observability import FaultDomain, current_span
from .harness._harness_binding import AppHarness
from .registration.catalog import CatalogEntry
from .response_contract import (
    HostedSkillResponseError,
    response_format_instructions,
    validate_response_contract,
)
from .runner import AgentResult, run_agent, run_agent_events
from .streaming_events import AgentStreamEvent, AgentStreamEventKind
from .system_tools.sandbox import build_sandbox_tools_for_session


def _resolve_session_id(session_id: str | None) -> tuple[str, bool]:
    validated = validate_session_id(session_id)
    return (validated or uuid.uuid4().hex, validated is None)


@dataclass(frozen=True)
class HostedSkill:
    """Invoke one composed agent without exposing its underlying harness.

    Application code obtains this facade through ``@app.hosted_skill`` injection.
    Direct construction with runtime catalog or harness objects is unsupported.
    """

    _entry: CatalogEntry
    _harness: AppHarness

    async def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
    ) -> AgentResult:
        """Run one agent turn and return its result."""
        resolved = self._entry.resolved
        capabilities = self._entry.capabilities
        effective_prompt = self._effective_prompt(validate_prompt(prompt))
        resolved_session_id, is_new_session = _resolve_session_id(session_id)
        response_error: HostedSkillResponseError | None = None
        with start_agent_run_span(
            resolved.slug,
            display_name=resolved.name,
            model=resolved.model,
            execution_surface="hosted_skill",
            session_id=resolved_session_id,
        ) as span:
            try:
                result = await run_agent(
                    effective_prompt,
                    instructions=resolved.instructions,
                    timeout=resolved.timeout,
                    tools=list(capabilities.filtered_user_tools or []),
                    mcp_tools=list(capabilities.filtered_mcp_tools or []),
                    skill_paths=list(capabilities.enabled_skill_paths),
                    skills=list(capabilities.skills),
                    skill_catalog=list(capabilities.skill_catalog),
                    model=resolved.model,
                    session_id=resolved_session_id,
                    sandbox_tools=build_sandbox_tools_for_session(
                        resolved,
                        resolved_session_id,
                    ),
                    web_request_tools=list(capabilities.web_request_tools or []),
                    agent_configuration=resolved.agent_configuration,
                    agent_name=resolved.slug,
                    _harness=self._harness,
                    _session_is_new=is_new_session,
                )
                if resolved.response_example or resolved.response_schema is not None:
                    try:
                        validate_response_contract(result.content, resolved.response_schema)
                    except HostedSkillResponseError as exc:
                        response_error = exc
                        span.set_attribute("af.agent.outcome", "error")
                        span.record_exception(exc, fault_domain=FaultDomain.APP)
                if response_error is None:
                    _set_run_result_attributes(span, result)
                    span.set_attribute("af.agent.outcome", "success")
            except Exception:
                span.set_attribute("af.agent.outcome", "error")
                raise
        if response_error is not None:
            raise response_error
        return result

    async def stream(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
    ) -> AsyncGenerator[AgentStreamEvent]:
        """Stream one agent turn as harness-neutral events."""
        resolved = self._entry.resolved
        capabilities = self._entry.capabilities
        effective_prompt = self._effective_prompt(validate_prompt(prompt))
        resolved_session_id, is_new_session = _resolve_session_id(session_id)
        events = run_agent_events(
            effective_prompt,
            instructions=resolved.instructions,
            timeout=resolved.timeout,
            tools=list(capabilities.filtered_user_tools or []),
            mcp_tools=list(capabilities.filtered_mcp_tools or []),
            skill_paths=list(capabilities.enabled_skill_paths),
            skills=list(capabilities.skills),
            skill_catalog=list(capabilities.skill_catalog),
            model=resolved.model,
            session_id=resolved_session_id,
            sandbox_tools=build_sandbox_tools_for_session(
                resolved,
                resolved_session_id,
            ),
            web_request_tools=list(capabilities.web_request_tools or []),
            agent_configuration=resolved.agent_configuration,
            agent_name=resolved.slug,
            display_name=resolved.name,
            _harness=self._harness,
            _session_is_new=is_new_session,
            _execution_surface="hosted_skill",
        )
        response_parts: list[str] = []
        try:
            async for event in events:
                if event.kind in {
                    AgentStreamEventKind.DELTA,
                    AgentStreamEventKind.MESSAGE,
                } and event.content:
                    response_parts.append(event.content)
                if event.kind is AgentStreamEventKind.DONE and (
                    resolved.response_example or resolved.response_schema is not None
                ):
                    try:
                        validate_response_contract(
                            "".join(response_parts),
                            resolved.response_schema,
                        )
                    except HostedSkillResponseError as exc:
                        span = current_span()
                        span.set_attribute("af.agent.outcome", "error")
                        span.record_exception(exc, fault_domain=FaultDomain.APP)
                        async for _ in events:
                            pass
                        yield AgentStreamEvent(
                            AgentStreamEventKind.ERROR,
                            content="Agent response validation failed",
                        )
                        return
                yield event
        finally:
            await events.aclose()

    def _effective_prompt(self, prompt: str) -> str:
        parts = response_format_instructions(self._entry.resolved)
        parts.append(prompt)
        return "\n\n".join(parts)