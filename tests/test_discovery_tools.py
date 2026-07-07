from __future__ import annotations

import sys
import textwrap
import types
from pathlib import Path

import pytest
from agent_framework import FunctionTool

from azure_functions_agents._function_tool import tool
from azure_functions_agents.discovery.tools import (
    clear_tool_discovery_cache,
    discover_project_tools,
    discover_user_tools,
)


def _write_tool_file(app_root: Path, name: str, body: str) -> None:
    tools_dir = app_root / "tools"
    tools_dir.mkdir()
    (tools_dir / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")


def _tool_names(tools: list[FunctionTool]) -> list[str]:
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

    first_tools = discover_user_tools(tmp_path)
    second_tools = discover_user_tools(tmp_path)

    assert _tool_names(first_tools) == ["ping"]
    assert _tool_names(second_tools) == ["ping"]
    assert counter.IMPORT_COUNT == 1


def test_discover_user_tools_normalizes_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_tool_file(tmp_path, "counter_tool", _counter_tool_source())
    counter = _set_counter_module(monkeypatch)

    first_tools = discover_user_tools(tmp_path)
    second_tools = discover_user_tools(tmp_path / ".")

    assert _tool_names(first_tools) == ["ping"]
    assert _tool_names(second_tools) == ["ping"]
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

    discovered_tools = discover_user_tools(tmp_path)
    discovered_tools.append(extra_tool)

    subsequent_tools = discover_user_tools(tmp_path)

    assert _tool_names(subsequent_tools) == ["ping"]


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
    assert discover_user_tools(tmp_path) == []


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
