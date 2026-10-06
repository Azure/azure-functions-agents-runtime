from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, get_type_hints

import azure.durable_functions as df
import azure.functions as func
import pytest

import azure_functions_agents._hosted_skill_app as hosted_app_module
from azure_functions_agents import HostedSkill
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
) -> tuple[HostedSkillFunctionApp, list[str | None]]:
    validated_models: list[str | None] = []

    class Manager:
        def validate_provider_settings(self, model: str | None) -> None:
            validated_models.append(model)

    monkeypatch.setattr(hosted_app_module, "get_client_manager", lambda: Manager())
    catalog = build_catalog({"internal": entry or _entry(tmp_path)})
    harness = AppHarness(harness_kind, tmp_path)
    return HostedSkillFunctionApp(catalog=catalog, harness=harness), validated_models


@pytest.mark.asyncio
async def test_decorator_hides_parameter_and_injects_fresh_facades(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app, validated_models = _app(monkeypatch, tmp_path)

    @app.route(route="orders")
    @app.hosted_skill(arg_name="skill", agent_name="internal")
    async def handler(req: func.HttpRequest, skill: HostedSkill) -> HostedSkill:
        return skill

    assert validated_models == ["model-one"]
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


def test_decorator_rejects_copilot_preview(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app, _ = _app(monkeypatch, tmp_path, harness_kind=HarnessKind.COPILOT)

    with pytest.raises(ValueError, match="Copilot preview"):
        app.hosted_skill(arg_name="skill", agent_name="internal")


def test_enhanced_apps_preserve_sdk_base_types(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        hosted_app_module,
        "get_client_manager",
        lambda: None,
    )
    catalog = build_catalog({"internal": _entry(tmp_path)})
    harness = AppHarness(HarnessKind.MAF, tmp_path)

    function_app = HostedSkillFunctionApp(catalog=catalog, harness=harness)
    durable_app = HostedSkillDFApp(catalog=catalog, harness=harness)

    assert isinstance(function_app, func.FunctionApp)
    assert isinstance(durable_app, df.DFApp)