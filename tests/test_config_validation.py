from __future__ import annotations

from pathlib import Path

import pytest

from azure_functions_agents.config.schema import DebugConfig, ResolvedAgent, ToolsFilter
from azure_functions_agents.config.validation import (
    validate_agent_frontmatter,
    validate_global_config_dict,
    validate_global_mcp_references,
    validate_resolved_agent,
)


@pytest.mark.parametrize(
    ("field", "target"),
    [
        ("runtime", None),
        ("execution_sandbox", "system_tools.execute_in_sessions"),
        ("tools_from_connections", "system_tools.tools_from_connections"),
    ],
)
def test_validate_agent_frontmatter_legacy_fields(
    field: str,
    target: str | None,
    tmp_path: Path,
) -> None:
    source = tmp_path / "agent.agent.md"
    with pytest.raises(ValueError) as exc_info:
        validate_agent_frontmatter({field: True}, source)
    message = str(exc_info.value)
    assert field in message
    assert str(source) in message
    assert "docs/front-matter-spec.md" in message
    if target is not None:
        assert target in message


@pytest.mark.parametrize(
    ("field", "target"),
    [
        ("execution_sandbox", "system_tools.execute_in_sessions"),
        ("tools_from_connections", "system_tools.tools_from_connections"),
    ],
)
def test_validate_global_config_dict_legacy_fields(
    field: str,
    target: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "agents.config.yaml"
    with pytest.raises(ValueError) as exc_info:
        validate_global_config_dict({field: True}, source)
    message = str(exc_info.value)
    assert field in message
    assert str(source) in message
    assert "docs/front-matter-spec.md" in message
    assert target in message


def test_validate_global_mcp_references(tmp_path: Path) -> None:
    source = tmp_path / "agents.config.yaml"
    mcp_json = tmp_path / "mcp.json"
    source.write_text("mcp:\n  - typo-server\n", encoding="utf-8")
    mcp_json.write_text('{"servers":{"known-server":{}}}', encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        validate_global_mcp_references(
            ["typo-server"],
            ["known-server"],
            source_file=source,
        )

    message = str(exc_info.value)
    assert str(source) in message
    assert "typo-server" in message
    assert "mcp.json" in message
    assert "docs/front-matter-spec.md#mcp" in message


def test_validate_resolved_agent_requires_trigger_for_non_main(
    tmp_path: Path,
) -> None:
    source = tmp_path / "report.agent.md"
    resolved = ResolvedAgent(
        name="Report",
        description="desc",
        trigger=None,
        instructions="x",
        is_main=False,
        debug=DebugConfig(),
        model=None,
        timeout=1.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
        connector_specs=[],
        input_schema=None,
        response_schema=None,
        response_example=None,
        metadata={},
        source_file=str(source),
    )
    with pytest.raises(ValueError, match=r"trigger"):
        validate_resolved_agent(resolved, all_global_mcp=[], discovered_skills=[])


def test_validate_resolved_agent_rejects_unknown_mcp_exclude(
    tmp_path: Path,
) -> None:
    source = tmp_path / "report.agent.md"
    resolved = ResolvedAgent(
        name="Report",
        description="desc",
        trigger=None,
        instructions="x",
        is_main=True,
        debug=DebugConfig(),
        model=None,
        timeout=1.0,
        enabled_mcp_names=["missing"],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
        connector_specs=[],
        input_schema=None,
        response_schema=None,
        response_example=None,
        metadata={},
        source_file=str(source),
    )
    with pytest.raises(ValueError, match=r"mcp\.exclude"):
        validate_resolved_agent(resolved, all_global_mcp=["known"], discovered_skills=[])
