from __future__ import annotations

from pathlib import Path

import pytest

from azure_functions_agents.config.schema import (
    AgentConfiguration,
    DebugConfig,
    ResolvedAgent,
    ToolsFilter,
    TriggerSpec,
)
from azure_functions_agents.config.validation import (
    validate_agent_configuration,
    validate_resolved_agent,
)


def _openai_agent_configuration() -> AgentConfiguration:
    return AgentConfiguration.model_validate(
        {
            'provider': 'openai',
            'model': 'gpt-4o',
            'openai': {},
        }
    )


def _resolved_agent(
    source: Path,
    *,
    is_main: bool = True,
    agent_configuration: AgentConfiguration | None = None,
    **overrides: object,
) -> ResolvedAgent:
    payload: dict[str, object] = {
        'name': 'Report',
        'description': 'desc',
        'trigger': None,
        'instructions': 'x',
        'is_main': is_main,
        'debug': DebugConfig(),
        'agent_configuration': agent_configuration or _openai_agent_configuration(),
        'enabled_mcp_names': [],
        'enabled_skills_names': [],
        'tool_filter': ToolsFilter(),
        'sandbox_config': None,
        'input_schema': None,
        'response_schema': None,
        'response_example': None,
        'metadata': {},
        'source_file': str(source),
    }
    payload.update(overrides)
    return ResolvedAgent(**payload)


def test_validate_agent_configuration_requires_azure_openai_endpoint(tmp_path: Path) -> None:
    agent_configuration = AgentConfiguration.model_validate(
        {
            'provider': 'azure_openai',
            'model': 'gpt-4o',
            'azure_openai': {
                'api_version': '2024-10-21',
            },
        }
    )

    with pytest.raises(
        ValueError,
        match=r'agent_configuration\.azure_openai\.azure_endpoint must be set',
    ):
        validate_agent_configuration(
            agent_configuration,
            source_file=tmp_path / 'agent.agent.md',
            agent_name='Report',
        )


def test_validate_agent_configuration_requires_foundry_project_endpoint(
    tmp_path: Path,
) -> None:
    agent_configuration = AgentConfiguration.model_validate(
        {
            'provider': 'foundry',
            'model': 'gpt-4o',
            'foundry': {},
        }
    )

    with pytest.raises(
        ValueError,
        match=r'agent_configuration\.foundry\.project_endpoint must be set',
    ):
        validate_agent_configuration(
            agent_configuration,
            source_file=tmp_path / 'agent.agent.md',
            agent_name='Report',
        )


def test_validate_resolved_agent_requires_trigger_for_non_main(
    tmp_path: Path,
) -> None:
    source = tmp_path / 'report.agent.md'
    resolved = _resolved_agent(source, is_main=False)

    with pytest.raises(ValueError) as exc_info:
        validate_resolved_agent(resolved, discovered_mcp_names=[], discovered_skills=[])

    message = str(exc_info.value)
    assert 'field `trigger`' in message
    assert message.count('docs/front-matter-spec.md#trigger') == 1
    assert 'docs/front-matter-spec.mddocs/front-matter-spec.md#trigger' not in message


@pytest.mark.parametrize(
    ('trigger_type', 'expected'),
    [
        ('activity_trigger', 'Durable Functions triggers are not supported'),
        ('orchestration_trigger', 'Durable Functions triggers are not supported'),
        ('entity_trigger', 'Durable Functions triggers are not supported'),
        ('warm_up_trigger', 'Warm-up triggers are host lifecycle hooks'),
        ('route', 'Use `http_trigger` instead'),
        ('schedule', 'Use `timer_trigger` instead'),
        ('assistant_skill_trigger', 'Assistant skill triggers are not supported'),
        ('mcp_tool_trigger', 'MCP tool triggers are registered'),
        ('mcp_resource_trigger', 'MCP resource triggers are registered'),
        ('mcp_prompt_trigger', 'MCP prompt triggers are registered'),
    ],
)
def test_validate_resolved_agent_rejects_unsupported_trigger_types(
    trigger_type: str,
    expected: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / 'report.agent.md'
    resolved = _resolved_agent(
        source,
        is_main=False,
        trigger=TriggerSpec(type=trigger_type, args={}),
    )

    with pytest.raises(ValueError) as exc_info:
        validate_resolved_agent(resolved, discovered_mcp_names=[], discovered_skills=[])

    message = str(exc_info.value)
    assert 'field `trigger.type`' in message
    assert expected in message
    assert message.count('docs/front-matter-spec.md#trigger') == 1


@pytest.mark.parametrize('trigger_type', ['teams.new_channel_message_trigger', 'connectors.generic_trigger'])
def test_validate_resolved_agent_rejects_dotted_connector_trigger_types(
    trigger_type: str,
    tmp_path: Path,
) -> None:
    source = tmp_path / 'report.agent.md'
    resolved = _resolved_agent(
        source,
        is_main=False,
        trigger=TriggerSpec(type=trigger_type, args={}),
    )

    with pytest.raises(ValueError) as exc_info:
        validate_resolved_agent(resolved, discovered_mcp_names=[], discovered_skills=[])

    message = str(exc_info.value)
    assert 'field `trigger.type`' in message
    assert 'Dotted connector trigger types are not supported' in message
    assert 'Use `connector_trigger` instead' in message
    assert message.count('docs/front-matter-spec.md#trigger') == 1


def test_validate_resolved_agent_allows_connector_trigger(tmp_path: Path) -> None:
    source = tmp_path / 'report.agent.md'
    resolved = _resolved_agent(
        source,
        is_main=False,
        trigger=TriggerSpec(type='connector_trigger', args={}),
    )

    validate_resolved_agent(resolved, discovered_mcp_names=[], discovered_skills=[])


def test_validate_resolved_agent_rejects_unknown_mcp_exclude(
    tmp_path: Path,
) -> None:
    source = tmp_path / 'report.agent.md'
    resolved = _resolved_agent(source, mcp_exclude_names=['missing'], enabled_mcp_names=['known'])

    with pytest.raises(ValueError) as exc_info:
        validate_resolved_agent(
            resolved, discovered_mcp_names=['known'], discovered_skills=[]
        )

    message = str(exc_info.value)
    assert 'field `mcp.exclude`' in message
    assert message.count('docs/front-matter-spec.md#mcp') == 1
    assert 'docs/front-matter-spec.mddocs/front-matter-spec.md#mcp' not in message


def test_validate_resolved_agent_warns_on_unknown_skill_exclude(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    source = tmp_path / 'agent.agent.md'
    resolved = _resolved_agent(source, name='A', description='d', skills_exclude_names=['missing-skill'])

    import logging

    with caplog.at_level(logging.WARNING):
        validate_resolved_agent(
            resolved,
            discovered_mcp_names=[],
            discovered_skills=['other-skill'],
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any('missing-skill' in msg for msg in messages)
    assert any('skills.exclude' in msg for msg in messages)


def test_validate_resolved_agent_warns_on_tool_exclude(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    source = tmp_path / 'agent.agent.md'
    resolved = _resolved_agent(
        source,
        name='A',
        description='d',
        tool_filter=ToolsFilter(exclude=['bash']),
        tool_exclude_names=['bash'],
    )

    import logging

    with caplog.at_level(logging.WARNING):
        validate_resolved_agent(
            resolved,
            discovered_mcp_names=[],
            discovered_skills=[],
        )

    assert any('bash' in record.getMessage() for record in caplog.records)
