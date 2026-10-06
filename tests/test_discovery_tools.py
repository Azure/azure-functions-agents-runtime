from __future__ import annotations

import sys
import textwrap
import threading
import types
from pathlib import Path

import pytest
from pydantic import ValidationError

from azure_functions_agents._function_tool import tool, workflow_tool
from azure_functions_agents._tool_descriptor import ToolDescriptor
from azure_functions_agents.discovery.tools import (
    _workflow_tool_from_member,
    clear_tool_discovery_cache,
    discover_project_tools,
    discover_user_tools,
)


def _write_tool_file(app_root: Path, name: str, body: str) -> None:
    tools_dir = app_root / "tools"
    tools_dir.mkdir(exist_ok=True)
    (tools_dir / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")


def _tool_names(tools: list[ToolDescriptor]) -> list[str]:
    return sorted(tool_obj.name for tool_obj in tools)


@tool
def extra_tool() -> str:
    return "extra"


def _counter_module() -> types.ModuleType:
    module = types.ModuleType("azure_functions_agents._test_tool_counter")
    module.IMPORT_COUNT = 0
    return module


def _counter_tool_source() -> str:
    return """
    import azure_functions_agents._test_tool_counter as _c
    _c.IMPORT_COUNT += 1

    from azure_functions_agents._function_tool import tool

    @tool
    def ping() -> str:
        return "pong"
    """


def _set_counter_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    counter = _counter_module()
    monkeypatch.setitem(sys.modules, "azure_functions_agents._test_tool_counter", counter)
    return counter


@pytest.fixture(autouse=True)
def clear_discovery_cache() -> None:
    clear_tool_discovery_cache()
    yield
    clear_tool_discovery_cache()


def test_discover_user_tools_caches_imports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_tool_file(tmp_path, "counter_tool", _counter_tool_source())
    counter = _set_counter_module(monkeypatch)

    first_result = discover_user_tools(tmp_path)
    second_result = discover_user_tools(tmp_path)

    assert _tool_names(first_result.tools) == ["ping"]
    assert _tool_names(second_result.tools) == ["ping"]
    assert counter.IMPORT_COUNT == 1


def test_discover_user_tools_normalizes_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_tool_file(tmp_path, "counter_tool", _counter_tool_source())
    counter = _set_counter_module(monkeypatch)

    first_result = discover_user_tools(tmp_path)
    second_result = discover_user_tools(tmp_path / ".")

    assert _tool_names(first_result.tools) == ["ping"]
    assert _tool_names(second_result.tools) == ["ping"]
    assert counter.IMPORT_COUNT == 1


def test_discover_user_tools_returns_independent_lists(tmp_path: Path) -> None:
    _write_tool_file(
        tmp_path,
        "sample_tool",
        """
        from azure_functions_agents._function_tool import tool

        @tool
        def ping() -> str:
            return "pong"
        """,
    )

    first_result = discover_user_tools(tmp_path)
    first_result.tools.append(extra_tool)

    second_result = discover_user_tools(tmp_path)

    assert _tool_names(second_result.tools) == ["ping"]


def test_clear_tool_discovery_cache_reruns_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_tool_file(tmp_path, "counter_tool", _counter_tool_source())
    counter = _set_counter_module(monkeypatch)

    discover_user_tools(tmp_path)
    clear_tool_discovery_cache()
    discover_user_tools(tmp_path)

    assert counter.IMPORT_COUNT == 2


def test_discover_user_tools_returns_empty_when_tools_dir_missing(tmp_path: Path) -> None:
    result = discover_user_tools(tmp_path)
    assert result.tools == []
    assert result.failed_loads == []


def test_discovery_records_neutral_metadata_without_maf_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from azure_functions_agents.harness.agent_framework import _maf_tools

    def forbidden(**kwargs):
        raise AssertionError("Discovery must not construct MAF tools")

    monkeypatch.setattr(_maf_tools, "FunctionTool", forbidden)
    _write_tool_file(
        tmp_path,
        "ordinary",
        """
        from azure_functions_agents import tool

        @tool(name="selected", description="Authored description")
        def lookup(value: int) -> int:
            return value
        """,
    )
    [descriptor] = discover_project_tools(tmp_path).user_tools

    assert type(descriptor) is ToolDescriptor
    assert descriptor.name == "selected"
    assert descriptor.description == "Authored description"
    assert descriptor.parameters()["properties"]["value"]["type"] == "integer"


def test_raw_maf_tools_are_ignored_in_discovery(tmp_path: Path, caplog) -> None:
    _write_tool_file(
        tmp_path,
        "legacy",
        """
        from agent_framework import FunctionTool

        class ExtendedTool(FunctionTool):
            pass

        lookup = ExtendedTool(name="legacy_lookup", func=lambda value: value)
        """,
    )
    assert discover_project_tools(tmp_path).user_tools == []
    assert "Ignoring unsupported custom tool" in caplog.text
    assert "legacy_lookup" not in caplog.text


@pytest.mark.parametrize("subclass", [False, True])
@pytest.mark.parametrize("stacked", [False, True])
def test_workflow_member_rejects_raw_sdk_before_metadata(
    subclass: bool, stacked: bool, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    from agent_framework import FunctionTool

    from azure_functions_agents.discovery import tools as discovery_tools

    class OpaqueTool(FunctionTool):
        def __getattribute__(self, name):
            raise AssertionError("No SDK attributes may be inspected")

    raw = (
        object.__new__(OpaqueTool)
        if subclass
        else FunctionTool(name="sensitive_sdk_name", func=lambda value: value)
    )
    if stacked:
        assert workflow_tool(name="sensitive_workflow_name")(raw) is raw
    caplog.clear()

    def forbidden_metadata(target):
        raise AssertionError("SDK rejection must precede workflow metadata extraction")

    monkeypatch.setattr(discovery_tools, "get_workflow_tool_metadata", forbidden_metadata)

    assert _workflow_tool_from_member(__name__, "sensitive_member_name", raw) is None
    assert [record.getMessage() for record in caplog.records] == [
        "Ignoring unsupported custom tool; use the runtime @tool decorator "
        "or a local public function in tools/."
    ]


def test_discovery_ignores_sdk_attributes_and_keeps_local_fallback(tmp_path: Path) -> None:
    _write_tool_file(
        tmp_path,
        "opaque",
        """
        from agent_framework import FunctionTool

        class OpaqueTool(FunctionTool):
            def __getattribute__(self, name):
                raise AssertionError("No SDK attributes may be inspected")

        raw = object.__new__(OpaqueTool)

        def local(value: str) -> str:
            return value
        """,
    )
    discovered = discover_project_tools(tmp_path)
    assert discovered.failed_loads == []
    assert _tool_names(discovered.user_tools) == ["local"]


@pytest.mark.parametrize("copilot", [False, True])
def test_runtime_only_authoring_scenario(copilot, monkeypatch, caplog) -> None:
    from azure_functions_agents.app import create_function_app
    from azure_functions_agents.config.loader import load_agent_specs, load_global_config
    from azure_functions_agents.config.merge import compose
    from azure_functions_agents.registration.capabilities import build_capabilities

    root = Path(__file__).parent / "fixtures" / "config_scenarios" / "24_runtime_tool_authoring"
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT", str(copilot).lower())
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-only")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_MODEL", "fixture-model")
    for name in ("WEBSITE_INSTANCE_ID", "FUNCTIONS_WORKER_PROCESS_COUNT"):
        monkeypatch.delenv(name, raising=False)
    discovered = discover_project_tools(root)
    assert discovered.failed_loads == []
    assert _tool_names(discovered.user_tools) == ["authored", "first_local"]
    assert sorted(item.name for item in discovered.workflow_tools) == ["authored", "workflow_only"]
    resolved = compose(
        load_agent_specs(root)[0], load_global_config(root),
        discovered_mcp_names=[], discovered_skill_names=[],
    )
    capabilities = build_capabilities(
        resolved, discovered_user_tools=discovered.user_tools,
        discovered_workflow_tools=discovered.workflow_tools,
        discovered_mcp_tools={}, discovered_skills={},
    )
    assert _tool_names(list(capabilities.filtered_user_tools)) == ["authored", "first_local"]
    assert create_function_app(root).get_functions()
    assert "Ignoring unsupported custom tool" in caplog.text


def test_workflow_tool_only_is_not_normal_user_tool(tmp_path: Path) -> None:
    _write_tool_file(
        tmp_path,
        "mixed_tools",
        """
        from azure_functions_agents import workflow_tool

        @workflow_tool(description="Workflow only")
        def fetch_logs(args: dict[str, object]) -> dict[str, object]:
            return {"args": args}

        def plain_tool() -> str:
            return "normal"
        """,
    )

    discovered = discover_project_tools(tmp_path)

    assert _tool_names(discovered.user_tools) == ["plain_tool"]
    assert [tool.name for tool in discovered.workflow_tools] == ["fetch_logs"]


def test_dual_decorator_tool_then_workflow_tool_is_both(tmp_path: Path) -> None:
    _write_tool_file(
        tmp_path,
        "shared_tool",
        """
        from azure_functions_agents import tool, workflow_tool

        @tool
        @workflow_tool(description="Shared")
        def shared(args: dict[str, object]) -> dict[str, object]:
            return {"args": args}
        """,
    )

    discovered = discover_project_tools(tmp_path)

    assert _tool_names(discovered.user_tools) == ["shared"]
    assert [tool.name for tool in discovered.workflow_tools] == ["shared"]


def test_dual_decorator_workflow_tool_then_tool_is_both(tmp_path: Path) -> None:
    _write_tool_file(
        tmp_path,
        "shared_tool",
        """
        from azure_functions_agents import tool, workflow_tool

        @workflow_tool(description="Shared")
        @tool
        def shared(args: dict[str, object]) -> dict[str, object]:
            return {"args": args}
        """,
    )

    discovered = discover_project_tools(tmp_path)

    assert _tool_names(discovered.user_tools) == ["shared"]
    assert [tool.name for tool in discovered.workflow_tools] == ["shared"]


def test_dual_decorator_with_schema_tool_then_workflow_tool_is_both(tmp_path: Path) -> None:
    _write_tool_file(
        tmp_path,
        "shared_tool",
        """
        from pydantic import BaseModel

        from azure_functions_agents import tool, workflow_tool

        class SharedArgs(BaseModel):
            value: str

        @tool(schema=SharedArgs)
        @workflow_tool(description="Shared schema tool")
        def shared(args: SharedArgs) -> dict[str, str]:
            return {"value": args.value}
        """,
    )

    discovered = discover_project_tools(tmp_path)

    assert _tool_names(discovered.user_tools) == ["shared"]
    assert discovered.user_tools[0].input_model.__name__ == "SharedArgs"
    [workflow_tool] = discovered.workflow_tools
    assert workflow_tool.name == "shared"
    assert workflow_tool.description == "Shared schema tool"
    assert workflow_tool.handler is not None


def test_dual_decorator_with_schema_workflow_tool_then_tool_is_both(tmp_path: Path) -> None:
    _write_tool_file(
        tmp_path,
        "shared_tool",
        """
        from pydantic import BaseModel

        from azure_functions_agents import tool, workflow_tool

        class SharedArgs(BaseModel):
            value: str

        @workflow_tool(description="Shared schema tool")
        @tool(schema=SharedArgs)
        def shared(args: SharedArgs) -> dict[str, str]:
            return {"value": args.value}
        """,
    )

    discovered = discover_project_tools(tmp_path)

    assert _tool_names(discovered.user_tools) == ["shared"]
    assert discovered.user_tools[0].input_model.__name__ == "SharedArgs"
    [workflow_tool] = discovered.workflow_tools
    assert workflow_tool.name == "shared"
    assert workflow_tool.description == "Shared schema tool"
    assert workflow_tool.handler is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("is_async", [False, True])
@pytest.mark.parametrize("workflow_first", [False, True])
async def test_dual_schema_tool_runs_as_workflow(
    tmp_path: Path, is_async: bool, workflow_first: bool
) -> None:
    from azure_functions_agents.workflows import activity, integration

    decorators = ["@tool(schema=SharedArgs)", "@workflow_tool"]
    if workflow_first:
        decorators.reverse()
    declaration = "async def" if is_async else "def"
    _write_tool_file(
        tmp_path,
        "shared_tool",
        f"""
        import asyncio
        import threading
        from pydantic import BaseModel
        from azure_functions_agents import tool, workflow_tool

        class SharedArgs(BaseModel):
            value: str

        {decorators[0]}
        {decorators[1]}
        {declaration} shared(args: SharedArgs) -> dict[str, object]:
            {"await asyncio.sleep(0)" if is_async else "pass"}
            return {{"value": args.value, "thread": threading.get_ident()}}
        """,
    )

    discovered = discover_project_tools(tmp_path)
    catalog = integration.build_workflow_handler_catalog(discovered.workflow_tools)
    handler = catalog["shared"].handler
    result = await activity.invoke_handler(handler, {"value": "ok"})

    assert result["value"] == "ok"
    assert (result["thread"] == threading.get_ident()) is is_async
    with pytest.raises(ValidationError):
        await activity.invoke_handler(handler, {})
    assert (await discovered.user_tools[0].func(value="direct"))["value"] == "direct"


def test_workflow_tool_public_false_flows_through_discovery(tmp_path: Path) -> None:
    _write_tool_file(
        tmp_path,
        "private_workflow_tool",
        """
        from azure_functions_agents import workflow_tool

        @workflow_tool(name="private_lookup", description="Internal lookup", public=False)
        def lookup(args: dict[str, object]) -> dict[str, object]:
            return {"args": args}
        """,
    )

    discovered = discover_project_tools(tmp_path)

    assert discovered.user_tools == []
    [workflow_tool] = discovered.workflow_tools
    assert workflow_tool.name == "private_lookup"
    assert workflow_tool.description == "Internal lookup"
    assert workflow_tool.public is False
    assert workflow_tool.handler is not None


def test_workflow_tool_retry_flows_through_discovery(tmp_path: Path) -> None:
    _write_tool_file(
        tmp_path,
        "retrying_workflow_tool",
        """
        from azure_functions_agents import (
            WorkflowRetryBackoff,
            WorkflowRetryPolicy,
            workflow_tool,
        )

        @workflow_tool(
            retry=WorkflowRetryPolicy(
                max_attempts=3,
                backoff=WorkflowRetryBackoff(
                    initial="PT1S",
                    multiplier=2.0,
                    max="PT4S",
                ),
            )
        )
        def reserve(args: dict[str, object]) -> dict[str, object]:
            return {"args": args}
        """,
    )

    [workflow_tool] = discover_project_tools(tmp_path).workflow_tools

    assert workflow_tool.retry is not None
    assert workflow_tool.retry.max_attempts == 3
    assert workflow_tool.retry.backoff is not None
    assert workflow_tool.retry.backoff.max == "PT4S"


def test_workflow_tool_timeout_flows_through_discovery(tmp_path: Path) -> None:
    _write_tool_file(
        tmp_path,
        "timed_workflow_tool",
        """
        from azure_functions_agents import workflow_tool

        @workflow_tool(timeout="PT20S")
        def reserve(args: dict[str, object]) -> dict[str, object]:
            return {"args": args}
        """,
    )

    [workflow_tool] = discover_project_tools(tmp_path).workflow_tools

    assert workflow_tool.timeout == "PT20S"


def test_workflow_tool_rejects_invalid_retry_type() -> None:
    with pytest.raises(TypeError, match="WorkflowRetryPolicy"):
        workflow_tool(retry="three attempts")  # type: ignore[arg-type]


def test_workflow_tool_rejects_continue_on_error() -> None:
    with pytest.raises(TypeError, match=r"unknown workflow_tool argument.*continue_on_error"):
        workflow_tool(continue_on_error=True)  # type: ignore[call-overload]


@pytest.mark.parametrize("timeout", ["PT0.5S", "PT11M", "not a duration"])
def test_workflow_tool_rejects_invalid_timeout(timeout: str) -> None:
    with pytest.raises(TypeError, match="workflow_tool timeout is invalid"):
        workflow_tool(timeout=timeout)


def test_async_workflow_tool_flows_into_handler_catalog(tmp_path: Path) -> None:
    from azure_functions_agents.workflows import integration

    _write_tool_file(
        tmp_path,
        "async_workflow_tool",
        """
        from azure_functions_agents import workflow_tool

        @workflow_tool(description="Fetch a record.")
        async def fetch_record(args: dict[str, object]) -> dict[str, object]:
            return {"args": args}
        """,
    )

    [discovered] = discover_project_tools(tmp_path).workflow_tools
    catalog = integration.build_workflow_handler_catalog([discovered])

    assert list(catalog) == ["fetch_record"]
    assert catalog["fetch_record"].handler is discovered.handler


def test_multiple_workflow_tools_can_be_declared_in_one_file(tmp_path: Path) -> None:
    _write_tool_file(
        tmp_path,
        "workflow_tools",
        """
        from azure_functions_agents import workflow_tool

        @workflow_tool
        def fetch_logs(args: dict[str, object]) -> dict[str, object]:
            return {"logs": args}

        @workflow_tool
        def fetch_metrics(args: dict[str, object]) -> dict[str, object]:
            return {"metrics": args}

        def _helper() -> str:
            return "not a tool"
        """,
    )

    discovered = discover_project_tools(tmp_path)

    assert discovered.user_tools == []
    assert [tool.name for tool in discovered.workflow_tools] == [
        "fetch_logs",
        "fetch_metrics",
    ]


def test_discover_user_tools_tracks_failed_loads(tmp_path: Path) -> None:
    """Test that failed tool loads are tracked and reported."""
    _write_tool_file(
        tmp_path,
        "broken_tool",
        """
        # This will fail with a syntax error
        def broken(
        """,
    )
    _write_tool_file(
        tmp_path,
        "good_tool",
        """
        from azure_functions_agents._function_tool import tool

        @tool
        def working() -> str:
            return "ok"
        """,
    )

    result = discover_user_tools(tmp_path)

    # Should have 1 success and 1 failure
    assert len(result.tools) == 1
    assert result.tools[0].name == "working"
    assert len(result.failed_loads) == 1
    assert "broken_tool.py" in result.failed_loads[0][0]
    assert "SyntaxError" in result.failed_loads[0][1]
