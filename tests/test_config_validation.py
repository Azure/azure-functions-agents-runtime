from __future__ import annotations

from pathlib import Path

import pytest

from azure_functions_agents.config.schema import (
    BuiltinEndpointsConfig,
    ResolvedAgent,
    SubagentRef,
    ToolsFilter,
    TriggerSpec,
)
from azure_functions_agents.config.validation import (
    validate_resolved_agent,
    validate_subagent_references,
)


def _make_resolved(**overrides: object) -> ResolvedAgent:
    """Build a minimal ResolvedAgent for validation tests, with sensible defaults.

    Only used by the newer subagents-focused tests below; existing tests in this
    file intentionally keep constructing ResolvedAgent(...) inline for clarity.
    """
    defaults: dict[str, object] = dict(
        name="Agent",
        slug="agent",
        description="desc",
        trigger=None,
        instructions="x",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(),
        model=None,
        timeout=1.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        subagents=[],
        sandbox_config=None,
        input_schema=None,
        response_schema=None,
        response_example=None,
        metadata={},
        source_file="agent.agent.md",
    )
    defaults.update(overrides)
    return ResolvedAgent(**defaults)  # type: ignore[arg-type]


def test_validate_resolved_agent_requires_trigger_when_no_builtin_endpoints(
    tmp_path: Path,
) -> None:
    source = tmp_path / "report.agent.md"
    resolved = ResolvedAgent(
        name="Report",
        description="desc",
        trigger=None,
        instructions="x",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(),
        model=None,
        timeout=1.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
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
    "builtin_endpoints",
    [
        BuiltinEndpointsConfig(debug_chat_ui=True),
        BuiltinEndpointsConfig(chat_api=True),
        BuiltinEndpointsConfig(mcp=True),
    ],
)
def test_validate_resolved_agent_allows_missing_trigger_with_builtin_endpoints(
    builtin_endpoints: BuiltinEndpointsConfig,
    tmp_path: Path,
) -> None:
    source = tmp_path / "endpoint.agent.md"
    resolved = ResolvedAgent(
        name="Endpoint Agent",
        description="desc",
        trigger=None,
        instructions="x",
        is_main=False,
        builtin_endpoints=builtin_endpoints,
        model=None,
        timeout=1.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
        input_schema=None,
        response_schema=None,
        response_example=None,
        metadata={},
        source_file=str(source),
    )

    validate_resolved_agent(resolved, discovered_mcp_names=[], discovered_skills=[])


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
        builtin_endpoints=BuiltinEndpointsConfig(chat_api=True),
        model=None,
        timeout=1.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
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


@pytest.mark.parametrize("trigger_type", ["teams.new_channel_message_trigger", "connectors.generic_trigger"])
def test_validate_resolved_agent_rejects_dotted_connector_trigger_types(
    trigger_type: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / "report.agent.md"
    resolved = ResolvedAgent(
        name="Report",
        description="desc",
        trigger=TriggerSpec(type=trigger_type, args={}),
        instructions="x",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(chat_api=True),
        model=None,
        timeout=1.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
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
    assert "Dotted connector trigger types are not supported" in message
    assert "Use `connector_trigger` instead" in message
    assert message.count("docs/front-matter-spec.md#trigger") == 1


def test_validate_resolved_agent_allows_connector_trigger(tmp_path: Path) -> None:
    source = tmp_path / "report.agent.md"
    resolved = ResolvedAgent(
        name="Report",
        description="desc",
        trigger=TriggerSpec(type="connector_trigger", args={}),
        instructions="x",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(chat_api=True),
        model=None,
        timeout=1.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
        input_schema=None,
        response_schema=None,
        response_example=None,
        metadata={},
        source_file=str(source),
    )

    validate_resolved_agent(resolved, discovered_mcp_names=[], discovered_skills=[])


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
        builtin_endpoints=BuiltinEndpointsConfig(chat_api=True),
        model=None,
        timeout=1.0,
        enabled_mcp_names=["known"],
        enabled_skills_names=[],
        mcp_exclude_names=["missing"],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
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
        builtin_endpoints=BuiltinEndpointsConfig(chat_api=True),
        model=None,
        timeout=1.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        skills_exclude_names=["missing-skill"],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
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
        builtin_endpoints=BuiltinEndpointsConfig(chat_api=True),
        model=None,
        timeout=1.0,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(exclude=["bash"]),
        tool_exclude_names=["bash"],
        sandbox_config=None,
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


def test_validate_resolved_agent_relaxes_trigger_requirement_when_referenced_as_subagent() -> None:
    """An agent reachable only as another agent's delegation target does not need its own
    trigger/builtin_endpoints (FRD 0006 Decision #18)."""
    resolved = _make_resolved(trigger=None, builtin_endpoints=BuiltinEndpointsConfig())

    validate_resolved_agent(
        resolved,
        discovered_mcp_names=[],
        discovered_skills=[],
        is_referenced_as_subagent=True,
    )


def test_validate_resolved_agent_still_requires_trigger_when_not_referenced() -> None:
    """Regression: default `is_referenced_as_subagent=False` preserves the original requirement."""
    resolved = _make_resolved(trigger=None, builtin_endpoints=BuiltinEndpointsConfig())

    with pytest.raises(ValueError, match="field `trigger`"):
        validate_resolved_agent(resolved, discovered_mcp_names=[], discovered_skills=[])


def test_validate_subagent_references_accepts_known_references() -> None:
    resolved = _make_resolved(
        slug="coordinator",
        subagents=[SubagentRef(agent="billing-specialist"), SubagentRef(agent="shipping-specialist")],
    )

    validate_subagent_references(
        resolved,
        known_slugs={"coordinator", "billing-specialist", "shipping-specialist"},
    )


def test_validate_subagent_references_accepts_no_subagents() -> None:
    resolved = _make_resolved(slug="coordinator", subagents=[])
    validate_subagent_references(resolved, known_slugs={"coordinator"})


def test_validate_subagent_references_rejects_self_reference() -> None:
    resolved = _make_resolved(
        slug="coordinator",
        subagents=[SubagentRef(agent="coordinator")],
    )

    with pytest.raises(ValueError) as exc_info:
        validate_subagent_references(resolved, known_slugs={"coordinator"})

    message = str(exc_info.value)
    assert "field `subagents`" in message
    assert "delegate to itself" in message
    assert "coordinator" in message


def test_validate_subagent_references_rejects_unknown_reference() -> None:
    resolved = _make_resolved(
        slug="coordinator",
        subagents=[SubagentRef(agent="does-not-exist")],
    )

    with pytest.raises(ValueError) as exc_info:
        validate_subagent_references(resolved, known_slugs={"coordinator", "billing-specialist"})

    message = str(exc_info.value)
    assert "field `subagents`" in message
    assert "Unknown agent reference" in message
    assert "does-not-exist" in message


def test_validate_subagent_references_rejects_duplicate_reference() -> None:
    resolved = _make_resolved(
        slug="coordinator",
        subagents=[SubagentRef(agent="billing-specialist"), SubagentRef(agent="billing-specialist")],
    )

    with pytest.raises(ValueError) as exc_info:
        validate_subagent_references(
            resolved, known_slugs={"coordinator", "billing-specialist"}
        )

    message = str(exc_info.value)
    assert "field `subagents`" in message
    assert "Duplicate reference" in message
    assert "billing-specialist" in message
