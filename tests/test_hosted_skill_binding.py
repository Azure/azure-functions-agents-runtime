from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, get_type_hints
from unittest.mock import Mock

import pytest

import azure_functions_agents.hosted_skill_binding as hosted_skill_module
from azure_functions_agents import _hosted_recovery_input as recovery_input
from azure_functions_agents._hosted_recovery_input import AUDIT_INSTRUCTIONS, RECOVERY_INSTRUCTIONS
from azure_functions_agents._observability import FaultDomain
from azure_functions_agents._tool_descriptor import ToolDescriptor
from azure_functions_agents.config.schema import (
    AgentConfiguration,
    BuiltinEndpointsConfig,
    ResolvedAgent,
    ToolsFilter,
)
from azure_functions_agents.discovery.skills import SkillDescriptor
from azure_functions_agents.harness._harness_binding import AppHarness, HarnessKind
from azure_functions_agents.hosted_skill_binding import HostedSkill
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
from tests.test_copilot_execution import invocation_spans as invocation_spans


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

    monkeypatch.setattr(
        "azure_functions_agents._agent_run_diagnostics.start_span",
        fake_start_span,
    )
    return spans


def _make_skill(
    tmp_path: Path,
    *,
    response_schema: dict[str, Any] | None = None,
    harness_kind: HarnessKind = HarnessKind.MAF,
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
    harness = AppHarness(harness_kind, tmp_path)
    return HostedSkill(CatalogEntry(resolved, capabilities), harness), capabilities


@pytest.mark.asyncio
@pytest.mark.parametrize("harness_kind", list(HarnessKind))
async def test_run_forwards_catalog_values_with_one_session_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    harness_kind: HarnessKind,
) -> None:
    skill, capabilities = _make_skill(tmp_path, harness_kind=harness_kind)
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
    assert captured["_harness"].name is harness_kind
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
    caplog: pytest.LogCaptureFixture,
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

    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        await skill.run("Prepare order", session_id="session-one")

    [span] = spans
    assert (
        "HostedSkill binding executed: execution_surface=hosted_skill_binding "
        "agent_name=internal mode=run"
    ) in caplog.messages
    assert span.attributes["af.agent.name"] == "internal"
    assert span.attributes["af.agent.execution_surface"] == "hosted_skill_binding"
    assert span.attributes["af.agent.session_id"] == "session-one"
    assert span.attributes["af.agent.tool_call_count"] == 1
    assert span.attributes["af.agent.response_bytes"] == len("complete")
    assert span.attributes["af.agent.outcome"] == "success"


@pytest.mark.asyncio
async def test_copilot_run_explicitly_owns_observations(
    monkeypatch, tmp_path, invocation_spans,
) -> None:
    from azure_functions_agents.harness._harness_binding import HarnessRequest
    from azure_functions_agents.harness.copilot_sdk._copilot_observability import invocation_span

    _, exporter = invocation_spans
    skill, _ = _make_skill(tmp_path, harness_kind=HarnessKind.COPILOT)

    async def run_agent(_prompt: str, **kwargs: Any) -> AgentResult:
        request = HarnessRequest(
            prompt=_prompt, instructions=None, agent_slug=kwargs["agent_name"],
            session_id=kwargs["session_id"], new_session=True, model=kwargs["model"],
            tools=(), max_output_tokens=None, deadline=1,
        )
        with invocation_span(request) as observations:
            observations.add_event("af.agent.model.usage", {"gen_ai.usage.output_tokens": 3})
        return AgentResult(session_id=request.session_id, content="\u00e9")

    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)
    await skill.run("hello")

    semantic, outer = exporter.get_finished_spans()
    assert semantic.parent.span_id == outer.context.span_id
    assert semantic.events == ()
    assert [event.name for event in outer.events] == ["af.agent.model.usage"]
    assert outer.attributes["af.agent.response_bytes"] == 2


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

    monkeypatch.setattr(
        "azure_functions_agents._agent_run_diagnostics.start_span",
        recording_start_span,
    )
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

    monkeypatch.setattr(
        "azure_functions_agents._agent_run_diagnostics.start_span",
        recording_start_span,
    )
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
@pytest.mark.parametrize("harness_kind", list(HarnessKind))
@pytest.mark.parametrize("audit_only", [False, True])
async def test_run_recovery_forwards_selected_custom_guidance_to_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, harness_kind: HarnessKind, audit_only: bool,
) -> None:
    skill, _ = _make_skill(tmp_path, harness_kind=harness_kind)
    guidance = "CUSTOM ADVISOR: inspect the invoice fields before suggesting a correction."
    skill._entry.resolved.instructions = guidance
    captured: dict[str, Any] = {}

    async def run_agent(prompt: str, **kwargs: Any) -> AgentResult:
        captured.update(kwargs, prompt=prompt)
        return AgentResult(kwargs["session_id"], "Review the invoice.")

    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)

    await skill._run_recovery("Analyze this failure", tools=[], timeout=1, audit_only=audit_only)

    policy = AUDIT_INSTRUCTIONS if audit_only else RECOVERY_INSTRUCTIONS
    assert captured["instructions"].startswith(guidance + "\n\nRuntime")
    assert captured["instructions"].endswith("\n" + policy)
    assert captured["instructions"].count(guidance) == 1
    assert captured["prompt"] == "Analyze this failure"
    for key in (
        "tools", "mcp_tools", "skill_paths", "skills", "skill_catalog", "sandbox_tools",
        "web_request_tools",
    ):
        assert captured[key] == []
    assert skill._entry.resolved.instructions == guidance


@pytest.mark.asyncio
@pytest.mark.parametrize("harness_kind", list(HarnessKind))
@pytest.mark.parametrize("audit_only", [False, True])
async def test_run_recovery_isolates_tools_and_response_contract_from_normal_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, harness_kind: HarnessKind, audit_only: bool,
) -> None:
    skill, capabilities = _make_skill(
        tmp_path, harness_kind=harness_kind,
        response_schema={
            "type": "object", "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        },
    )
    tool_names = ["list_files", "read_file", "search_source"] if audit_only else ["retry_function"]
    tools = [ToolDescriptor.create(
        name=name, description="Invocation-local tool.", func=lambda: "complete",
    ) for name in tool_names]
    sandbox = Mock(return_value=["sandbox"])
    monkeypatch.setattr(hosted_skill_module, "build_sandbox_tools_for_session", sandbox)
    captured = []
    results = []

    async def run_agent(prompt: str, **kwargs: Any) -> AgentResult:
        captured.append((prompt, kwargs))
        content = '{"ok": true}' if len(captured) in (1, 3) else "not JSON"
        result = AgentResult(kwargs["session_id"], content)
        results.append(result)
        return result

    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)

    assert await skill.run("Normal before") is results[0]
    sandbox.assert_called_once()
    recovery_result = await skill._run_recovery(
        "Recover this invocation", tools=tools, timeout=3, audit_only=audit_only,
    )
    assert recovery_result is results[1]
    assert recovery_result.content == "not JSON"
    sandbox.assert_called_once()
    recovery_prompt, recovery = captured[1]
    assert recovery_prompt == "Recover this invocation"
    assert recovery["tools"] == tools
    assert recovery["tools"] is not tools
    assert recovery["tools"][0] is tools[0]
    assert [tool.name for tool in recovery["tools"]] == tool_names
    assert await recovery["tools"][0].invoke(arguments={}) == "complete"
    for key in ("mcp_tools", "skill_paths", "skills", "skill_catalog", "sandbox_tools", "web_request_tools"):
        assert recovery[key] == []
    assert recovery["instructions"] != skill._entry.resolved.instructions
    assert recovery["instructions"] == recovery_input.recovery_instructions(
        skill._entry.resolved.instructions, audit_only=audit_only,
    )
    assert AUDIT_INSTRUCTIONS != RECOVERY_INSTRUCTIONS
    if not audit_only:
        assert "drop unfamiliar" in recovery["instructions"]
        assert "exact input text" in recovery["instructions"]
    assert recovery["timeout"] == 3
    assert recovery["model"] == "model-one"
    assert recovery["agent_name"] == "internal"
    assert recovery["agent_configuration"] is skill._entry.resolved.agent_configuration
    assert recovery["_harness"] is skill._harness
    assert recovery["_session_is_new"] is True
    recovery["tools"].clear()
    assert len(tools) == len(tool_names)

    assert await skill.run("Normal after") is results[2]
    assert sandbox.call_count == 2
    for index, prompt in ((0, "Normal before"), (2, "Normal after")):
        normal_prompt, normal = captured[index]
        assert normal_prompt.endswith(prompt)
        assert normal_prompt != prompt
        assert normal["instructions"] == "Follow policy."
        assert normal["timeout"] == 12
        assert normal["tools"] == capabilities.filtered_user_tools == ["tool"]
        assert normal["mcp_tools"] == capabilities.filtered_mcp_tools == ["mcp"]
        assert normal["skill_paths"] == capabilities.enabled_skill_paths
        assert normal["skills"] == list(capabilities.skills)
        assert normal["skill_catalog"] == list(capabilities.skill_catalog)
        assert normal["web_request_tools"] == capabilities.web_request_tools == ["web"]
        assert normal["sandbox_tools"] == ["sandbox"]
    assert len({kwargs["session_id"] for _, kwargs in captured}) == 3
    assert skill._entry.resolved.instructions == "Follow policy."
    with pytest.raises(InvalidResponseJsonError):
        await skill.run("Still validate normal responses")


def test_run_recovery_has_additive_keyword_only_audit_option() -> None:
    parameters = inspect.signature(HostedSkill._run_recovery).parameters

    assert list(parameters) == ["self", "prompt", "tools", "timeout", "audit_only"]
    for name in ("tools", "timeout", "audit_only"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["tools"].default is inspect.Parameter.empty
    assert parameters["timeout"].default is inspect.Parameter.empty
    assert parameters["audit_only"].default is False
    assert get_type_hints(HostedSkill._run_recovery)["audit_only"] is bool


@pytest.mark.asyncio
@pytest.mark.parametrize("harness_kind", list(HarnessKind))
async def test_concurrent_audit_and_retry_calls_do_not_mutate_shared_catalog_or_normal_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, harness_kind: HarnessKind,
) -> None:
    skill, capabilities = _make_skill(tmp_path, harness_kind=harness_kind)
    audit_tools = [ToolDescriptor.create(
        name=name, description="Inspect source only.", func=lambda: "source",
    ) for name in ("list_files", "read_file", "search_source")]
    retry_tools = [ToolDescriptor.create(
        name="retry_function", description="Retry this invocation only.", func=lambda: "retried",
    )]
    captured = {}
    both_running = asyncio.Event()
    sandbox = Mock(return_value=["sandbox"])
    monkeypatch.setattr(hosted_skill_module, "build_sandbox_tools_for_session", sandbox)

    async def run_agent(prompt: str, **kwargs: Any) -> AgentResult:
        captured[prompt] = kwargs
        if len(captured) == 2:
            both_running.set()
        if prompt in {"Audit this failure", "Retry this failure"}:
            await both_running.wait()
        return AgentResult(kwargs["session_id"], prompt)

    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)
    audit_result, retry_result = await asyncio.wait_for(asyncio.gather(
        skill._run_recovery("Audit this failure", tools=audit_tools, timeout=2, audit_only=True),
        skill._run_recovery("Retry this failure", tools=retry_tools, timeout=3),
    ), timeout=2)

    assert audit_result.content == "Audit this failure"
    assert retry_result.content == "Retry this failure"
    assert set(captured) == {"Audit this failure", "Retry this failure"}
    audit = captured["Audit this failure"]
    retry = captured["Retry this failure"]
    assert audit["instructions"] == recovery_input.recovery_instructions(
        skill._entry.resolved.instructions, audit_only=True,
    )
    assert retry["instructions"] == recovery_input.recovery_instructions(
        skill._entry.resolved.instructions,
    )
    assert audit["timeout"] == 2
    assert retry["timeout"] == 3
    assert audit["session_id"] == audit_result.session_id
    assert retry["session_id"] == retry_result.session_id
    assert audit["session_id"] != retry["session_id"]
    assert audit["tools"] == audit_tools
    assert retry["tools"] == retry_tools
    assert audit["tools"] is not audit_tools
    assert retry["tools"] is not retry_tools
    assert audit["tools"] is not retry["tools"]
    for call in (audit, retry):
        assert call["_harness"] is skill._harness
        assert call["agent_configuration"] is skill._entry.resolved.agent_configuration
        assert call["_session_is_new"] is True
    for key in ("mcp_tools", "skill_paths", "skills", "skill_catalog", "sandbox_tools", "web_request_tools"):
        assert audit[key] == retry[key] == []
        assert audit[key] is not retry[key]
        audit[key].append("audit-local mutation")
        assert retry[key] == []
    audit["tools"].clear()
    assert len(audit_tools) == 3
    assert retry["tools"] == retry_tools
    sandbox.assert_not_called()

    normal_result = await skill.run("Normal after both modes")
    normal = captured[normal_result.content]
    sandbox.assert_called_once()
    assert normal["instructions"] == "Follow policy."
    assert normal["timeout"] == 12
    assert normal["tools"] == capabilities.filtered_user_tools == ["tool"]
    assert normal["mcp_tools"] == capabilities.filtered_mcp_tools == ["mcp"]
    assert normal["skill_paths"] == capabilities.enabled_skill_paths
    assert normal["skills"] == list(capabilities.skills)
    assert normal["skill_catalog"] == list(capabilities.skill_catalog)
    assert normal["web_request_tools"] == capabilities.web_request_tools == ["web"]
    assert normal["sandbox_tools"] == ["sandbox"]
    assert len({call["session_id"] for call in (audit, retry, normal)}) == 3
    assert skill._entry.resolved.instructions == "Follow policy."


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_only", [False, True])
async def test_run_recovery_keeps_selected_advisor_guidance_separate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, audit_only: bool,
) -> None:
    custom, _ = _make_skill(tmp_path)
    default, _ = _make_skill(tmp_path)
    custom_guidance = "CUSTOM GUIDANCE: inspect invoice fields."
    default_guidance = "DEFAULT GUIDANCE: inspect generic handler inputs."
    custom._entry.resolved.instructions = custom_guidance
    default._entry.resolved.instructions = default_guidance
    captured: dict[str, str] = {}

    async def run_agent(prompt: str, **kwargs: Any) -> AgentResult:
        captured[prompt] = kwargs["instructions"]
        return AgentResult(kwargs["session_id"], "Review inputs.")

    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)
    for prompt, skill in (("default", default), ("custom", custom), ("default again", default)):
        await skill._run_recovery(prompt, tools=[], timeout=1, audit_only=audit_only)

    assert captured["custom"] == recovery_input.recovery_instructions(
        custom_guidance, audit_only=audit_only,
    )
    assert captured["default"] == captured["default again"] == recovery_input.recovery_instructions(
        default_guidance, audit_only=audit_only,
    )
    assert default_guidance not in captured["custom"]
    assert custom_guidance not in captured["default"]
    assert custom._entry.resolved.instructions == custom_guidance
    assert default._entry.resolved.instructions == default_guidance


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_only", [False, True])
async def test_run_recovery_validates_prompt_before_runner_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, audit_only: bool,
) -> None:
    skill, _ = _make_skill(tmp_path)
    runner = Mock()
    monkeypatch.setattr(hosted_skill_module, "run_agent", runner)

    with pytest.raises(ValueError, match="nonblank"):
        await skill._run_recovery(" ", tools=[], timeout=1, audit_only=audit_only)

    runner.assert_not_called()


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
@pytest.mark.parametrize("harness_kind", list(HarnessKind))
async def test_stream_binds_sandbox_and_runner_to_same_public_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_id: str | None,
    harness_kind: HarnessKind,
    caplog: pytest.LogCaptureFixture,
) -> None:
    skill, capabilities = _make_skill(tmp_path, harness_kind=harness_kind)
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
        captured["harness"] = kwargs["_harness"]
        yield HostedSkillEvent(
            HostedSkillEventKind.SESSION,
            session_id=kwargs["session_id"],
        )
        yield HostedSkillEvent(HostedSkillEventKind.DONE)

    monkeypatch.setattr(hosted_skill_module, "build_sandbox_tools_for_session", build_sandbox)
    monkeypatch.setattr(hosted_skill_module, "run_agent_events", events)

    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        output = [event async for event in skill.stream("Prepare order", session_id=session_id)]

    assert captured["sandbox"] == captured["runner"] == output[0].session_id
    assert (
        "HostedSkill binding executed: execution_surface=hosted_skill_binding "
        "agent_name=internal mode=stream"
    ) in caplog.messages
    assert captured["execution_surface"] == "hosted_skill_binding"
    assert captured["session_is_new"] is (session_id is None)
    assert captured["display_name"] == "Internal"
    assert captured["harness"].name is harness_kind
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