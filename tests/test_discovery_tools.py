from __future__ import annotations

import sys
import textwrap
import types
from pathlib import Path

import pytest
from agent_framework import FunctionTool

from azure_functions_agents._function_tool import tool
from azure_functions_agents.discovery.tools import clear_tool_discovery_cache, discover_user_tools


def _write_tool_file(app_root: Path, name: str, body: str) -> None:
    tools_dir = app_root / "tools"
    tools_dir.mkdir(exist_ok=True)
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
