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
    ("field", "target", "spec_link"),
    [
        ("runtime", None, "docs/front-matter-spec.md"),
        (
            "execution_sandbox",
            "system_tools.execute_in_sessions",
            "docs/front-matter-spec.md#system_tools",
        ),
        (
            "tools_from_connections",
            "system_tools.tools_from_connections",
            "docs/front-matter-spec.md#system_tools",
        ),
    ],
)
def test_validate_agent_frontmatter_legacy_fields(
    field: str,
    target: str | None,
    spec_link: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "agent.agent.md"
    with pytest.raises(ValueError) as exc_info:
        validate_agent_frontmatter({field: True}, source)
    message = str(exc_info.value)
    assert field in message
    assert str(source) in message
    assert spec_link in message
    if target is not None:
        assert target in message
    if field == "runtime":
        assert "docs/front-matter-spec.md#system_tools" not in message


@pytest.mark.parametrize(
    ("field", "target", "spec_link"),
    [
        (
            "execution_sandbox",
            "system_tools.execute_in_sessions",
            "docs/front-matter-spec.md#system_tools",
        ),
        (
            "tools_from_connections",
            "system_tools.tools_from_connections",
            "docs/front-matter-spec.md#system_tools",
        ),
    ],
)
def test_validate_global_config_dict_legacy_fields(
    field: str,
    target: str,
    spec_link: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "agents.config.yaml"
    with pytest.raises(ValueError) as exc_info:
        validate_global_config_dict({field: True}, source)
    message = str(exc_info.value)
    assert field in message
    assert str(source) in message
    assert spec_link in message
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
    with pytest.raises(ValueError) as exc_info:
        validate_resolved_agent(resolved, all_global_mcp=[], discovered_skills=[])
    message = str(exc_info.value)
    assert "field `trigger`" in message
    assert message.count("docs/front-matter-spec.md#trigger") == 1
    assert "docs/front-matter-spec.mddocs/front-matter-spec.md#trigger" not in message


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
    with pytest.raises(ValueError) as exc_info:
        validate_resolved_agent(resolved, all_global_mcp=["known"], discovered_skills=[])
    message = str(exc_info.value)
    assert "field `mcp.exclude`" in message
    assert message.count("docs/front-matter-spec.md#mcp") == 1
    assert "docs/front-matter-spec.mddocs/front-matter-spec.md#mcp" not in message


def test_validate_global_mcp_references_no_missing_is_silent() -> None:
    """Defensive happy path: when every global MCP name is discovered, no error is raised."""
    validate_global_mcp_references(["a", "b"], ["a", "b", "c"], source_file="agents.config.yaml")


def test_validate_global_mcp_references_default_source_label() -> None:
    """Default source_file=None falls back to a <unknown> placeholder in the error."""
    with pytest.raises(ValueError) as exc_info:
        validate_global_mcp_references(["typo"], ["known"])
    assert "<unknown>" in str(exc_info.value)
    assert "typo" in str(exc_info.value)


def test_validate_resolved_agent_warns_on_unknown_skill_exclude(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Defensive: an unknown name in skills.exclude logs a warning (does NOT raise) so users
    catch typos without breaking startup."""
    source = tmp_path / "agent.agent.md"
    resolved = ResolvedAgent(
        name="A",
        description="d",
        trigger=None,
        instructions="x",
        is_main=True,
        debug=DebugConfig(),
        model=None,
        timeout=1.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        skills_exclude_names=["missing-skill"],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
        connector_specs=[],
        input_schema=None,
        response_schema=None,
        response_example=None,
        metadata={},
        source_file=str(source),
    )

    import logging

    with caplog.at_level(logging.WARNING):
        validate_resolved_agent(
            resolved,
            all_global_mcp=[],
            discovered_skills=["other-skill"],
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any("missing-skill" in msg for msg in messages)
    assert any("skills.exclude" in msg for msg in messages)


def test_validate_resolved_agent_warns_on_tool_exclude(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Defensive: tool excludes are warned (not validated) since tool registry is dynamic."""
    source = tmp_path / "agent.agent.md"
    resolved = ResolvedAgent(
        name="A",
        description="d",
        trigger=None,
        instructions="x",
        is_main=True,
        debug=DebugConfig(),
        model=None,
        timeout=1.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(exclude=["bash"]),
        tool_exclude_names=["bash"],
        sandbox_config=None,
        connector_specs=[],
        input_schema=None,
        response_schema=None,
        response_example=None,
        metadata={},
        source_file=str(source),
    )

    import logging

    with caplog.at_level(logging.WARNING):
        validate_resolved_agent(
            resolved,
            all_global_mcp=[],
            discovered_skills=[],
        )

    assert any("bash" in record.getMessage() for record in caplog.records)
