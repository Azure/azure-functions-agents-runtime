from __future__ import annotations

import pytest

from azure_functions_agents.execution.aca_composition import compose_aca_application
from azure_functions_agents.experimental.durable_loop_config import (
    DURABLE_LOOP_ACTIVITY_TIMEOUT_SECONDS_ENV,
    DURABLE_LOOP_BACKGROUND_MODEL_ENABLED_ENV,
    DURABLE_LOOP_ENABLED_ENV,
    DURABLE_LOOP_EVENT_RESULT_RETENTION_SECONDS_ENV,
    DURABLE_LOOP_FAULT_INJECTION_ENABLED_ENV,
    DURABLE_LOOP_HUMAN_CONTENT_RETENTION_SECONDS_ENV,
    DURABLE_LOOP_HUMAN_WAIT_SECONDS_ENV,
    DURABLE_LOOP_INPUT_COST_RATE_ENV,
    DURABLE_LOOP_LOCAL_TOOL_TIMEOUT_SECONDS_ENV,
    DURABLE_LOOP_MAX_APP_OWNED_SANDBOXES_ENV,
    DURABLE_LOOP_MAX_COST_MICROUNITS_ENV,
    DURABLE_LOOP_MAX_RUN_WAIT_SECONDS_ENV,
    DURABLE_LOOP_MAX_TOTAL_TOKENS_ENV,
    DURABLE_LOOP_OUTPUT_COST_RATE_ENV,
    DURABLE_LOOP_POLL_INITIAL_SECONDS_ENV,
    DURABLE_LOOP_POLL_MAX_SECONDS_ENV,
    DURABLE_LOOP_RECEIPT_RETENTION_SECONDS_ENV,
    DURABLE_LOOP_RETAINED_SANDBOX_AUTO_DELETE_SECONDS_ENV,
    DURABLE_LOOP_RETAINED_SANDBOX_ENABLED_ENV,
    DURABLE_LOOP_SANDBOX_REAPER_AGE_SECONDS_ENV,
    DURABLE_LOOP_SESSION_RETENTION_SECONDS_ENV,
    DURABLE_LOOP_SKILL_BLOB_CLIENT_ID_ENV,
    DURABLE_LOOP_SKILL_BLOB_URI_ENV,
    DURABLE_LOOP_SKILL_GRACE_SECONDS_ENV,
    DURABLE_LOOP_SKILL_PROVIDER_ENV,
    DURABLE_LOOP_TOMBSTONE_RETENTION_SECONDS_ENV,
    DURABLE_LOOP_TRIGGER_ADMISSION_DEADLINE_SECONDS_ENV,
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
            DURABLE_LOOP_BACKGROUND_MODEL_ENABLED_ENV: "true",
            DURABLE_LOOP_RETAINED_SANDBOX_ENABLED_ENV: "true",
            DURABLE_LOOP_FAULT_INJECTION_ENABLED_ENV: "true",
            DURABLE_LOOP_MAX_APP_OWNED_SANDBOXES_ENV: "7",
            DURABLE_LOOP_RETAINED_SANDBOX_AUTO_DELETE_SECONDS_ENV: "7200",
            DURABLE_LOOP_SANDBOX_REAPER_AGE_SECONDS_ENV: "900",
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
    assert settings.background_model_enabled is True
    assert settings.retained_sandbox_enabled is True
    assert settings.fault_injection_enabled is True
    assert settings.max_app_owned_sandboxes == 7
    assert settings.retained_sandbox_auto_delete_seconds == 7200
    assert settings.sandbox_reaper_age_seconds == 900


def test_durable_loop_settings_parse_skill_and_retention_values() -> None:
    settings = DurableLoopSettings.from_environment(
        {
            DURABLE_LOOP_ENABLED_ENV: "true",
            DURABLE_LOOP_SKILL_PROVIDER_ENV: "blob",
            DURABLE_LOOP_SKILL_BLOB_URI_ENV: (
                "https://skills.blob.core.windows.net/catalog"
            ),
            DURABLE_LOOP_SKILL_BLOB_CLIENT_ID_ENV: (
                "11111111-2222-3333-4444-555555555555"
            ),
            DURABLE_LOOP_EVENT_RESULT_RETENTION_SECONDS_ENV: "3600",
            DURABLE_LOOP_RECEIPT_RETENTION_SECONDS_ENV: "7200",
            DURABLE_LOOP_SKILL_GRACE_SECONDS_ENV: "86400",
            DURABLE_LOOP_HUMAN_CONTENT_RETENTION_SECONDS_ENV: "0",
            DURABLE_LOOP_SESSION_RETENTION_SECONDS_ENV: "86400",
            DURABLE_LOOP_TOMBSTONE_RETENTION_SECONDS_ENV: "7200",
            DURABLE_LOOP_TRIGGER_ADMISSION_DEADLINE_SECONDS_ENV: "300",
        }
    )

    assert settings is not None
    assert settings.skill_provider == "blob"
    assert settings.skill_blob_uri == (
        "https://skills.blob.core.windows.net/catalog"
    )
    assert settings.event_result_retention_seconds == 3600
    assert settings.receipt_retention_seconds == 7200
    assert settings.skill_grace_seconds == 86400
    assert settings.human_content_retention_seconds == 0
    assert settings.session_retention_seconds == 86400
    assert settings.tombstone_retention_seconds == 7200
    assert settings.trigger_admission_deadline_seconds == 300


@pytest.mark.parametrize(
    "overrides",
    [
        {DURABLE_LOOP_SKILL_PROVIDER_ENV: "bad/provider"},
        {DURABLE_LOOP_SKILL_PROVIDER_ENV: "blob"},
        {
            DURABLE_LOOP_SKILL_PROVIDER_ENV: "blob",
            DURABLE_LOOP_SKILL_BLOB_URI_ENV: (
                "https://skills.blob.core.windows.net/catalog?sig=secret"
            ),
        },
        {
            DURABLE_LOOP_SKILL_PROVIDER_ENV: "filesystem",
            DURABLE_LOOP_SKILL_BLOB_URI_ENV: (
                "https://skills.blob.core.windows.net/catalog"
            ),
        },
        {
            DURABLE_LOOP_SKILL_PROVIDER_ENV: "blob",
            DURABLE_LOOP_SKILL_BLOB_URI_ENV: (
                "https://skills.blob.core.windows.net/catalog"
            ),
            DURABLE_LOOP_SKILL_BLOB_CLIENT_ID_ENV: "not-a-uuid",
        },
        {
            DURABLE_LOOP_EVENT_RESULT_RETENTION_SECONDS_ENV: "7200",
            DURABLE_LOOP_RECEIPT_RETENTION_SECONDS_ENV: "3600",
        },
        {
            DURABLE_LOOP_EVENT_RESULT_RETENTION_SECONDS_ENV: "3600",
            DURABLE_LOOP_HUMAN_CONTENT_RETENTION_SECONDS_ENV: "3601",
        },
        {
            DURABLE_LOOP_RECEIPT_RETENTION_SECONDS_ENV: "7200",
            DURABLE_LOOP_TOMBSTONE_RETENTION_SECONDS_ENV: "7199",
        },
        {DURABLE_LOOP_TRIGGER_ADMISSION_DEADLINE_SECONDS_ENV: "299"},
    ],
)
def test_durable_loop_skill_and_retention_settings_fail_closed(
    overrides: dict[str, str],
) -> None:
    with pytest.raises(DurableLoopConfigurationError):
        DurableLoopSettings.from_environment(
            {DURABLE_LOOP_ENABLED_ENV: "true", **overrides}
        )


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
