from __future__ import annotations

import pytest
from pydantic import ValidationError

from azure_functions_agents.client_manager.providers import (
    AzureOpenAIConfig,
    FoundryConfig,
    OpenAIConfig,
)
from azure_functions_agents.config.schema import (
    AgentConfiguration,
    AgentSpec,
    DebugConfig,
    GlobalConfig,
    McpFilter,
    SystemToolsConfig,
    ToolsFilter,
    TriggerSpec,
)


def test_agent_spec_constructs() -> None:
    spec = AgentSpec(name='X', description='Y')
    assert spec.name == 'X'


def test_agent_spec_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        AgentSpec.model_validate({'name': 'X', 'description': 'Y', 'extra_field': 1})


@pytest.mark.parametrize('value', [True, False, None, DebugConfig(http=True)])
def test_agent_spec_debug_variants(value: bool | None | DebugConfig) -> None:
    spec = AgentSpec(name='X', description='Y', debug=value)
    assert spec.debug == value


@pytest.mark.parametrize('value', [False, None, McpFilter(exclude=['x'])])
def test_agent_spec_mcp_variants(value: bool | None | McpFilter) -> None:
    spec = AgentSpec(name='X', description='Y', mcp=value)
    assert spec.mcp == value


@pytest.mark.parametrize('value', [False, None, ToolsFilter(exclude=['x'])])
def test_agent_spec_tools_variants(value: bool | None | ToolsFilter) -> None:
    spec = AgentSpec(name='X', description='Y', tools=value)
    assert spec.tools == value


def test_agent_spec_accepts_logger_field() -> None:
    spec = AgentSpec.model_validate({'name': 'X', 'description': 'Y', 'logger': True})
    assert spec.logger is True


def test_trigger_spec_validates() -> None:
    trigger = TriggerSpec(type='timer_trigger', args={'schedule': '0 0 * * * *'})
    assert trigger.type == 'timer_trigger'


def test_trigger_spec_rejects_empty_type() -> None:
    with pytest.raises(ValidationError):
        TriggerSpec(type='')


def test_global_config_extra_forbidden() -> None:
    with pytest.raises(ValidationError):
        GlobalConfig.model_validate({'extra_field': 1})


def test_system_tools_config_parses() -> None:
    config = SystemToolsConfig.model_validate(
        {'execute_in_sessions': {'session_pool_management_endpoint': 'https://example.test'}}
    )
    assert config.execute_in_sessions is not None


def test_agent_configuration_accepts_top_level_model_only() -> None:
    config = AgentConfiguration.model_validate(
        {
            'provider': 'openai',
            'model': 'gpt-4o',
            'openai': {},
        }
    )

    assert config.model == 'gpt-4o'


@pytest.mark.parametrize(
    ('provider_config_class', 'provider_kwargs'),
    [
        (OpenAIConfig, {}),
        (
            AzureOpenAIConfig,
            {
                'azure_endpoint': 'https://azure-openai.example.test',
                'api_version': '2024-10-21',
            },
        ),
        (FoundryConfig, {'project_endpoint': 'https://foundry.example.test'}),
    ],
    ids=['openai', 'azure_openai', 'foundry'],
)
@pytest.mark.parametrize('model_value', ['gpt-4o', None, ''], ids=['string', 'none', 'empty'])
def test_provider_subblock_rejects_model_field(
    provider_config_class: type[OpenAIConfig] | type[AzureOpenAIConfig] | type[FoundryConfig],
    provider_kwargs: dict[str, str],
    model_value: str | None,
) -> None:
    with pytest.raises(ValidationError) as exc:
        provider_config_class(model=model_value, **provider_kwargs)

    assert "'model' is not a valid field in a provider sub-block" in str(exc.value)


def test_agent_configuration_rejects_when_no_model_anywhere() -> None:
    with pytest.raises(ValidationError) as exc:
        AgentConfiguration.model_validate(
            {
                'provider': 'openai',
                'openai': {},
            }
        )

    assert 'agent_configuration.model is required' in str(exc.value)


def test_all_empty_strings_for_model_fails_validation() -> None:
    with pytest.raises(ValidationError) as exc:
        AgentConfiguration.model_validate(
            {
                'provider': 'openai',
                'model': '   ',
                'openai': {},
            }
        )

    assert 'agent_configuration.model is required' in str(exc.value)


def test_agent_configuration_full_with_top_level_model_succeeds() -> None:
    config = AgentConfiguration.model_validate(
        {
            'provider': 'azure_openai',
            'model': 'gpt-4o',
            'azure_openai': {
                'azure_endpoint': 'https://azure-openai.example.test',
                'api_version': '2024-10-21',
            },
        }
    )

    assert config.provider_config.model_dump(exclude_none=True) == {
        'azure_endpoint': 'https://azure-openai.example.test',
        'api_version': '2024-10-21',
    }


def test_agent_spec_accepts_dict_for_agent_configuration() -> None:
    spec = AgentSpec(name='X', description='Y', agent_configuration={'model': 'x'})

    assert spec.agent_configuration == {'model': 'x'}
    assert isinstance(spec.agent_configuration, dict)


def test_agent_configuration_rejects_unknown_provider() -> None:
    with pytest.raises(ValidationError) as exc_info:
        AgentConfiguration.model_validate(
            {
                'provider': 'bogus',
                'model': 'gpt-4o',
            }
        )

    message = str(exc_info.value)
    assert 'Unknown provider' in message
    assert "'bogus'" in message


def test_agent_configuration_rejects_multiple_provider_sub_blocks() -> None:
    with pytest.raises(
        ValidationError,
        match='Only the sub-block matching the declared provider is permitted',
    ):
        AgentConfiguration.model_validate(
            {
                'provider': 'openai',
                'model': 'gpt-4o',
                'openai': {},
                'azure_openai': {
                    'azure_endpoint': 'https://azure-openai.example.test',
                    'api_version': '2024-10-21',
                },
            }
        )


def test_agent_configuration_rejects_mismatched_provider_sub_block() -> None:
    with pytest.raises(ValidationError) as exc_info:
        AgentConfiguration.model_validate(
            {
                'provider': 'openai',
                'model': 'gpt-4o',
                'azure_openai': {
                    'azure_endpoint': 'https://azure-openai.example.test',
                    'api_version': '2024-10-21',
                },
            }
        )

    message = str(exc_info.value)
    assert 'agent_configuration.openai must be provided' in message
