from __future__ import annotations

import asyncio
import inspect
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_type_hints
from unittest.mock import AsyncMock, Mock

import azure.durable_functions as df
import azure.functions as func
import pytest

import azure_functions_agents as agents_module
import azure_functions_agents._hosted_skill_app as hosted_app_module
import azure_functions_agents._hosted_skill_recovery as recovery_module
import azure_functions_agents.hosted_skill_binding as hosted_skill_module
from azure_functions_agents import HostedSkill
from azure_functions_agents._hosted_recovery_http import RecoveryRequest
from azure_functions_agents._hosted_recovery_input import (
    AUDIT_INSTRUCTIONS,
    RECOVERY_INSTRUCTIONS,
    HostedSkillRecoveryError,
)
from azure_functions_agents._hosted_recovery_queue import RecoveryQueueMessage
from azure_functions_agents._hosted_skill_app import (
    HostedSkillDFApp,
    HostedSkillFunctionApp,
)
from azure_functions_agents.config.schema import (
    AgentConfiguration,
    BuiltinEndpointsConfig,
    ResolvedAgent,
    SubagentRef,
    ToolsFilter,
    WorkflowConfig,
)
from azure_functions_agents.harness._harness_binding import AppHarness, HarnessKind
from azure_functions_agents.registration.capabilities import AgentCapabilities
from azure_functions_agents.registration.catalog import CatalogEntry, build_catalog
from azure_functions_agents.runner import AgentResult


def _entry(
    tmp_path: Path,
    *,
    subagents: list[SubagentRef] | None = None,
    workflows: WorkflowConfig | None = None,
) -> CatalogEntry:
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
        workflows=workflows,
        subagents=subagents or [],
        sandbox_config=None,
        input_schema=None,
        response_schema=None,
        response_example=None,
        source_file=str(tmp_path / "internal.agent.md"),
        agent_configuration=AgentConfiguration(),
    )
    return CatalogEntry(resolved, AgentCapabilities())


def _app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    entry: CatalogEntry | None = None,
    harness_kind: HarnessKind = HarnessKind.MAF,
    durable: bool = False,
) -> tuple[HostedSkillFunctionApp | HostedSkillDFApp, list[str | None]]:
    validated_models: list[str | None] = []

    def validate_hosted_skill(resolved: ResolvedAgent) -> None:
        validated_models.append(resolved.model)

    catalog = build_catalog({"internal": entry or _entry(tmp_path)})
    validator = validate_hosted_skill if harness_kind is HarnessKind.MAF else None
    harness = AppHarness(
        harness_kind,
        tmp_path,
        _create_runner=lambda _harness: SimpleNamespace(validate_hosted_skill=validator),
    )
    app_type = HostedSkillDFApp if durable else HostedSkillFunctionApp
    return app_type(catalog=catalog, harness=harness), validated_models


def _audit_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record for record in caplog.records
        if record.name == "azure.functions.AgentRuntime"
        and record.getMessage().startswith("smart_retry audit-only remediation:")
    ]


def _audit_input(trigger_kind: str, *, valid: bool = False) -> func.HttpRequest | func.QueueMessage:
    count = "1" if valid else "original"
    if trigger_kind == "queue":
        body = b'{"count": 1}' if valid else b'{"count": "original"}'
        return func.QueueMessage(body=body, id="message-one", pop_receipt="receipt")
    return func.HttpRequest(
        method="HEAD" if trigger_kind == "head" else "GET",
        url=f"https://example.test/count/{count}?view=full", body=b"",
        route_params={"count": count}, params={"view": "full"},
        headers={"X-Original": "unchanged"},
    )


@pytest.mark.asyncio
async def test_decorator_hides_parameter_and_injects_fresh_facades(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, validated_models = _app(monkeypatch, tmp_path)

    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):

        @app.route(route="orders")
        @app.hosted_skill(arg_name="skill", agent_name="internal")
        async def handler(req: func.HttpRequest, skill: HostedSkill) -> HostedSkill:
            return skill

    assert validated_models == ["model-one"]
    assert (
        "HostedSkill binding defined: execution_surface=hosted_skill_binding "
        "agent_name=internal handler=handler arg_name=skill"
    ) in caplog.messages
    functions = app.get_functions()
    assert [function.get_function_name() for function in functions] == ["handler"]
    registered_handler = functions[0].get_user_function()
    assert list(inspect.signature(registered_handler).parameters) == ["req"]
    assert "skill" not in registered_handler.__annotations__
    assert registered_handler.__wrapped__.__name__ == "handler"
    assert get_type_hints(registered_handler.__wrapped__)["skill"] is HostedSkill

    request = object()
    first = await registered_handler(request)
    second = await registered_handler(request)

    assert isinstance(first, HostedSkill)
    assert isinstance(second, HostedSkill)
    assert first is not second


@pytest.mark.asyncio
async def test_decorator_preserves_variadic_arguments_and_rejects_injection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app, _ = _app(monkeypatch, tmp_path)

    @app.hosted_skill(arg_name="skill", agent_name="internal")
    async def handler(
        value: str,
        skill: HostedSkill,
        *rest: str,
        suffix: str = "default",
    ) -> tuple[str, HostedSkill, tuple[str, ...], str]:
        return value, skill, rest, suffix

    value, skill, rest, suffix = await handler("one", "two", suffix="custom")

    assert value == "one"
    assert isinstance(skill, HostedSkill)
    assert rest == ("two",)
    assert suffix == "custom"
    with pytest.raises(TypeError, match="runtime-managed"):
        await handler("one", skill=skill)


@pytest.mark.parametrize(
    ("handler_factory", "message"),
    [
        (lambda: (lambda skill: None), "async def"),
    ],
)
def test_decorator_rejects_invalid_handler_shape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    handler_factory: Any,
    message: str,
) -> None:
    app, _ = _app(monkeypatch, tmp_path)

    with pytest.raises(TypeError, match=message):
        app.hosted_skill(arg_name="skill", agent_name="internal")(handler_factory())


def test_decorator_rejects_missing_parameter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app, _ = _app(monkeypatch, tmp_path)

    async def handler() -> None:
        pass

    with pytest.raises(TypeError, match="not present"):
        app.hosted_skill(arg_name="skill", agent_name="internal")(handler)


def test_decorator_requires_hosted_skill_annotation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app, _ = _app(monkeypatch, tmp_path)

    with pytest.raises(TypeError, match="annotated HostedSkill"):

        @app.hosted_skill(arg_name="skill", agent_name="internal")
        async def handler(skill: object) -> None:
            pass


def test_decorator_rejects_unknown_agent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app, _ = _app(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="Unknown HostedSkill agent 'missing'"):
        app.hosted_skill(arg_name="skill", agent_name="missing")


@pytest.mark.parametrize(
    ("entry_kwargs", "message"),
    [
        ({"subagents": [SubagentRef(agent="other")]}, "subagents"),
        ({"workflows": WorkflowConfig(enabled=True)}, "Dynamic Workflows"),
    ],
)
def test_decorator_rejects_unsupported_agent_capabilities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    entry_kwargs: dict[str, Any],
    message: str,
) -> None:
    app, _ = _app(monkeypatch, tmp_path, entry=_entry(tmp_path, **entry_kwargs))

    with pytest.raises(ValueError, match=message):
        app.hosted_skill(arg_name="skill", agent_name="internal")


@pytest.mark.parametrize("harness_kind", list(HarnessKind))
def test_decorator_accepts_bound_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    harness_kind: HarnessKind,
) -> None:
    app, validated_models = _app(monkeypatch, tmp_path, harness_kind=harness_kind)

    decorator = app.hosted_skill(arg_name="skill", agent_name="internal")

    assert callable(decorator)
    assert validated_models == (["model-one"] if harness_kind is HarnessKind.MAF else [])


def test_enhanced_apps_preserve_sdk_base_types(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    catalog = build_catalog({"internal": _entry(tmp_path)})
    harness = AppHarness(
        HarnessKind.MAF,
        tmp_path,
        _create_runner=lambda _harness: SimpleNamespace(
            validate_hosted_skill=lambda _resolved: None
        ),
    )

    function_app = HostedSkillFunctionApp(catalog=catalog, harness=harness)
    durable_app = HostedSkillDFApp(catalog=catalog, harness=harness)

    assert isinstance(function_app, func.FunctionApp)
    assert isinstance(durable_app, df.DFApp)


@pytest.mark.parametrize("durable", [False, True])
def test_smart_retry_has_exact_keyword_only_options_and_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, durable: bool,
) -> None:
    app, _ = _app(monkeypatch, tmp_path, durable=durable)
    parameters = inspect.signature(app.smart_retry).parameters

    assert list(parameters) == ["agent_name", "max_retries", "timeout", "request_arg", "audit_only"]
    assert all(parameter.kind is inspect.Parameter.KEYWORD_ONLY for parameter in parameters.values())
    assert parameters["agent_name"].default is inspect.Parameter.empty
    assert parameters["max_retries"].default == 1
    assert type(parameters["max_retries"].default) is int
    assert parameters["timeout"].default == 60
    assert parameters["request_arg"].default is None
    assert parameters["audit_only"].default is False
    hints = get_type_hints(app.smart_retry)
    assert hints["agent_name"] is str
    assert hints["max_retries"] is int
    assert hints["timeout"] is float
    assert hints["request_arg"] == str | None
    assert hints["audit_only"] is bool


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("audit_only", [False, True])
def test_smart_retry_preserves_authored_signature_annotations_and_worker_bindings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, durable: bool, audit_only: bool,
) -> None:
    app, _ = _app(monkeypatch, tmp_path, durable=durable)

    async def handler(
        incoming: func.HttpRequest, /, context: func.Context, *, label: str = "default",
    ) -> func.HttpResponse:
        """Authored handler documentation."""
        return func.HttpResponse(label)

    signature = inspect.signature(handler)
    annotations = dict(handler.__annotations__)
    wrapped = app.smart_retry(agent_name="internal", audit_only=audit_only)(handler)
    app.route(route="signature", trigger_arg_name="incoming")(wrapped)
    function = next(item for item in app.get_functions() if item.get_function_name() == "handler")
    registered = function.get_user_function()

    assert registered is wrapped
    assert registered.__wrapped__ is handler
    assert inspect.signature(registered) == signature
    assert registered.__annotations__ == annotations
    assert handler.__annotations__ == annotations
    assert get_type_hints(registered) == get_type_hints(handler)
    assert registered.__name__ == handler.__name__
    assert registered.__qualname__ == handler.__qualname__
    assert registered.__doc__ == handler.__doc__
    assert inspect.iscoroutinefunction(registered)
    assert [binding.name for binding in function.get_bindings()] == ["incoming", "$return"]
    assert app._hosted_skill_recovery_bindings[registered].argument == "incoming"


def test_removed_recovery_configuration_is_not_exported() -> None:
    assert "HostedSkillRecovery" not in agents_module.__all__
    assert not hasattr(agents_module, "HostedSkillRecovery")
    assert not hasattr(recovery_module, "HostedSkillRecovery")


@pytest.mark.parametrize("durable", [False, True])
def test_decorators_reject_removed_keywords(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, durable: bool,
) -> None:
    app, _ = _app(monkeypatch, tmp_path, durable=durable)

    assert list(inspect.signature(app.hosted_skill).parameters) == ["arg_name", "agent_name"]
    with pytest.raises(TypeError, match="recovery"):
        app.hosted_skill(arg_name="skill", agent_name="internal", recovery=None)
    with pytest.raises(TypeError, match="arg_name"):
        app.smart_retry(agent_name="internal", arg_name="skill")


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("inner, outer", [
    ("smart_retry", "smart_retry"),
    ("hosted_skill", "smart_retry"),
    ("smart_retry", "hosted_skill"),
    ("hosted_skill", "hosted_skill"),
])
def test_decorators_reject_repeated_wrapping_and_injection_retry_composition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, durable: bool, inner: str, outer: str,
) -> None:
    app, _ = _app(monkeypatch, tmp_path, durable=durable)
    decorators = {
        "hosted_skill": app.hosted_skill(arg_name="skill", agent_name="internal"),
        "smart_retry": app.smart_retry(agent_name="internal", request_arg="req"),
    }

    async def handler(req: func.HttpRequest, skill: HostedSkill) -> None:
        pass

    wrapped = decorators[inner](handler)

    with pytest.raises(TypeError, match="cannot be combined or repeated"):
        decorators[outer](wrapped)


@pytest.mark.parametrize("durable", [False, True])
def test_smart_retry_rejects_sync_handlers_and_sdk_builder_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, durable: bool,
) -> None:
    app, _ = _app(monkeypatch, tmp_path, durable=durable)

    with pytest.raises(TypeError, match="async def"):
        app.smart_retry(agent_name="internal")(lambda req: None)

    async def handler(req: func.HttpRequest) -> None:
        pass

    builder = app.route(route="wrong-order")(handler)
    with pytest.raises(TypeError, match="innermost decorator"):
        app.smart_retry(agent_name="internal")(builder)


@pytest.mark.parametrize("agent_name", ["", None, 1])
def test_smart_retry_rejects_invalid_agent_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agent_name: Any,
) -> None:
    app, _ = _app(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="agent_name must be a non-empty string"):
        app.smart_retry(agent_name=agent_name)


def test_smart_retry_rejects_unknown_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    app, _ = _app(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="Unknown HostedSkill agent 'missing'"):
        app.smart_retry(agent_name="missing")


@pytest.mark.parametrize("entry_kwargs, message", [
    ({"subagents": [SubagentRef(agent="other")]}, "subagents"),
    ({"workflows": WorkflowConfig(enabled=True)}, "Dynamic Workflows"),
])
def test_smart_retry_rejects_unsupported_agent_capabilities(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, entry_kwargs: dict[str, Any], message: str,
) -> None:
    app, _ = _app(monkeypatch, tmp_path, entry=_entry(tmp_path, **entry_kwargs))

    with pytest.raises(ValueError, match=message):
        app.smart_retry(agent_name="internal")


@pytest.mark.parametrize("harness_kind", list(HarnessKind))
def test_smart_retry_qualifies_the_app_bound_harness_without_running_an_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, harness_kind: HarnessKind,
) -> None:
    app, validated_models = _app(monkeypatch, tmp_path, harness_kind=harness_kind)
    runner = AsyncMock()
    monkeypatch.setattr(hosted_skill_module, "run_agent", runner)

    decorator = app.smart_retry(agent_name="internal")

    assert callable(decorator)
    assert validated_models == (["model-one"] if harness_kind is HarnessKind.MAF else [])
    runner.assert_not_called()


def test_smart_retry_propagates_provider_qualification_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    app, _ = _app(monkeypatch, tmp_path)
    validator = Mock(side_effect=ValueError("Provider configuration missing"))
    monkeypatch.setattr(
        "azure_functions_agents.harness._agent_runner.get_agent_runner",
        lambda _harness: SimpleNamespace(validate_hosted_skill=validator),
    )

    with pytest.raises(ValueError, match="Provider configuration missing"):
        app.smart_retry(agent_name="internal")

    validator.assert_called_once_with(app._hosted_skill_catalog["internal"].resolved)


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("keyword_request", [False, True])
async def test_smart_retry_replays_original_handler_without_injection_and_preserves_arguments(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    durable: bool,
    keyword_request: bool,
) -> None:
    app, _ = _app(monkeypatch, tmp_path, durable=durable)
    calls = []
    runner_calls = []
    retry_results = []
    recover = AsyncMock(wraps=recovery_module.recover_handler)
    monkeypatch.setattr(hosted_app_module, "recover_handler", recover)
    response = func.HttpResponse("3", status_code=202, headers={"X-Original": "yes"})
    context = object()
    marker = object()
    request = func.HttpRequest(
        method="GET", url="https://example.test/add/1/2?view=full", body=b"",
        route_params={"a": "1", "b": "2"}, params={"view": "full"},
        headers={"X-Request": "original"},
    )

    @app.smart_retry(agent_name="internal", request_arg="http_input", timeout=2)
    async def handler(
        http_input: func.HttpRequest, context: object,
        *rest: object, suffix: str = "default", **extras: object,
    ) -> func.HttpResponse:
        calls.append((http_input, context, rest, suffix, extras))
        a, b = http_input.route_params["a"], http_input.route_params["b"]
        if type(a) is not int or type(b) is not int:
            raise TypeError("Expected exact integers")
        return response if a + b == 3 else func.HttpResponse("wrong sum", status_code=400)

    async def run_agent(prompt: str, **kwargs: Any) -> AgentResult:
        runner_calls.append((prompt, kwargs))
        retry = next(tool for tool in kwargs["tools"] if tool.name == "retry_function")
        retry_results.append(await retry.invoke(arguments={"route_params": {"a": 1, "b": 2}}))
        return AgentResult(kwargs["session_id"], "Model text is not the HTTP response")

    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)
    if keyword_request:
        result = await handler(http_input=request, context=context, suffix="custom", marker=marker)
        expected_rest = ()
    else:
        result = await handler(request, context, marker, suffix="custom", marker=marker)
        expected_rest = (marker,)

    assert result is response
    assert len(runner_calls) == 1
    assert runner_calls[0][1]["timeout"] == 2
    recover.assert_awaited_once()
    assert recover.await_args.args[4] is handler.__wrapped__
    assert isinstance(recover.await_args.args[0], HostedSkill)
    prompt = json.loads(runner_calls[0][0].rsplit("\n", 1)[1])
    assert prompt["handler"] == handler.__wrapped__.__qualname__
    assert retry_results == [{"status": "succeeded"}]
    assert len(calls) == 2
    original, retried = calls
    assert original[0] is request
    assert retried[0] is not request
    for call in calls:
        assert call[1] is context
        assert call[2] == expected_rest
        assert call[3] == "custom"
        assert call[4] == {"marker": marker}
    assert retried[0].route_params == {"a": 1, "b": 2}
    assert all(type(value) is int for value in retried[0].route_params.values())
    assert request.route_params == {"a": "1", "b": "2"}
    assert request.params == {"view": "full"}
    assert request.url == "https://example.test/add/1/2?view=full"
    assert dict(request.headers) == {"x-request": "original"}
    assert request.get_body() == b""


@pytest.mark.asyncio
async def test_smart_retry_normal_success_does_not_run_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    app, _ = _app(monkeypatch, tmp_path)
    runner = AsyncMock()
    monkeypatch.setattr(hosted_skill_module, "run_agent", runner)
    response = func.HttpResponse("unchanged")
    calls = []

    @app.smart_retry(agent_name="internal", request_arg="req")
    async def handler(req: func.HttpRequest) -> func.HttpResponse:
        calls.append(req)
        return response

    request = func.HttpRequest(method="GET", url="https://example.test/", body=b"")
    assert await handler(request) is response
    assert len(calls) == 1
    assert calls[0] is request
    runner.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt_retry", [False, True])
async def test_smart_retry_no_correction_or_failed_retry_returns_500(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, attempt_retry: bool,
) -> None:
    app, _ = _app(monkeypatch, tmp_path)
    calls = []
    runner_calls = []
    retry_results = []

    @app.smart_retry(agent_name="internal", request_arg="req")
    async def handler(req: func.HttpRequest) -> func.HttpResponse:
        calls.append(req)
        raise ValueError("Handler still fails")

    async def run_agent(_prompt: str, **kwargs: Any) -> AgentResult:
        runner_calls.append(kwargs)
        if attempt_retry:
            retry = next(tool for tool in kwargs["tools"] if tool.name == "retry_function")
            for _ in range(2):
                retry_results.append(await retry.invoke(arguments={"route_params": {"a": 1, "b": 2}}))
        return AgentResult(kwargs["session_id"], "Review the handler inputs.")

    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)
    request = func.HttpRequest(
        method="GET", url="https://example.test/add/1/2", body=b"",
        route_params={"a": "1", "b": "2"},
    )
    result = await handler(request)

    assert result.status_code == 500
    assert json.loads(result.get_body()) == {
        "error": "FUNCTION_RECOVERY_FAILED", "requires_user_action": True,
        "remediation": "Review the handler inputs.",
    }
    assert len(runner_calls) == 1
    assert runner_calls[0]["timeout"] == 60
    assert len(calls) == (2 if attempt_retry else 1)
    assert calls[0] is request
    if attempt_retry:
        assert calls[1].route_params == {"a": 1, "b": 2}
        assert retry_results == [
            {"status": "failed", "error_type": "ValueError"},
            {"status": "failed", "error_type": "RuntimeError"},
        ]
    assert request.route_params == {"a": "1", "b": "2"}


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_at", ["handler", "agent", "retry"])
async def test_smart_retry_propagates_cancellation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cancel_at: str,
) -> None:
    app, _ = _app(monkeypatch, tmp_path)
    entered = asyncio.Event()
    calls = []
    runner_calls = []

    @app.smart_retry(agent_name="internal", request_arg="req")
    async def handler(req: func.HttpRequest) -> func.HttpResponse:
        calls.append(req)
        if cancel_at == "handler" or len(calls) == 2:
            entered.set()
            await asyncio.Event().wait()
        raise ValueError("Recover this input")

    async def run_agent(_prompt: str, **kwargs: Any) -> AgentResult:
        runner_calls.append(kwargs)
        if cancel_at == "retry":
            retry = next(tool for tool in kwargs["tools"] if tool.name == "retry_function")
            await retry.invoke(arguments={"route_params": {"a": 1, "b": 2}})
        entered.set()
        await asyncio.Event().wait()
        return AgentResult(kwargs["session_id"], "unreachable")

    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)
    request = func.HttpRequest(
        method="GET", url="https://example.test/add/1/2", body=b"",
        route_params={"a": "1", "b": "2"},
    )
    task = asyncio.create_task(handler(request))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(runner_calls) == (0 if cancel_at == "handler" else 1)
    assert len(calls) == (2 if cancel_at == "retry" else 1)
    assert request.route_params == {"a": "1", "b": "2"}


@pytest.mark.asyncio
@pytest.mark.parametrize("separate_wrappers", [False, True])
async def test_concurrent_smart_retry_wrappers_keep_invocation_state_isolated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, separate_wrappers: bool,
) -> None:
    app, _ = _app(monkeypatch, tmp_path)
    calls = {"one": [], "two": []}
    responses = {name: func.HttpResponse(name) for name in calls}
    runners = []
    retry_results = []
    both_running = asyncio.Event()
    recover = AsyncMock(wraps=recovery_module.recover_handler)
    monkeypatch.setattr(hosted_app_module, "recover_handler", recover)

    def decorate():
        @app.smart_retry(agent_name="internal", request_arg="req")
        async def handler(req: func.HttpRequest, *, label: str) -> func.HttpResponse:
            calls[label].append(req)
            a, b = req.route_params["a"], req.route_params["b"]
            if type(a) is not int or type(b) is not int:
                raise TypeError("Expected exact integers")
            return responses[label] if a + b == 3 else func.HttpResponse("wrong sum", status_code=400)

        return handler

    async def run_agent(_prompt: str, **kwargs: Any) -> AgentResult:
        runners.append(kwargs)
        if len(runners) == 2:
            both_running.set()
        await both_running.wait()
        retry = next(tool for tool in kwargs["tools"] if tool.name == "retry_function")
        retry_results.append(await retry.invoke(arguments={"route_params": {"a": 1, "b": 2}}))
        retry_results.append(await retry.invoke(arguments={"route_params": {"a": 1, "b": 2}}))
        return AgentResult(kwargs["session_id"], "not the handler response")

    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)
    first = decorate()
    second = decorate() if separate_wrappers else first
    requests = [func.HttpRequest(
        method="GET", url=f"https://example.test/{name}", body=b"",
        route_params={"a": "1", "b": "2"},
    ) for name in calls]
    results = await asyncio.wait_for(asyncio.gather(
        first(requests[0], label="one"), second(requests[1], label="two"),
    ), timeout=2)

    assert results[0] is responses["one"]
    assert results[1] is responses["two"]
    assert len(runners) == 2
    assert runners[0]["session_id"] != runners[1]["session_id"]
    assert runners[0]["tools"] is not runners[1]["tools"]
    assert recover.await_count == 2
    assert recover.await_args_list[0].args[0] is not recover.await_args_list[1].args[0]
    assert retry_results.count({"status": "succeeded"}) == 2
    assert retry_results.count({"status": "failed", "error_type": "RuntimeError"}) == 2
    for (label, invocation_calls), request in zip(calls.items(), requests, strict=True):
        assert len(invocation_calls) == 2
        assert invocation_calls[0] is request
        assert invocation_calls[1] is not request
        assert invocation_calls[1].url.endswith(label)
        assert request.route_params == {"a": "1", "b": "2"}


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("audit_only", [False, True])
@pytest.mark.parametrize("options, message", [
    ({"timeout": 0}, "finite and positive"),
    ({"timeout": -1}, "finite and positive"),
    ({"timeout": True}, "finite and positive"),
    ({"timeout": False}, "finite and positive"),
    ({"timeout": float("inf")}, "finite and positive"),
    ({"timeout": float("-inf")}, "finite and positive"),
    ({"timeout": float("nan")}, "finite and positive"),
    ({"timeout": "60"}, "finite and positive"),
    ({"timeout": None}, "finite and positive"),
    ({"request_arg": "not valid"}, "parameter name"),
    ({"request_arg": "class"}, "parameter name"),
    ({"request_arg": ""}, "parameter name"),
    ({"request_arg": 1}, "parameter name"),
    ({"max_retries": 0}, "positive integer"),
    ({"max_retries": -1}, "positive integer"),
    ({"max_retries": True}, "positive integer"),
    ({"max_retries": False}, "positive integer"),
    ({"max_retries": 1.0}, "positive integer"),
    ({"max_retries": 1.5}, "positive integer"),
    ({"max_retries": "1"}, "positive integer"),
    ({"max_retries": None}, "positive integer"),
])
def test_smart_retry_validates_options_at_decorator_definition(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    options: dict[str, Any], message: str, durable: bool, audit_only: bool,
) -> None:
    app, _ = _app(monkeypatch, tmp_path, durable=durable)

    with pytest.raises(ValueError, match=message):
        app.smart_retry(agent_name="internal", audit_only=audit_only, **options)


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("audit_only", [0, 1, -1, 0.0, 1.0, "true", "false", "", None, [], {}])
def test_smart_retry_requires_an_exact_boolean_audit_option_before_qualification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, durable: bool, audit_only: Any,
) -> None:
    app, validated_models = _app(monkeypatch, tmp_path, durable=durable)

    with pytest.raises(ValueError, match="audit_only"):
        app.smart_retry(agent_name="internal", audit_only=audit_only)

    assert validated_models == []


@pytest.mark.parametrize("request_arg", ["missing", "rest", "extras"])
def test_smart_retry_requires_a_concrete_request_parameter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request_arg: str,
) -> None:
    app, _ = _app(monkeypatch, tmp_path)

    async def handler(req: func.HttpRequest, *rest: object, **extras: object) -> None:
        pass

    with pytest.raises(TypeError, match="visible handler parameter"):
        app.smart_retry(agent_name="internal", request_arg=request_arg)(handler)


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("keyword_request", [False, True])
@pytest.mark.parametrize("max_retries", [1, 3])
async def test_smart_retry_selects_renamed_http_trigger_and_retries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, durable: bool, keyword_request: bool,
    max_retries: int,
) -> None:
    app, _ = _app(monkeypatch, tmp_path, durable=durable)
    response = func.HttpResponse("corrected")
    calls = []
    retry_results = []

    @app.route(route="value/{a}", trigger_arg_name="incoming")
    @app.smart_retry(agent_name="internal", max_retries=max_retries)
    async def handler(context: object, incoming: object) -> func.HttpResponse:
        calls.append((context, incoming))
        if incoming.route_params["a"] != "1":
            raise ValueError("Needs correction")
        return response

    async def run_agent(_prompt: str, **kwargs: Any) -> AgentResult:
        retry = next(tool for tool in kwargs["tools"] if tool.name == "retry_function")
        for attempt in range(max_retries):
            retry_results.append(await retry.invoke(arguments={
                "route_params": {"a": "1" if attempt == max_retries - 1 else "wrong"},
            }))
        return AgentResult(kwargs["session_id"], "done")

    runner = AsyncMock(side_effect=run_agent)
    monkeypatch.setattr(hosted_skill_module, "run_agent", runner)
    function = next(item for item in app.get_functions() if item.get_function_name() == "handler")
    registered = function.get_user_function()
    marker = object()
    request = func.HttpRequest(
        method="GET", url="https://example.test/value/one", body=b"",
        route_params={"a": "one"},
    )
    if keyword_request:
        result = await registered(context=marker, incoming=request)
    else:
        result = await registered(marker, request)
    assert result is response
    assert len(calls) == max_retries + 1
    assert all(call[0] is marker for call in calls)
    assert calls[0][1] is request
    assert calls[-1][1].route_params == {"a": "1"}
    assert retry_results == [
        *[{"status": "failed", "error_type": "ValueError"}] * (max_retries - 1),
        {"status": "succeeded"},
    ]
    runner.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_smart_retry_explicit_override_takes_precedence_over_trigger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, durable: bool,
) -> None:
    app, _ = _app(monkeypatch, tmp_path, durable=durable)
    response = func.HttpResponse("unchanged")
    request = func.HttpRequest(method="GET", url="https://example.test/", body=b"")
    calls = []

    @app.route(route="value")
    @app.smart_retry(agent_name="internal", request_arg="override")
    async def handler(req: object, override: object) -> func.HttpResponse:
        calls.append((req, override))
        return response

    function = next(item for item in app.get_functions() if item.get_function_name() == "handler")
    marker = object()
    assert await function.get_user_function()(marker, request) is response
    assert calls == [(marker, request)]


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("trigger_kind", ["queue", "timer"])
async def test_smart_retry_selects_non_http_trigger_but_preserves_adapter_rejection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, durable: bool, trigger_kind: str,
) -> None:
    from azure_functions_agents._hosted_recovery_http import RecoveryRequest

    app, _ = _app(monkeypatch, tmp_path, durable=durable)
    handler_calls = []
    runner = AsyncMock()
    monkeypatch.setattr(hosted_skill_module, "run_agent", runner)
    prepare = AsyncMock(wraps=RecoveryRequest.prepare)
    monkeypatch.setattr(RecoveryRequest, "prepare", prepare)
    trigger = (
        app.queue_trigger(arg_name="incoming", queue_name="items", connection="Storage")
        if trigger_kind == "queue"
        else app.timer_trigger(arg_name="incoming", schedule="0 */5 * * * *")
    )

    @trigger
    @app.smart_retry(agent_name="internal")
    async def handler(context: object, incoming: object) -> None:
        handler_calls.append(incoming)

    function = next(item for item in app.get_functions() if item.get_function_name() == "handler")
    payload = object()
    with pytest.raises(TypeError, match="Unsupported recovery request type"):
        await function.get_user_function()(object(), payload)
    prepare.assert_awaited_once_with(payload)
    assert not handler_calls
    runner.assert_not_called()


def test_smart_retry_rejects_trigger_absent_from_signature_at_indexing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    app, _ = _app(monkeypatch, tmp_path)

    @app.route(route="value", trigger_arg_name="missing")
    @app.smart_retry(agent_name="internal")
    async def handler(incoming: object) -> None:
        pass

    with pytest.raises(TypeError, match="visible handler parameter"):
        app.get_functions()


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_smart_retry_auto_selection_requires_indexing_before_input_or_handler_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, durable: bool,
) -> None:
    app, _ = _app(monkeypatch, tmp_path, durable=durable)
    calls = []
    prepare = AsyncMock()
    monkeypatch.setattr(hosted_app_module, "prepare_recovery_input", prepare)
    runner = AsyncMock()
    monkeypatch.setattr(hosted_skill_module, "run_agent", runner)

    @app.smart_retry(agent_name="internal")
    async def handler(req: object) -> None:
        calls.append(req)

    with pytest.raises(RuntimeError, match="index the app"):
        await handler(object())
    assert not calls
    prepare.assert_not_awaited()
    runner.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_smart_retry_shared_decorator_resolves_each_handlers_trigger_independently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, durable: bool,
) -> None:
    app, _ = _app(monkeypatch, tmp_path, durable=durable)
    retry = app.smart_retry(agent_name="internal")

    @app.route(route="first", trigger_arg_name="first_input")
    @retry
    async def first(first_input: object) -> object:
        return first_input

    @app.route(route="second", trigger_arg_name="second_input")
    @retry
    async def second(second_input: object) -> object:
        return second_input

    functions = {function.get_function_name(): function for function in app.get_functions()}
    requests = [
        func.HttpRequest(method="GET", url=f"https://example.test/{name}", body=b"")
        for name in ("first", "second")
    ]
    results = await asyncio.gather(
        functions["first"].get_user_function()(requests[0]),
        functions["second"].get_user_function()(requests[1]),
    )
    assert results[0] is requests[0]
    assert results[1] is requests[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("keyword_request", [False, True])
async def test_smart_retry_direct_override_preserves_parameter_kinds_and_default_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, durable: bool, keyword_request: bool,
) -> None:
    app, _ = _app(monkeypatch, tmp_path, durable=durable)
    calls = []
    retry_results = []
    marker = object()
    response = func.HttpResponse("corrected")

    async def execute(incoming: func.HttpRequest, context: object, label: str) -> func.HttpResponse:
        calls.append((incoming, context, label))
        if incoming.route_params["value"] != "corrected":
            raise ValueError("Needs correction")
        return response

    async def positional(
        incoming: func.HttpRequest, /, context: object, *, label: str = "default",
    ) -> func.HttpResponse:
        return await execute(incoming, context, label)

    async def keyword(
        context: object, /, *, incoming: func.HttpRequest, label: str = "default",
    ) -> func.HttpResponse:
        return await execute(incoming, context, label)

    handler = app.smart_retry(agent_name="internal", request_arg="incoming")(
        keyword if keyword_request else positional
    )

    async def run_agent(_prompt: str, **kwargs: Any) -> AgentResult:
        retry = next(tool for tool in kwargs["tools"] if tool.name == "retry_function")
        retry_results.append(await retry.invoke(arguments={"route_params": {"value": "corrected"}}))
        return AgentResult(kwargs["session_id"], "done")

    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)
    request = func.HttpRequest(
        method="GET", url="https://example.test/original", body=b"",
        route_params={"value": "original"},
    )
    if keyword_request:
        result = await handler(marker, incoming=request)
    else:
        result = await handler(request, marker)

    assert result is response
    assert retry_results == [{"status": "succeeded"}]
    assert len(calls) == 2
    assert calls[0][0] is request
    assert calls[1][0].route_params == {"value": "corrected"}
    assert all(call[1] is marker and call[2] == "default" for call in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_smart_retry_queue_indexing_retains_none_success_and_fresh_sequential_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, durable: bool,
) -> None:
    app, _ = _app(monkeypatch, tmp_path, durable=durable)
    calls = []
    runner_calls = []
    retry_results = []
    recover = AsyncMock(wraps=recovery_module.recover_handler)
    monkeypatch.setattr(hosted_app_module, "recover_handler", recover)

    @app.queue_trigger(arg_name="message", queue_name="items", connection="Storage")
    @app.smart_retry(agent_name="internal")
    async def handler(message: func.QueueMessage) -> None:
        calls.append(message)
        if message.get_body() != b"corrected":
            raise ValueError("Needs correction")

    async def run_agent(_prompt: str, **kwargs: Any) -> AgentResult:
        runner_calls.append(kwargs)
        retry = next(tool for tool in kwargs["tools"] if tool.name == "retry_function")
        retry_results.append(await retry.invoke(arguments={"body": "corrected"}))
        retry_results.append(await retry.invoke(arguments={"body": "unneeded"}))
        return AgentResult(kwargs["session_id"], "not the handler result")

    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)
    function = next(item for item in app.get_functions() if item.get_function_name() == "handler")
    registered = function.get_user_function()
    messages = [func.QueueMessage(body=b"original", id=name) for name in ("one", "two")]
    results = []
    for message in messages:
        results.append(await registered(message))

    assert results == [None, None]
    assert len(calls) == 4
    assert calls[0] is messages[0]
    assert calls[2] is messages[1]
    assert calls[1].get_body() == calls[3].get_body() == b"corrected"
    assert calls[1].id == "one"
    assert calls[3].id == "two"
    assert [message.get_body() for message in messages] == [b"original", b"original"]
    assert len(runner_calls) == 2
    assert runner_calls[0]["session_id"] != runner_calls[1]["session_id"]
    assert runner_calls[0]["tools"] is not runner_calls[1]["tools"]
    assert recover.await_count == 2
    assert recover.await_args_list[0].args[0] is not recover.await_args_list[1].args[0]
    assert retry_results == [
        {"status": "succeeded"}, {"status": "failed", "error_type": "RuntimeError"},
        {"status": "succeeded"}, {"status": "failed", "error_type": "RuntimeError"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("constructor_name", ["HostedSkillFunctionApp", "create_function_app"])
async def test_factory_created_apps_index_smart_retry_without_a_synthetic_skill_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, durable: bool, constructor_name: str,
) -> None:
    (tmp_path / "internal.agent.md").write_text(
        "---\nname: Internal\ndescription: Internal advisor.\n---\nFollow policy.\n",
        encoding="utf-8",
    )
    if durable:
        (tmp_path / "workflow.agent.md").write_text(
            "---\nname: Workflow\ndescription: Workflow coordinator.\n"
            "builtin_endpoints:\n  chat_api: true\nworkflows:\n  enabled: true\n"
            "---\nCoordinate work.\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT", "false")
    monkeypatch.setattr(
        "azure_functions_agents.harness.agent_framework._maf_runner.resolve_inference_target",
        lambda _model: None,
    )
    runner = AsyncMock()
    monkeypatch.setattr(hosted_skill_module, "run_agent", runner)
    app = getattr(agents_module, constructor_name)(tmp_path)
    response = func.HttpResponse("unchanged")
    calls = []

    @app.route(route="factory-retry", trigger_arg_name="incoming")
    @app.smart_retry(agent_name="internal")
    async def handler(incoming: func.HttpRequest) -> func.HttpResponse:
        calls.append(incoming)
        return response

    expected_type = HostedSkillDFApp if durable else HostedSkillFunctionApp
    assert type(app) is expected_type
    function = next(item for item in app.get_functions() if item.get_function_name() == "handler")
    registered = function.get_user_function()
    assert list(inspect.signature(registered).parameters) == ["incoming"]
    assert "skill" not in {binding.name for binding in function.get_bindings()}
    request = func.HttpRequest(method="GET", url="https://example.test/", body=b"")

    assert await registered(request) is response
    assert calls == [request]
    runner.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("trigger_kind", ["http", "queue"])
async def test_audit_only_success_runs_once_without_advisor_or_remediation_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    durable: bool, trigger_kind: str,
) -> None:
    app, _ = _app(monkeypatch, tmp_path, durable=durable)
    runner = AsyncMock()
    recover = AsyncMock(wraps=recovery_module.recover_handler)
    monkeypatch.setattr(hosted_skill_module, "run_agent", runner)
    monkeypatch.setattr(hosted_app_module, "recover_handler", recover)
    calls = []
    response = None if trigger_kind == "queue" else func.HttpResponse("unchanged", status_code=202)
    trigger = (
        app.queue_trigger(arg_name="incoming", queue_name="items", connection="Storage")
        if trigger_kind == "queue"
        else app.route(route="audit-success/{count}", trigger_arg_name="incoming")
    )

    @trigger
    @app.smart_retry(agent_name="internal", audit_only=True, max_retries=5)
    async def handler(incoming: Any) -> object:
        count = (
            json.loads(incoming.get_body())["count"]
            if trigger_kind == "queue" else int(incoming.route_params["count"])
        )
        calls.append((incoming, count))
        return response

    function = next(item for item in app.get_functions() if item.get_function_name() == "handler")
    incoming = _audit_input(trigger_kind, valid=True)
    with caplog.at_level(logging.WARNING, logger="azure.functions.AgentRuntime"):
        result = await function.get_user_function()(incoming)

    assert result is response
    assert len(calls) == 1
    assert calls[0][0] is incoming
    assert calls[0][1] == 1
    runner.assert_not_called()
    recover.assert_not_awaited()
    assert not _audit_records(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("trigger_kind", ["http", "head", "queue"])
@pytest.mark.parametrize("max_retries", [1, 5])
async def test_audit_only_failure_inspects_source_without_replay_or_input_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    durable: bool, trigger_kind: str, max_retries: int,
) -> None:
    app, _ = _app(monkeypatch, tmp_path, durable=durable)
    source = "def parse_count(value):\n    return int(value)\n"
    source_path = tmp_path / "recovery_fixture.py"
    source_path.write_text(source, encoding="utf-8", newline="\n")
    advice = "  " + "Inspect parse_count and supply an integer. " * 100 + "  "
    expected_advice = advice.strip()[:2000]
    calls = []
    runner_calls = []
    source_results = []
    failure_calls = []
    corrected = Mock(side_effect=AssertionError("Audit must not prepare corrected arguments"))
    adapter_type = RecoveryQueueMessage if trigger_kind == "queue" else RecoveryRequest
    original_failure = adapter_type.failure

    def failure(adapter: Any, remediation: str) -> object:
        failure_calls.append((adapter.request, remediation, len(_audit_records(caplog))))
        return original_failure(adapter, remediation)

    monkeypatch.setattr(adapter_type, "corrected_arguments", corrected)
    monkeypatch.setattr(adapter_type, "failure", failure)
    recover = AsyncMock(wraps=recovery_module.recover_handler)
    monkeypatch.setattr(hosted_app_module, "recover_handler", recover)
    trigger = (
        app.queue_trigger(arg_name="incoming", queue_name="items", connection="Storage")
        if trigger_kind == "queue"
        else app.route(route="audit-failure/{count}", trigger_arg_name="incoming")
    )

    @trigger
    @app.smart_retry(agent_name="internal", audit_only=True, max_retries=max_retries, timeout=2)
    async def handler(incoming: object, *, marker: object) -> None:
        calls.append((incoming, marker))
        raise ValueError("Original failure")

    async def run_agent(prompt: str, **kwargs: Any) -> AgentResult:
        runner_calls.append((prompt, kwargs))
        tools = {tool.name: tool for tool in kwargs["tools"]}
        source_results.append(await tools["list_files"].invoke(arguments={}))
        source_results.append(await tools["read_file"].invoke(arguments={"path": "recovery_fixture.py"}))
        source_results.append(await tools["search_source"].invoke(arguments={"query": "parse_count"}))
        return AgentResult(kwargs["session_id"], advice)

    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)
    function = next(item for item in app.get_functions() if item.get_function_name() == "handler")
    incoming = _audit_input(trigger_kind)
    marker = object()
    if trigger_kind == "queue":
        metadata = (
            incoming.id, incoming.pop_receipt, incoming.dequeue_count, incoming.insertion_time,
            incoming.expiration_time, incoming.time_next_visible,
        )
    with caplog.at_level(logging.WARNING, logger="azure.functions.AgentRuntime"):
        if trigger_kind == "queue":
            with pytest.raises(HostedSkillRecoveryError) as caught:
                await function.get_user_function()(incoming, marker=marker)
            assert caught.value.remediation == expected_advice
        else:
            result = await function.get_user_function()(incoming, marker=marker)
            assert result.status_code == 500
            if trigger_kind == "head":
                assert result.get_body() == b""
            else:
                assert json.loads(result.get_body()) == {
                    "error": "FUNCTION_RECOVERY_FAILED", "requires_user_action": True,
                    "remediation": expected_advice,
                }

    assert len(calls) == 1
    assert calls[0][0] is incoming
    assert calls[0][1] is marker
    corrected.assert_not_called()
    assert failure_calls == [(incoming, expected_advice, 1)]
    recover.assert_awaited_once()
    assert recover.await_args.kwargs["audit_only"] is True
    assert recover.await_args.kwargs["max_retries"] == max_retries
    assert len(runner_calls) == 1
    prompt, runner = runner_calls[0]
    diagnostic = json.loads(prompt.rsplit("\n", 1)[1])
    assert diagnostic["audit_only"] is True
    assert diagnostic["max_retries"] == 0
    assert diagnostic["error_type"] == "ValueError"
    assert diagnostic["handler"] == function.get_user_function().__wrapped__.__qualname__
    assert runner["instructions"] == AUDIT_INSTRUCTIONS
    assert runner["instructions"] != RECOVERY_INSTRUCTIONS
    assert prompt.startswith(AUDIT_INSTRUCTIONS + "\n")
    assert runner["timeout"] == 2
    assert runner["_harness"] is app._hosted_skill_harness
    assert runner["agent_name"] == "internal"
    assert len(runner["tools"]) == 3
    assert {tool.name for tool in runner["tools"]} == {"list_files", "read_file", "search_source"}
    for key in ("mcp_tools", "skill_paths", "skills", "skill_catalog", "sandbox_tools", "web_request_tools"):
        assert runner[key] == []
    assert source_results == [
        ["recovery_fixture.py"], source, ["recovery_fixture.py:1:def parse_count(value):"],
    ]
    assert source_path.read_text(encoding="utf-8") == source
    [record] = _audit_records(caplog)
    assert record.msg == "smart_retry audit-only remediation: %s"
    assert record.levelno == logging.WARNING
    assert record.args == (expected_advice,)
    assert record.getMessage() == "smart_retry audit-only remediation: " + expected_advice
    if trigger_kind == "queue":
        assert incoming.get_body() == b'{"count": "original"}'
        assert (
            incoming.id, incoming.pop_receipt, incoming.dequeue_count, incoming.insertion_time,
            incoming.expiration_time, incoming.time_next_visible,
        ) == metadata
        assert diagnostic["request"] == {"body": '{"count": "original"}'}
    else:
        assert incoming.method == ("HEAD" if trigger_kind == "head" else "GET")
        assert incoming.url == "https://example.test/count/original?view=full"
        assert incoming.get_body() == b""
        assert incoming.route_params == {"count": "original"}
        assert incoming.params == {"view": "full"}
        assert dict(incoming.headers) == {"x-original": "unchanged"}
    arguments = {
        "list_files": {}, "read_file": {"path": "recovery_fixture.py"},
        "search_source": {"query": "parse_count"},
    }
    for tool in runner["tools"]:
        with pytest.raises(RuntimeError, match="no longer active"):
            await tool.invoke(arguments=arguments[tool.name])


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("trigger_kind", ["http", "queue"])
@pytest.mark.parametrize("outcome", ["empty", "provider_error", "timeout"])
async def test_audit_only_advisor_failures_log_fallback_once_and_retain_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    durable: bool, trigger_kind: str, outcome: str,
) -> None:
    app, _ = _app(monkeypatch, tmp_path, durable=durable)
    calls = []
    runner_calls = []
    finished = asyncio.Event()

    @app.smart_retry(agent_name="internal", request_arg="incoming", audit_only=True, timeout=0.05)
    async def handler(incoming: object) -> None:
        calls.append(incoming)
        raise ValueError("Original failure")

    async def run_agent(_prompt: str, **kwargs: Any) -> AgentResult:
        runner_calls.append(kwargs)
        try:
            if outcome == "provider_error":
                raise RuntimeError("private provider details")
            if outcome == "timeout":
                await asyncio.Event().wait()
            return AgentResult(kwargs["session_id"], "  \n  ")
        finally:
            finished.set()

    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)
    incoming = _audit_input(trigger_kind)
    with caplog.at_level(logging.WARNING, logger="azure.functions.AgentRuntime"):
        if trigger_kind == "queue":
            with pytest.raises(HostedSkillRecoveryError) as caught:
                await handler(incoming)
            remediation = caught.value.remediation
        else:
            result = await handler(incoming)
            assert result.status_code == 500
            payload = json.loads(result.get_body())
            assert payload["error"] == "FUNCTION_RECOVERY_FAILED"
            assert payload["requires_user_action"] is True
            remediation = payload["remediation"]

    assert remediation == (
        "Recovery could not complete. Review the handler inputs and project source, then retry."
    )
    assert len(calls) == 1
    assert calls[0] is incoming
    assert len(runner_calls) == 1
    assert finished.is_set()
    [record] = _audit_records(caplog)
    assert record.levelno == logging.WARNING
    assert record.args == (remediation,)
    assert "private provider details" not in caplog.text
    assert {tool.name for tool in runner_calls[0]["tools"]} == {
        "list_files", "read_file", "search_source",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("trigger_kind", ["http", "queue"])
@pytest.mark.parametrize("cancel_at", ["handler", "advisor"])
async def test_audit_only_external_cancellation_has_no_failure_outcome_or_remediation_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    durable: bool, trigger_kind: str, cancel_at: str,
) -> None:
    app, _ = _app(monkeypatch, tmp_path, durable=durable)
    entered = asyncio.Event()
    finished = asyncio.Event()
    calls = []
    runner_calls = []
    failure = Mock(side_effect=AssertionError("Cancellation must not become adapter failure"))
    corrected = Mock(side_effect=AssertionError("Audit must not prepare corrected arguments"))
    adapter_type = RecoveryQueueMessage if trigger_kind == "queue" else RecoveryRequest
    monkeypatch.setattr(adapter_type, "failure", failure)
    monkeypatch.setattr(adapter_type, "corrected_arguments", corrected)

    @app.smart_retry(agent_name="internal", request_arg="incoming", audit_only=True, timeout=5)
    async def handler(incoming: object) -> None:
        calls.append(incoming)
        if cancel_at == "handler":
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()
        raise ValueError("Original failure")

    async def run_agent(_prompt: str, **kwargs: Any) -> AgentResult:
        runner_calls.append(kwargs)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()
        return AgentResult(kwargs["session_id"], "unreachable")

    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)
    incoming = _audit_input(trigger_kind)
    with caplog.at_level(logging.WARNING, logger="azure.functions.AgentRuntime"):
        task = asyncio.create_task(handler(incoming))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert len(calls) == 1
    assert calls[0] is incoming
    assert len(runner_calls) == (0 if cancel_at == "handler" else 1)
    assert finished.is_set()
    failure.assert_not_called()
    corrected.assert_not_called()
    assert not _audit_records(caplog)
    if runner_calls:
        source_tool = next(tool for tool in runner_calls[0]["tools"] if tool.name == "list_files")
        with pytest.raises(RuntimeError, match="no longer active"):
            await source_tool.invoke(arguments={})


@pytest.mark.asyncio
@pytest.mark.parametrize("durable", [False, True])
async def test_concurrent_audit_and_retry_wrappers_share_catalog_but_not_execution_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture, durable: bool,
) -> None:
    app, _ = _app(monkeypatch, tmp_path, durable=durable)
    entry = app._hosted_skill_catalog["internal"]
    calls = {"audit": [], "retry": []}
    runners = {}
    retry_results = []
    both_running = asyncio.Event()
    response = func.HttpResponse("corrected", status_code=202)

    def decorate(audit_only: bool):
        mode = "audit" if audit_only else "retry"

        @app.smart_retry(
            agent_name="internal", request_arg="incoming", audit_only=audit_only,
            max_retries=3, timeout=2,
        )
        async def handler(incoming: func.HttpRequest) -> func.HttpResponse:
            calls[mode].append(incoming)
            if incoming.route_params["count"] != 1:
                raise ValueError("Needs correction")
            return response

        return handler

    async def run_agent(prompt: str, **kwargs: Any) -> AgentResult:
        diagnostic = json.loads(prompt.rsplit("\n", 1)[1])
        mode = "audit" if diagnostic.get("audit_only", False) else "retry"
        runners[mode] = (diagnostic, kwargs)
        if len(runners) == 2:
            both_running.set()
        await both_running.wait()
        if mode == "retry":
            retry = next(tool for tool in kwargs["tools"] if tool.name == "retry_function")
            retry_results.append(await retry.invoke(arguments={"route_params": {"count": 1}}))
        return AgentResult(kwargs["session_id"], f"{mode} advice")

    monkeypatch.setattr(hosted_skill_module, "run_agent", run_agent)
    audit, retry = decorate(True), decorate(False)
    requests = [_audit_input("http"), _audit_input("http")]
    with caplog.at_level(logging.WARNING, logger="azure.functions.AgentRuntime"):
        audit_result, retry_result = await asyncio.wait_for(asyncio.gather(
            audit(requests[0]), retry(requests[1]),
        ), timeout=3)

    assert audit_result.status_code == 500
    assert json.loads(audit_result.get_body())["remediation"] == "audit advice"
    assert retry_result is response
    assert len(calls["audit"]) == 1
    assert calls["audit"][0] is requests[0]
    assert len(calls["retry"]) == 2
    assert calls["retry"][0] is requests[1]
    assert calls["retry"][1] is not requests[1]
    assert calls["retry"][1].route_params == {"count": 1}
    assert [incoming.route_params for incoming in requests] == [{"count": "original"}] * 2
    assert set(runners) == {"audit", "retry"}
    audit_diagnostic, audit_runner = runners["audit"]
    retry_diagnostic, retry_runner = runners["retry"]
    assert audit_diagnostic["audit_only"] is True
    assert audit_diagnostic["max_retries"] == 0
    assert retry_diagnostic["max_retries"] == 3
    assert audit_runner["instructions"] == AUDIT_INSTRUCTIONS
    assert retry_runner["instructions"] == RECOVERY_INSTRUCTIONS
    assert {tool.name for tool in audit_runner["tools"]} == {"list_files", "read_file", "search_source"}
    assert {tool.name for tool in retry_runner["tools"]} == {
        "list_files", "read_file", "search_source", "retry_function",
    }
    assert audit_runner["session_id"] != retry_runner["session_id"]
    assert audit_runner["tools"] is not retry_runner["tools"]
    assert retry_results == [{"status": "succeeded"}]
    assert app._hosted_skill_catalog["internal"] is entry
    assert entry.resolved.instructions == "Follow policy."
    [record] = _audit_records(caplog)
    assert record.args == ("audit advice",)