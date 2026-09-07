from __future__ import annotations

import pytest

from azure_functions_agents.execution.aca_composition import compose_aca_application
from azure_functions_agents.experimental.durable_loop_config import (
    DURABLE_LOOP_ACTIVITY_TIMEOUT_SECONDS_ENV,
    DURABLE_LOOP_ENABLED_ENV,
    DURABLE_LOOP_HUMAN_WAIT_SECONDS_ENV,
    DURABLE_LOOP_INPUT_COST_RATE_ENV,
    DURABLE_LOOP_LOCAL_TOOL_TIMEOUT_SECONDS_ENV,
    DURABLE_LOOP_MAX_COST_MICROUNITS_ENV,
    DURABLE_LOOP_MAX_RUN_WAIT_SECONDS_ENV,
    DURABLE_LOOP_MAX_TOTAL_TOKENS_ENV,
    DURABLE_LOOP_OUTPUT_COST_RATE_ENV,
    DURABLE_LOOP_POLL_INITIAL_SECONDS_ENV,
    DURABLE_LOOP_POLL_MAX_SECONDS_ENV,
    DurableLoopConfigurationError,
    DurableLoopSettings,
)


def test_durable_loop_settings_are_absent_without_private_gate() -> None:
    assert DurableLoopSettings.from_environment({}) is None


def test_durable_loop_settings_parse_bounded_private_values() -> None:
    settings = DurableLoopSettings.from_environment(
        {
            DURABLE_LOOP_ENABLED_ENV: "true",
            DURABLE_LOOP_ACTIVITY_TIMEOUT_SECONDS_ENV: "240",
            DURABLE_LOOP_LOCAL_TOOL_TIMEOUT_SECONDS_ENV: "120",
            DURABLE_LOOP_INPUT_COST_RATE_ENV: "1000000",
            DURABLE_LOOP_MAX_COST_MICROUNITS_ENV: "25000000",
            DURABLE_LOOP_MAX_TOTAL_TOKENS_ENV: "500000",
            DURABLE_LOOP_OUTPUT_COST_RATE_ENV: "2000000",
            DURABLE_LOOP_POLL_INITIAL_SECONDS_ENV: "1.5",
            DURABLE_LOOP_POLL_MAX_SECONDS_ENV: "12",
        }
    )

    assert settings is not None
    assert settings.activity_timeout_seconds == 240
    assert settings.local_tool_timeout_seconds == 120
    assert settings.max_cost_microunits == 25_000_000
    assert settings.max_total_tokens == 500_000
    assert settings.input_cost_microunits_per_million_tokens == 1_000_000
    assert settings.output_cost_microunits_per_million_tokens == 2_000_000
    assert settings.poll_initial_seconds == 1.5
    assert settings.poll_max_seconds == 12


@pytest.mark.parametrize(
    "environment",
    [
        {DURABLE_LOOP_ENABLED_ENV: "maybe"},
        {
            DURABLE_LOOP_ENABLED_ENV: "true",
            DURABLE_LOOP_ACTIVITY_TIMEOUT_SECONDS_ENV: "481",
        },
        {
            DURABLE_LOOP_ENABLED_ENV: "true",
            DURABLE_LOOP_ACTIVITY_TIMEOUT_SECONDS_ENV: "60",
            DURABLE_LOOP_LOCAL_TOOL_TIMEOUT_SECONDS_ENV: "61",
        },
        {
            DURABLE_LOOP_ENABLED_ENV: "true",
            DURABLE_LOOP_POLL_INITIAL_SECONDS_ENV: "31",
            DURABLE_LOOP_POLL_MAX_SECONDS_ENV: "30",
        },
        {
            DURABLE_LOOP_ENABLED_ENV: "true",
            DURABLE_LOOP_HUMAN_WAIT_SECONDS_ENV: "100",
            DURABLE_LOOP_MAX_RUN_WAIT_SECONDS_ENV: "99",
        },
        {
            DURABLE_LOOP_ENABLED_ENV: "true",
            DURABLE_LOOP_MAX_TOTAL_TOKENS_ENV: "0",
        },
        {
            DURABLE_LOOP_ENABLED_ENV: "true",
            DURABLE_LOOP_MAX_COST_MICROUNITS_ENV: "1",
        },
    ],
)
def test_durable_loop_settings_fail_closed(environment: dict[str, str]) -> None:
    with pytest.raises(DurableLoopConfigurationError):
        DurableLoopSettings.from_environment(environment)


def test_durable_loop_composition_never_imports_customer_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv(DURABLE_LOOP_ENABLED_ENV, "true")
    (tmp_path / "main.agent.md").write_text(
        (
            "---\n"
            "name: Main\n"
            "description: Durable test\n"
            "builtin_endpoints: true\n"
            "---\n"
            "Test."
        ),
        encoding="utf-8",
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "must_not_import.py").write_text(
        "raise RuntimeError('worker imported customer code')\n",
        encoding="utf-8",
    )

    composition = compose_aca_application(tmp_path)

    assert composition.tool_result.user_tools == []


def test_durable_loop_rejects_dynamic_workflows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv(DURABLE_LOOP_ENABLED_ENV, "true")
    (tmp_path / "main.agent.md").write_text(
        (
            "---\n"
            "name: Main\n"
            "description: Durable test\n"
            "builtin_endpoints: true\n"
            "workflows:\n"
            "  enabled: true\n"
            "---\n"
            "Test."
        ),
        encoding="utf-8",
    )

    with pytest.raises(DurableLoopConfigurationError, match="separate private engines"):
        compose_aca_application(tmp_path)


def test_durable_loop_rejects_declared_triggers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv(DURABLE_LOOP_ENABLED_ENV, "true")
    (tmp_path / "main.agent.md").write_text(
        (
            "---\n"
            "name: Main\n"
            "description: Durable test\n"
            "builtin_endpoints: true\n"
            "trigger:\n"
            "  type: timer_trigger\n"
            "  args:\n"
            "    schedule: '0 */5 * * * *'\n"
            "---\n"
            "Test."
        ),
        encoding="utf-8",
    )

    with pytest.raises(DurableLoopConfigurationError, match="declared triggers"):
        compose_aca_application(tmp_path)
