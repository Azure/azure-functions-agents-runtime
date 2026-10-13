from __future__ import annotations

from pathlib import Path

import pytest

from azure_functions_agents.discovery.local_tools import discover_local_tools
from azure_functions_agents.discovery.tools import (
    clear_tool_discovery_cache,
    discover_project_tools,
)


def _write_tool(
    root: Path,
    *,
    metadata: str,
    source: str = "export function greet(name) { return `Hello, ${name}!`; }\n",
) -> Path:
    tool_dir = root / "tools" / "greet"
    tool_dir.mkdir(parents=True)
    (tool_dir / "tool.md").write_text(metadata, encoding="utf-8")
    (tool_dir / "greet.js").write_text(source, encoding="utf-8")
    return tool_dir


def _write_csharp_tool(root: Path, *, metadata: str, source: str) -> Path:
    tool_dir = root / "tools" / "lookup_order"
    tool_dir.mkdir(parents=True)
    (tool_dir / "tool.md").write_text(metadata, encoding="utf-8")
    (tool_dir / "LookupOrder.cs").write_text(source, encoding="utf-8")
    return tool_dir


def _write_go_tool(root: Path, *, metadata: str, source: str) -> Path:
    tool_dir = root / "tools" / "lookup_order"
    tool_dir.mkdir(parents=True)
    (tool_dir / "tool.md").write_text(metadata, encoding="utf-8")
    (tool_dir / "lookup_order.go").write_text(source, encoding="utf-8")
    return tool_dir


def test_discovers_markdown_described_javascript_tool(tmp_path: Path) -> None:
    tool_dir = _write_tool(
        tmp_path,
        metadata="""---
name: greet
description: Create a friendly greeting.
parameters:
  - name: name
    type: string
    description: Person to greet.
---
""",
    )

    result = discover_local_tools(tmp_path)

    assert result.failed_loads == ()
    assert len(result.tools) == 1
    [tool] = result.tools
    assert tool.name == "greet"
    assert tool.entrypoint == "greet"
    assert tool.metadata_path == (tool_dir / "tool.md").resolve()
    assert tool.source_path == (tool_dir / "greet.js").resolve()
    assert tool.input_schema() == {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Person to greet.",
            }
        },
        "additionalProperties": False,
        "required": ["name"],
    }


def test_invalid_tool_folder_is_isolated(tmp_path: Path) -> None:
    tool_dir = _write_tool(
        tmp_path,
        metadata="""---
name: greet
description: Create a friendly greeting.
unknown: value
---
This body is not allowed.
""",
    )
    (tool_dir / "other.js").write_text("export const other = 1;\n", encoding="utf-8")

    result = discover_local_tools(tmp_path)

    assert result.tools == ()
    assert len(result.failed_loads) == 1
    assert result.failed_loads[0][0] == str(tool_dir)
    assert "exactly one" in result.failed_loads[0][1]


def test_discovers_markdown_described_csharp_tool(tmp_path: Path) -> None:
    tool_dir = _write_csharp_tool(
        tmp_path,
        metadata="""---
name: lookup_order
description: Look up an order.
entrypoint: OrderTools.LookupOrder
parameters:
  - name: order_id
    type: string
    description: Order identifier.
---
""",
        source=(
            "public static class OrderTools { "
            "public static object LookupOrder(string order_id) => new { order_id }; }\n"
        ),
    )

    result = discover_local_tools(tmp_path)

    assert result.failed_loads == ()
    [tool] = result.tools
    assert tool.language.value == "csharp"
    assert tool.entrypoint == "OrderTools.LookupOrder"
    assert tool.source_path == (tool_dir / "LookupOrder.cs").resolve()


def test_csharp_tool_requires_qualified_entrypoint(tmp_path: Path) -> None:
    _write_csharp_tool(
        tmp_path,
        metadata="---\nname: lookup_order\ndescription: Look up an order.\n---\n",
        source="public static class OrderTools {}\n",
    )

    result = discover_local_tools(tmp_path)

    assert result.tools == ()
    assert "TypeName.MethodName" in result.failed_loads[0][1]


def test_discovers_markdown_described_go_tool(tmp_path: Path) -> None:
    tool_dir = _write_go_tool(
        tmp_path,
        metadata="""---
name: lookup_order
description: Look up an order.
entrypoint: LookupOrder
parameters:
  - name: order_id
    type: string
    description: Order identifier.
---
""",
        source=(
            'package main\n\nfunc LookupOrder(orderID string) any { '
            'return map[string]any{"order_id": orderID} }\n'
        ),
    )

    result = discover_local_tools(tmp_path)

    assert result.failed_loads == ()
    [tool] = result.tools
    assert tool.language.value == "go"
    assert tool.entrypoint == "LookupOrder"
    assert tool.source_path == (tool_dir / "lookup_order.go").resolve()


def test_go_tool_requires_explicit_exported_entrypoint(tmp_path: Path) -> None:
    _write_go_tool(
        tmp_path,
        metadata="---\nname: lookup_order\ndescription: Look up an order.\n---\n",
        source="package main\nfunc LookupOrder() {}\n",
    )

    result = discover_local_tools(tmp_path)

    assert result.tools == ()
    assert "entrypoint" in result.failed_loads[0][1]


def test_go_tool_requires_package_main(tmp_path: Path) -> None:
    _write_go_tool(
        tmp_path,
        metadata=(
            "---\nname: lookup_order\ndescription: Look up an order.\n"
            "entrypoint: LookupOrder\n---\n"
        ),
        source="package tool\nfunc LookupOrder() {}\n",
    )

    result = discover_local_tools(tmp_path)

    assert result.tools == ()
    assert "package main" in result.failed_loads[0][1]


def test_mixed_supported_sources_are_rejected(tmp_path: Path) -> None:
    tool_dir = _write_tool(
        tmp_path,
        metadata="---\nname: greet\ndescription: Greeting.\n---\n",
    )
    (tool_dir / "Greet.cs").write_text("public static class Greet {}\n", encoding="utf-8")

    result = discover_local_tools(tmp_path)

    assert result.tools == ()
    assert "found 2" in result.failed_loads[0][1]


@pytest.mark.parametrize(
    ("metadata", "diagnostic"),
    [
        (
            "---\nname: greet\ndescription: Greeting.\nunknown: value\n---\n",
            "Extra inputs are not permitted",
        ),
        (
            "---\nname: greet\ndescription: Greeting.\n---\nBody is not allowed.\n",
            "Markdown body must be empty",
        ),
        (
            """---
name: greet
description: Greeting.
parameters:
  - name: value
    type: string
    description: First value.
  - name: value
    type: integer
    description: Duplicate value.
---
""",
            "duplicate parameter names",
        ),
    ],
)
def test_contract_validation_reports_invalid_metadata(
    tmp_path: Path, metadata: str, diagnostic: str
) -> None:
    _write_tool(tmp_path, metadata=metadata)

    result = discover_local_tools(tmp_path)

    assert result.tools == ()
    assert diagnostic in result.failed_loads[0][1]


def test_project_tool_discovery_includes_local_declarations(tmp_path: Path) -> None:
    _write_tool(
        tmp_path,
        metadata="""---
name: greet
description: Create a friendly greeting.
---
""",
        source="export function greet() { return 'Hello!'; }\n",
    )
    clear_tool_discovery_cache()

    result = discover_project_tools(tmp_path)

    assert [tool.name for tool in result.local_tool_specs] == ["greet"]
    clear_tool_discovery_cache()


def test_config_scenario_discovers_javascript_local_tool() -> None:
    fixture = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "config_scenarios"
        / "25_javascript_local_tools"
    )
    clear_tool_discovery_cache()

    result = discover_project_tools(fixture)

    assert [tool.name for tool in result.local_tool_specs] == ["lookup_order"]
    clear_tool_discovery_cache()


def test_config_scenario_discovers_csharp_local_tool() -> None:
    fixture = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "config_scenarios"
        / "26_csharp_local_tools"
    )
    clear_tool_discovery_cache()

    result = discover_project_tools(fixture)

    assert [tool.name for tool in result.local_tool_specs] == ["lookup_order"]
    assert result.local_tool_specs[0].language.value == "csharp"
    clear_tool_discovery_cache()


def test_config_scenario_discovers_go_local_tool() -> None:
    fixture = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "config_scenarios"
        / "27_go_local_tools"
    )
    clear_tool_discovery_cache()

    result = discover_project_tools(fixture)

    assert [tool.name for tool in result.local_tool_specs] == ["lookup_order"]
    assert result.local_tool_specs[0].language.value == "go"
    clear_tool_discovery_cache()
