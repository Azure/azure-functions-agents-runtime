from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from azure_functions_agents.client_manager.providers import AzureOpenAIConfig, OpenAIConfig
from azure_functions_agents.config.schema import (
    AgentConfiguration,
    DebugConfig,
    ResolvedAgent,
    ToolsFilter,
)
from azure_functions_agents.config.validation import (
    validate_agent_configuration,
    validate_resolved_agent,
)


def _openai_agent_configuration() -> AgentConfiguration:
    return AgentConfiguration.model_validate(
        {
            "provider": "openai",
            "openai": {"model": "gpt-4o"},
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
        "name": "Report",
        "description": "desc",
        "trigger": None,
        "instructions": "x",
        "is_main": is_main,
        "debug": DebugConfig(),
        "agent_configuration": agent_configuration or _openai_agent_configuration(),
        "enabled_mcp_names": [],
        "enabled_skills_names": [],
        "tool_filter": ToolsFilter(),
        "sandbox_config": None,
        "connector_specs": [],
        "input_schema": None,
        "response_schema": None,
        "response_example": None,
        "metadata": {},
        "source_file": str(source),
    }
    payload.update(overrides)
    return ResolvedAgent(**payload)


def test_validate_agent_configuration_rejects_unknown_provider(tmp_path: Path) -> None:
    agent_configuration = AgentConfiguration.model_construct(
        provider="bogus",
        timeout=None,
        temperature=None,
        top_p=None,
        max_tokens=None,
        openai=None,
        azure_openai=None,
        foundry=None,
    )

    with pytest.raises(ValueError, match="declares unknown provider 'bogus'"):
        validate_agent_configuration(
            agent_configuration,
            source_file=tmp_path / "agent.agent.md",
            agent_name="Report",
        )


def test_validate_agent_configuration_rejects_multiple_provider_sub_blocks(
    tmp_path: Path,
) -> None:
    agent_configuration = AgentConfiguration.model_construct(
        provider="openai",
        timeout=None,
        temperature=None,
        top_p=None,
        max_tokens=None,
        openai=OpenAIConfig(model="gpt-4o"),
        azure_openai=AzureOpenAIConfig(
            model="gpt-4o",
            azure_endpoint="https://azure-openai.example.test",
            api_version="2024-10-21",
        ),
        foundry=None,
    )

    with pytest.raises(ValueError, match="declares multiple provider sub-blocks"):
        validate_agent_configuration(
            agent_configuration,
            source_file=tmp_path / "agent.agent.md",
            agent_name="Report",
        )


def test_agent_configuration_rejects_multiple_provider_sub_blocks_at_parse_time() -> None:
    with pytest.raises(
        ValidationError,
        match="Only the sub-block matching the declared provider is permitted",
    ):
        AgentConfiguration.model_validate(
            {
                "provider": "openai",
                "openai": {"model": "gpt-4o"},
                "azure_openai": {
                    "model": "gpt-4o",
                    "azure_endpoint": "https://azure-openai.example.test",
                    "api_version": "2024-10-21",
                },
            }
        )


def test_validate_agent_configuration_rejects_mismatched_provider_sub_block(
    tmp_path: Path,
) -> None:
    agent_configuration = AgentConfiguration.model_construct(
        provider="openai",
        timeout=None,
        temperature=None,
        top_p=None,
        max_tokens=None,
        openai=None,
        azure_openai=AzureOpenAIConfig(
            model="gpt-4o",
            azure_endpoint="https://azure-openai.example.test",
            api_version="2024-10-21",
        ),
        foundry=None,
    )

    with pytest.raises(ValueError, match="requires the matching `openai` sub-block; got `azure_openai` instead"):
        validate_agent_configuration(
            agent_configuration,
            source_file=tmp_path / "agent.agent.md",
            agent_name="Report",
        )


def test_validate_resolved_agent_requires_trigger_for_non_main(
    tmp_path: Path,
) -> None:
    source = tmp_path / "report.agent.md"
    resolved = _resolved_agent(source, is_main=False)

    with pytest.raises(ValueError) as exc_info:
        validate_resolved_agent(resolved, discovered_mcp_names=[], discovered_skills=[])

    message = str(exc_info.value)
    assert "field `trigger`" in message
    assert message.count("docs/front-matter-spec.md#trigger") == 1
    assert "docs/front-matter-spec.mddocs/front-matter-spec.md#trigger" not in message


def test_validate_resolved_agent_rejects_unknown_mcp_exclude(
    tmp_path: Path,
) -> None:
    source = tmp_path / "report.agent.md"
    resolved = _resolved_agent(source, mcp_exclude_names=["missing"], enabled_mcp_names=["known"])

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
    source = tmp_path / "agent.agent.md"
    resolved = _resolved_agent(source, name="A", description="d", skills_exclude_names=["missing-skill"])

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
    source = tmp_path / "agent.agent.md"
    resolved = _resolved_agent(
        source,
        name="A",
        description="d",
        tool_filter=ToolsFilter(exclude=["bash"]),
        tool_exclude_names=["bash"],
    )

    import logging

    with caplog.at_level(logging.WARNING):
        validate_resolved_agent(
            resolved,
            discovered_mcp_names=[],
            discovered_skills=[],
        )

    assert any("bash" in record.getMessage() for record in caplog.records)
