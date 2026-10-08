from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

import azure_functions_agents.hosted_skill as hosted_skill_module
from azure_functions_agents._observability import FaultDomain
from azure_functions_agents.config.schema import (
    AgentConfiguration,
    BuiltinEndpointsConfig,
    ResolvedAgent,
    ToolsFilter,
)
from azure_functions_agents.discovery.skills import SkillDescriptor
from azure_functions_agents.harness._harness_binding import AppHarness, HarnessKind
from azure_functions_agents.hosted_skill import HostedSkill
from azure_functions_agents.registration.capabilities import AgentCapabilities
from azure_functions_agents.registration.catalog import CatalogEntry
from azure_functions_agents.response_contract import (
    InvalidResponseJsonError,
    ResponseSchemaValidationError,
)
from azure_functions_agents.runner import AgentResult
from azure_functions_agents.streaming_events import (
    HostedSkillEvent,
    HostedSkillEventKind,
)


class _CapturedSpan:
    def __init__(self, attributes: dict[str, Any]) -> None:
        self.attributes = dict(attributes)
        self.exceptions: list[BaseException] = []
        self.fault_domains: list[str | None] = []

    def set_attribute(self, key: str, value: Any) -> None:
        if value is not None:
            self.attributes[key] = value

    def set_content(self, key: str, value: str) -> None:
        pass

    def record_exception(self, exc: BaseException, *, fault_domain: str | None = None) -> None:
        self.exceptions.append(exc)
        self.fault_domains.append(fault_domain)


def _install_start_span_capture(monkeypatch: pytest.MonkeyPatch) -> list[_CapturedSpan]:
    spans: list[_CapturedSpan] = []

    @contextlib.contextmanager
    def fake_start_span(
        _name: str,
        *,
        lifecycle_stage: str | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[_CapturedSpan]:
        span = _CapturedSpan(attributes or {})
        spans.append(span)
        yield span

    monkeypatch.setattr(hosted_skill_module, "start_span", fake_start_span)
    return spans


def _make_skill(
    tmp_path: Path,
    *,
    response_schema: dict[str, Any] | None = None,
) -> tuple[HostedSkill, AgentCapabilities]:
    approved_skill = SkillDescriptor.create(
        name="one",
        path=tmp_path / "skills" / "one",
    )
    excluded_skill = SkillDescriptor.create(
        name="excluded",
        path=tmp_path / "skills" / "excluded",
    )
    resolved = ResolvedAgent(
        name="Internal",
        slug="internal",
        description="Internal skill",
        trigger=None,
        instructions="Follow policy.",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(),
        model="model-one",
        timeout=12.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
        input_schema=None,
        response_schema=response_schema,
        response_example=None,
        source_file=str(tmp_path / "internal.agent.md"),
        agent_configuration=AgentConfiguration(),
    )
    capabilities = AgentCapabilities(
        filtered_user_tools=["tool"],
        filtered_mcp_tools=["mcp"],  # type: ignore[list-item]
        enabled_skill_paths=[tmp_path / "skills" / "one"],
        web_request_tools=["web"],
        skills=(approved_skill,),
        skill_catalog=(approved_skill, excluded_skill),
    )
    harness = AppHarness(HarnessKind.MAF, tmp_path)
    return HostedSkill(CatalogEntry(resolved, capabilities), harness), capabilities


@pytest.mark.asyncio
async def test_run_forwards_catalog_values_with_one_session_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skill, capabilities = _make_skill(tmp_path)
    captured: dict[str, Any] = {}

    def build_sandbox(_resolved: ResolvedAgent, session_id: str) -> list[str]:
        captured["sandbox_session_id"] = session_id
        return ["sandbox"]

    async def run_agent(prompt: str, **kwargs: Any) -> AgentResult:
        captured["prompt"] = prompt
        captured.update(kwargs)
        return AgentResult(kwargs["session_id"], "complete")

    monkeypatch.setattr(hosted_skill_module, "build_sandbox_tools_for_session", build_sandbox)
    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)

    result = await skill.run("Prepare order")

    assert result.session_id == captured["sandbox_session_id"] == captured["session_id"]
    assert captured["prompt"] == "Prepare order"
    assert captured["instructions"] == "Follow policy."
    assert captured["model"] == "model-one"
    assert captured["agent_name"] == "internal"
    assert captured["_session_is_new"] is True
    assert captured["tools"] == capabilities.filtered_user_tools
    assert captured["tools"] is not capabilities.filtered_user_tools
    assert captured["mcp_tools"] is not capabilities.filtered_mcp_tools
    assert captured["skill_paths"] is not capabilities.enabled_skill_paths
    assert captured["skills"] == list(capabilities.skills)
    assert captured["skills"] is not capabilities.skills
    assert captured["skill_catalog"] == list(capabilities.skill_catalog)
    assert captured["skill_catalog"] is not capabilities.skill_catalog
    assert captured["web_request_tools"] is not capabilities.web_request_tools

    captured["tools"].append("mutated")
    assert capabilities.filtered_user_tools == ["tool"]

    await skill.run("Prepare another order")
    assert captured["tools"] == ["tool"]


@pytest.mark.asyncio
async def test_run_records_hosted_skill_span_and_result_attributes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skill, _ = _make_skill(tmp_path)
    spans = _install_start_span_capture(monkeypatch)

    async def run_agent(_prompt: str, **kwargs: Any) -> AgentResult:
        return AgentResult(
            kwargs["session_id"],
            "complete",
            tool_calls=[{"name": "lookup", "result": "ok"}],
        )

    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)

    await skill.run("Prepare order", session_id="session-one")

    [span] = spans
    assert span.attributes["af.agent.name"] == "internal"
    assert span.attributes["af.agent.execution_surface"] == "hosted_skill"
    assert span.attributes["af.agent.session_id"] == "session-one"
    assert span.attributes["af.agent.tool_call_count"] == 1
    assert span.attributes["af.agent.response_bytes"] == len("complete")
    assert span.attributes["af.agent.outcome"] == "success"


@pytest.mark.asyncio
async def test_run_records_failure_once_through_span_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skill, _ = _make_skill(tmp_path)
    span = _CapturedSpan({})
    failure = RuntimeError("run failed")

    @contextlib.contextmanager
    def recording_start_span(*_args: Any, **_kwargs: Any) -> Iterator[_CapturedSpan]:
        try:
            yield span
        except BaseException as exc:
            span.record_exception(exc)
            raise

    async def run_agent(_prompt: str, **_kwargs: Any) -> AgentResult:
        raise failure

    monkeypatch.setattr(hosted_skill_module, "start_span", recording_start_span)
    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)

    with pytest.raises(RuntimeError, match="run failed"):
        await skill.run("Prepare order")

    assert span.attributes["af.agent.outcome"] == "error"
    assert span.exceptions == [failure]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected_error"),
    [
        ("not JSON", InvalidResponseJsonError),
        ('{"wrong": true}', ResponseSchemaValidationError),
    ],
)
async def test_run_raises_response_contract_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    content: str,
    expected_error: type[ValueError],
) -> None:
    skill, _ = _make_skill(
        tmp_path,
        response_schema={
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        },
    )

    async def run_agent(_prompt: str, **kwargs: Any) -> AgentResult:
        return AgentResult(kwargs["session_id"], content)

    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)

    with pytest.raises(expected_error):
        await skill.run("Return JSON")


@pytest.mark.asyncio
async def test_run_empty_response_schema_still_requires_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skill, _ = _make_skill(tmp_path, response_schema={})

    async def run_agent(_prompt: str, **kwargs: Any) -> AgentResult:
        return AgentResult(kwargs["session_id"], "not JSON")

    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)

    with pytest.raises(InvalidResponseJsonError):
        await skill.run("Return JSON")


@pytest.mark.asyncio
async def test_run_records_response_contract_failure_as_app_fault_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skill, _ = _make_skill(
        tmp_path,
        response_schema={
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        },
    )
    span = _CapturedSpan({})

    @contextlib.contextmanager
    def recording_start_span(*_args: Any, **_kwargs: Any) -> Iterator[_CapturedSpan]:
        try:
            yield span
        except BaseException as exc:
            span.record_exception(exc)
            raise

    async def run_agent(_prompt: str, **kwargs: Any) -> AgentResult:
        return AgentResult(kwargs["session_id"], "not JSON")

    monkeypatch.setattr(hosted_skill_module, "start_span", recording_start_span)
    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)

    with pytest.raises(InvalidResponseJsonError) as caught:
        await skill.run("Return JSON")

    assert span.attributes["af.agent.outcome"] == "error"
    assert span.exceptions == [caught.value]
    assert span.fault_domains == [FaultDomain.APP]


@pytest.mark.asyncio
async def test_run_response_contract_returns_original_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skill, _ = _make_skill(
        tmp_path,
        response_schema={
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        },
    )
    result = AgentResult("session-one", '{"ok": true}')

    async def run_agent(_prompt: str, **_kwargs: Any) -> AgentResult:
        return result

    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)

    assert await skill.run("Return JSON", session_id="session-one") is result


@pytest.mark.asyncio
async def test_run_reuses_explicit_session_and_rejects_invalid_input_before_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skill, _ = _make_skill(tmp_path)
    calls = 0

    async def run_agent(_prompt: str, **kwargs: Any) -> AgentResult:
        nonlocal calls
        calls += 1
        return AgentResult(kwargs["session_id"], "complete")

    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)

    result = await skill.run("Continue", session_id="session-one")

    assert result.session_id == "session-one"
    assert calls == 1
    with pytest.raises(ValueError, match="nonblank"):
        await skill.run(" ")
    with pytest.raises(ValueError, match="Invalid session_id"):
        await skill.run("Continue", session_id="../unsafe")
    assert calls == 1


def test_hosted_skill_is_not_callable(tmp_path: Path) -> None:
    skill, _ = _make_skill(tmp_path)

    assert not callable(skill)


@pytest.mark.asyncio
async def test_stream_validates_completed_response_before_done(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skill, _ = _make_skill(
        tmp_path,
        response_schema={
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        },
    )
    closed = False
    completed = False
    generator_exit = False

    async def events() -> AsyncIterator[HostedSkillEvent]:
        nonlocal closed, completed, generator_exit
        try:
            yield HostedSkillEvent(HostedSkillEventKind.SESSION, session_id="session-one")
            yield HostedSkillEvent(HostedSkillEventKind.DELTA, content='{"wrong": true}')
            yield HostedSkillEvent(HostedSkillEventKind.DONE)
            completed = True
        except GeneratorExit:
            generator_exit = True
            raise
        finally:
            closed = True

    monkeypatch.setattr(hosted_skill_module, "run_agent_events", lambda *_a, **_k: events())

    output = [event async for event in skill.stream("Return JSON")]

    assert [event.kind for event in output] == [
        HostedSkillEventKind.SESSION,
        HostedSkillEventKind.DELTA,
        HostedSkillEventKind.ERROR,
    ]
    assert output[-1].content == "Agent response validation failed"
    assert closed is True
    assert completed is True
    assert generator_exit is False


@pytest.mark.asyncio
async def test_stream_empty_response_schema_still_requires_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skill, _ = _make_skill(tmp_path, response_schema={})

    async def events() -> AsyncIterator[HostedSkillEvent]:
        yield HostedSkillEvent(HostedSkillEventKind.SESSION, session_id="session-one")
        yield HostedSkillEvent(HostedSkillEventKind.DELTA, content="not JSON")
        yield HostedSkillEvent(HostedSkillEventKind.DONE)

    monkeypatch.setattr(hosted_skill_module, "run_agent_events", lambda *_a, **_k: events())

    output = [event async for event in skill.stream("Return JSON")]

    assert output[-1] == HostedSkillEvent(
        HostedSkillEventKind.ERROR,
        content="Agent response validation failed",
    )


@pytest.mark.asyncio
async def test_stream_validation_failure_marks_runner_span_as_app_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skill, _ = _make_skill(
        tmp_path,
        response_schema={
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        },
    )
    span = _CapturedSpan({"af.agent.outcome": "success"})

    async def events() -> AsyncIterator[HostedSkillEvent]:
        yield HostedSkillEvent(HostedSkillEventKind.SESSION, session_id="session-one")
        yield HostedSkillEvent(HostedSkillEventKind.DELTA, content='{"wrong": true}')
        yield HostedSkillEvent(HostedSkillEventKind.DONE)

    monkeypatch.setattr(hosted_skill_module, "run_agent_events", lambda *_a, **_k: events())
    monkeypatch.setattr(hosted_skill_module, "current_span", lambda: span, raising=False)

    output = [event async for event in skill.stream("Return JSON")]

    assert output[-1].kind is HostedSkillEventKind.ERROR
    assert span.attributes["af.agent.outcome"] == "error"
    assert len(span.exceptions) == 1
    assert span.fault_domains == ["app"]


@pytest.mark.asyncio
async def test_stream_translates_invalid_response_schema_to_error_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skill, _ = _make_skill(tmp_path, response_schema={"type": 123})

    async def events() -> AsyncIterator[HostedSkillEvent]:
        yield HostedSkillEvent(HostedSkillEventKind.SESSION, session_id="session-one")
        yield HostedSkillEvent(HostedSkillEventKind.DELTA, content='{"message":"ok"}')
        yield HostedSkillEvent(HostedSkillEventKind.DONE)

    monkeypatch.setattr(hosted_skill_module, "run_agent_events", lambda *_a, **_k: events())

    output = [event async for event in skill.stream("Return JSON")]

    assert output[-1] == HostedSkillEvent(
        HostedSkillEventKind.ERROR,
        content="Agent response validation failed",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id", [None, "session-one"])
async def test_stream_binds_sandbox_and_runner_to_same_public_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_id: str | None,
) -> None:
    skill, capabilities = _make_skill(tmp_path)
    captured: dict[str, Any] = {}

    def build_sandbox(_resolved: ResolvedAgent, resolved_id: str) -> list[str]:
        captured["sandbox"] = resolved_id
        return ["sandbox"]

    async def events(_prompt: str, **kwargs: Any) -> AsyncIterator[HostedSkillEvent]:
        captured["runner"] = kwargs["session_id"]
        captured["execution_surface"] = kwargs["_execution_surface"]
        captured["session_is_new"] = kwargs["_session_is_new"]
        captured["display_name"] = kwargs["display_name"]
        captured["skills"] = kwargs["skills"]
        captured["skill_catalog"] = kwargs["skill_catalog"]
        yield HostedSkillEvent(
            HostedSkillEventKind.SESSION,
            session_id=kwargs["session_id"],
        )
        yield HostedSkillEvent(HostedSkillEventKind.DONE)

    monkeypatch.setattr(hosted_skill_module, "build_sandbox_tools_for_session", build_sandbox)
    monkeypatch.setattr(hosted_skill_module, "run_agent_events", events)

    output = [event async for event in skill.stream("Prepare order", session_id=session_id)]

    assert captured["sandbox"] == captured["runner"] == output[0].session_id
    assert captured["execution_surface"] == "hosted_skill"
    assert captured["session_is_new"] is (session_id is None)
    assert captured["display_name"] == "Internal"
    assert captured["skills"] == list(capabilities.skills)
    assert captured["skill_catalog"] == list(capabilities.skill_catalog)
    if session_id is not None:
        assert output[0].session_id == session_id


@pytest.mark.asyncio
async def test_stream_cancellation_propagates_and_closes_core_iterator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skill, _ = _make_skill(tmp_path)
    waiting = asyncio.Event()
    closed = False

    async def events() -> AsyncIterator[HostedSkillEvent]:
        nonlocal closed
        try:
            yield HostedSkillEvent(HostedSkillEventKind.SESSION, session_id="session-one")
            waiting.set()
            await asyncio.Event().wait()
        finally:
            closed = True

    monkeypatch.setattr(hosted_skill_module, "run_agent_events", lambda *_a, **_k: events())
    stream = skill.stream("Prepare order")

    assert (await anext(stream)).kind is HostedSkillEventKind.SESSION
    pending = asyncio.create_task(anext(stream))
    await waiting.wait()
    pending.cancel()

    with pytest.raises(asyncio.CancelledError):
        await pending
    assert closed is True


@pytest.mark.asyncio
async def test_stream_response_validation_excludes_reasoning_and_tool_results(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skill, _ = _make_skill(
        tmp_path,
        response_schema={
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        },
    )

    async def events() -> AsyncIterator[HostedSkillEvent]:
        yield HostedSkillEvent(HostedSkillEventKind.SESSION, session_id="session-one")
        yield HostedSkillEvent(HostedSkillEventKind.DELTA, content='{"ok":')
        yield HostedSkillEvent(HostedSkillEventKind.INTERMEDIATE, content="not JSON")
        yield HostedSkillEvent(
            HostedSkillEventKind.TOOL_END,
            tool_call_id="call-one",
            result="not JSON",
        )
        yield HostedSkillEvent(HostedSkillEventKind.MESSAGE, content="true}")
        yield HostedSkillEvent(HostedSkillEventKind.DONE)

    monkeypatch.setattr(hosted_skill_module, "run_agent_events", lambda *_a, **_k: events())

    output = [event async for event in skill.stream("Return JSON")]

    assert output[-1].kind is HostedSkillEventKind.DONE
    assert sum(
        event.kind in {HostedSkillEventKind.DONE, HostedSkillEventKind.ERROR}
        for event in output
    ) == 1