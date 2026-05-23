from __future__ import annotations

from pathlib import Path

import pytest

from azure_functions_agents.config.schema import (
    DebugConfig,
    ResolvedAgent,
    ToolsFilter,
    TriggerSpec,
)
from azure_functions_agents.config.validation import validate_resolved_agent


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
        validate_resolved_agent(resolved, discovered_mcp_names=[], discovered_skills=[])
    message = str(exc_info.value)
    assert "field `trigger`" in message
    assert message.count("docs/front-matter-spec.md#trigger") == 1
    assert "docs/front-matter-spec.mddocs/front-matter-spec.md#trigger" not in message


@pytest.mark.parametrize(
    ("trigger_type", "expected"),
    [
        ("activity_trigger", "Durable Functions triggers are not supported"),
        ("orchestration_trigger", "Durable Functions triggers are not supported"),
        ("entity_trigger", "Durable Functions triggers are not supported"),
        ("warm_up_trigger", "Warm-up triggers are host lifecycle hooks"),
        ("route", "Use `http_trigger` instead"),
        ("schedule", "Use `timer_trigger` instead"),
        ("assistant_skill_trigger", "Assistant skill triggers are not supported"),
        ("mcp_tool_trigger", "MCP tool triggers are registered"),
        ("mcp_resource_trigger", "MCP resource triggers are registered"),
        ("mcp_prompt_trigger", "MCP prompt triggers are registered"),
        ("connector_trigger", "Use dotted connector trigger types instead"),
    ],
)
def test_validate_resolved_agent_rejects_unsupported_trigger_types(
    trigger_type: str,
    expected: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "report.agent.md"
    resolved = ResolvedAgent(
        name="Report",
        description="desc",
        trigger=TriggerSpec(type=trigger_type, args={}),
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
        validate_resolved_agent(resolved, discovered_mcp_names=[], discovered_skills=[])

    message = str(exc_info.value)
    assert "field `trigger.type`" in message
    assert expected in message
    assert message.count("docs/front-matter-spec.md#trigger") == 1


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
        enabled_mcp_names=["known"],
        enabled_skills_names=[],
        mcp_exclude_names=["missing"],
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
        validate_resolved_agent(
            resolved, discovered_mcp_names=["known"], discovered_skills=[]
        )
    message = str(exc_info.value)
    assert "field `mcp.exclude`" in message
    assert message.count("docs/front-matter-spec.md#mcp") == 1
    assert "docs/front-matter-spec.mddocs/front-matter-spec.md#mcp" not in message


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
            discovered_mcp_names=[],
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
            discovered_mcp_names=[],
            discovered_skills=[],
        )

    assert any("bash" in record.getMessage() for record in caplog.records)
