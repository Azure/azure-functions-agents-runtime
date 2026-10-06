"""Catalog-backed facade for invoking a hosted agent from application code."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

from ._observability import FaultDomain, LifecycleStage, current_span, start_span
from ._session_id import validate_session_id
from .harness._harness_binding import AppHarness
from .registration._handlers import (
    _set_run_result_attributes,
    build_sandbox_tools_for_session,
)
from .registration.catalog import CatalogEntry
from .response_contract import (
    HostedSkillResponseError,
    response_format_instructions,
    validate_response_contract,
)
from .runner import AgentResult, run_agent, run_agent_events
from .streaming_events import HostedSkillEvent, HostedSkillEventKind


def _validate_prompt(prompt: str) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a nonblank string")
    return prompt


def _resolve_session_id(session_id: str | None) -> tuple[str, bool]:
    validated = validate_session_id(session_id)
    return (validated or uuid.uuid4().hex, validated is None)


@dataclass(frozen=True)
class HostedSkill:
    """Invoke one composed agent without exposing its underlying harness."""

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
        effective_prompt = self._effective_prompt(_validate_prompt(prompt))
        resolved_session_id, is_new_session = _resolve_session_id(session_id)
        with start_span(
            f"agent.run {resolved.slug}",
            lifecycle_stage=LifecycleStage.AGENT_RUN,
            attributes={
                "af.agent.name": resolved.slug,
                "af.agent.display_name": resolved.name,
                "af.agent.execution_surface": "hosted_skill",
                "af.agent.session_id": resolved_session_id,
                "af.agent.model": resolved.model,
            },
        ) as span:
            try:
                result = await run_agent(
                    effective_prompt,
                    instructions=resolved.instructions,
                    timeout=resolved.timeout,
                    tools=list(capabilities.filtered_user_tools or []),
                    mcp_tools=list(capabilities.filtered_mcp_tools or []),
                    skill_paths=list(capabilities.enabled_skill_paths),
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
                if resolved.response_example or resolved.response_schema:
                    validate_response_contract(result.content, resolved.response_schema)
                _set_run_result_attributes(span, result)
                span.set_attribute("af.agent.outcome", "success")
                return result
            except Exception:
                span.set_attribute("af.agent.outcome", "error")
                raise

    async def stream(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
    ) -> AsyncIterator[HostedSkillEvent]:
        """Stream one agent turn as harness-neutral events."""
        resolved = self._entry.resolved
        capabilities = self._entry.capabilities
        effective_prompt = self._effective_prompt(_validate_prompt(prompt))
        resolved_session_id, _ = _resolve_session_id(session_id)
        events = run_agent_events(
            effective_prompt,
            instructions=resolved.instructions,
            timeout=resolved.timeout,
            tools=list(capabilities.filtered_user_tools or []),
            mcp_tools=list(capabilities.filtered_mcp_tools or []),
            skill_paths=list(capabilities.enabled_skill_paths),
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
            _execution_surface="hosted_skill",
        )
        response_parts: list[str] = []
        try:
            async for event in events:
                if event.kind in {
                    HostedSkillEventKind.DELTA,
                    HostedSkillEventKind.MESSAGE,
                } and event.content:
                    response_parts.append(event.content)
                if event.kind is HostedSkillEventKind.DONE and (
                    resolved.response_example or resolved.response_schema
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
                        yield HostedSkillEvent(
                            HostedSkillEventKind.ERROR,
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