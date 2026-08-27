from __future__ import annotations

import pytest

from azure_functions_agents.config.env import SANDBOX_ENV_PREFIX
from azure_functions_agents.controller.sandbox_config import (
    BUILTIN_SANDBOX_ENV_NAMES,
    MODEL_API_KEY_PLACEHOLDER,
    SANDBOX_DISK_ENV,
    SANDBOX_DISK_ID_ENV,
    build_bootstrap_entrypoint,
    build_sandbox_create_profile,
    build_sandbox_create_request,
    build_sandbox_environment,
    resolve_sandbox_create_source,
)
from azure_functions_agents.journal_paths import SANDBOX_PYTHONPATH
from azure_functions_agents.transport.transport_models import (
    DiskIdSource,
    DiskSource,
    SandboxEgressPolicy,
    SandboxProvisioningError,
    SandboxProvisioningLabels,
)


def test_build_sandbox_environment_forwards_only_documented_sources() -> None:
    environment = {
        BUILTIN_SANDBOX_ENV_NAMES[0]: "foundry",
        f"{SANDBOX_ENV_PREFIX}CUSTOM_FLAG": "enabled",
        f"{SANDBOX_ENV_PREFIX}DATABASE_PASSWORD": "explicit-value",
        "AzureWebJobsStorage": "not-forwarded",
        "UNRELATED": "not-forwarded",
    }

    forwarded = build_sandbox_environment(environment)

    assert forwarded == {
        BUILTIN_SANDBOX_ENV_NAMES[0]: "foundry",
        "CUSTOM_FLAG": "enabled",
        "DATABASE_PASSWORD": "explicit-value",
        "AZURE_FUNCTIONS_AGENTS_SANDBOX": "1",
        "PYTHONPATH": SANDBOX_PYTHONPATH,
    }
    with pytest.raises(TypeError):
        forwarded["OTHER"] = "value"  # type: ignore[index]


def test_sandbox_environment_forwards_region_for_delivered_config_reconstruction() -> None:
    forwarded = build_sandbox_environment(
        {"AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_REGION": "westus2"}
    )

    assert forwarded["AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_REGION"] == "westus2"


def test_sandbox_environment_prefix_is_stripped_once() -> None:
    forwarded = build_sandbox_environment(
        {"AZURE_FUNCTIONS_AGENTS_SANDBOXENV_MY_API_HOST": "https://sandbox.example"}
    )

    assert forwarded["MY_API_HOST"] == "https://sandbox.example"


def test_sandbox_environment_prefix_requires_a_nonempty_suffix() -> None:
    with pytest.raises(SandboxProvisioningError, match="must name an environment variable"):
        build_sandbox_environment({SANDBOX_ENV_PREFIX: "value"})


def test_host_process_values_are_not_forwarded_without_explicit_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AzureWebJobsStorage", "host-storage-credential")
    monkeypatch.setenv("UNRELATED_HOST_VALUE", "host-value")

    forwarded = build_sandbox_environment()

    assert "AzureWebJobsStorage" not in forwarded
    assert "UNRELATED_HOST_VALUE" not in forwarded


def test_builtin_profile_rejects_non_string_values_even_when_empty() -> None:
    with pytest.raises(SandboxProvisioningError, match="must be strings"):
        build_sandbox_environment({BUILTIN_SANDBOX_ENV_NAMES[0]: 0})  # type: ignore[dict-item]


def test_prefixed_setting_overrides_builtin_value_for_the_sandbox() -> None:
    environment = {
        "AZURE_OPENAI_ENDPOINT": "https://controller.example",
        f"{SANDBOX_ENV_PREFIX}AZURE_OPENAI_ENDPOINT": "https://sandbox.example",
    }

    assert build_sandbox_environment(environment)["AZURE_OPENAI_ENDPOINT"] == "https://sandbox.example"


def test_unprefixed_model_key_uses_a_non_secret_sandbox_placeholder() -> None:
    forwarded = build_sandbox_environment({"AZURE_OPENAI_API_KEY": "controller-key"})

    assert forwarded["AZURE_OPENAI_API_KEY"] == MODEL_API_KEY_PLACEHOLDER


def test_explicit_prefixed_model_key_is_forwarded_with_a_guest_exposure_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    forwarded = build_sandbox_environment(
        {
            "AZURE_OPENAI_API_KEY": "controller-key",
            f"{SANDBOX_ENV_PREFIX}AZURE_OPENAI_API_KEY": "sandbox-key",
        }
    )

    assert forwarded["AZURE_OPENAI_API_KEY"] == "sandbox-key"
    assert "bypasses proxy-managed isolation" in caplog.text


def test_default_disk_matches_python_minor_and_supports_overrides() -> None:
    assert resolve_sandbox_create_source({}, python_minor=13) == DiskSource.create("python-3.13")
    assert resolve_sandbox_create_source({SANDBOX_DISK_ENV: "custom-disk"}) == DiskSource.create(
        "custom-disk"
    )
    assert resolve_sandbox_create_source(
        {SANDBOX_DISK_ID_ENV: "immutable-disk-id"}
    ) == DiskIdSource.create("immutable-disk-id")


def test_conflicting_disk_overrides_fail_closed() -> None:
    with pytest.raises(SandboxProvisioningError, match="Only one"):
        resolve_sandbox_create_source(
            {
                SANDBOX_DISK_ENV: "custom-disk",
                SANDBOX_DISK_ID_ENV: "immutable-disk-id",
            }
        )


def test_create_request_uses_disk_safe_supervisor_and_full_inspection() -> None:
    request = build_sandbox_create_request(
        labels=SandboxProvisioningLabels.create(
            owner_hash_version="o1",
            owner_hash="o1-" + ("a" * 52),
            app_hash="a1-" + ("b" * 52),
            session_id="session-1",
        ),
        remaining_setup_budget_seconds=10,
        auto_suspend_seconds=300,
        egress_policy=SandboxEgressPolicy.create(),
        environment={f"{SANDBOX_ENV_PREFIX}CUSTOM_FLAG": "enabled"},
        source=DiskSource.create("python-3.13"),
    )

    assert request.auto_suspend_mode == "Disk"
    assert request.cmd == ()
    assert request.entrypoint[:2] == ("/bin/sh", "-c")
    assert ".boot-ready" in request.entrypoint[2]
    assert "bootstrap.py" in request.entrypoint[2]
    assert request.environment == {
        "CUSTOM_FLAG": "enabled",
        "AZURE_FUNCTIONS_AGENTS_SANDBOX": "1",
        "PYTHONPATH": SANDBOX_PYTHONPATH,
    }
    assert request.egress_policy.traffic_inspection == "Full"


def test_explicit_pythonpath_is_appended_after_runtime_import_paths() -> None:
    forwarded = build_sandbox_environment({f"{SANDBOX_ENV_PREFIX}PYTHONPATH": "/customer/modules"})

    assert forwarded["PYTHONPATH"] == f"{SANDBOX_PYTHONPATH}:/customer/modules"


def test_bootstrap_entrypoint_runs_once_after_content_is_ready() -> None:
    supervisor = build_bootstrap_entrypoint()[2]

    assert "--session-root" in supervisor
    assert "--journal-root" in supervisor
    assert "python3 -E -S" in supervisor
    assert "while :" not in supervisor


def test_create_profile_binds_runtime_marker_and_create_time_egress() -> None:
    profile = build_sandbox_create_profile(
        web_request_allowed_hosts=(),
        mcp_urls=(),
        model_endpoint=None,
        telemetry_endpoint=None,
        environment={f"{SANDBOX_ENV_PREFIX}CUSTOM_FLAG": "enabled"},
    )

    request = profile.build_request(
        labels=SandboxProvisioningLabels.create(
            owner_hash_version="o1",
            owner_hash="o1-" + ("a" * 52),
            app_hash="a1-" + ("b" * 52),
            session_id="session-1",
        ),
        remaining_setup_budget_seconds=10,
        auto_suspend_seconds=300,
    )

    assert request.environment["AZURE_FUNCTIONS_AGENTS_SANDBOX"] == "1"
    assert request.egress_policy.default_action == "Deny"
    assert request.egress_policy.traffic_inspection == "Full"


def test_create_profile_derives_telemetry_destination_from_connection_string() -> None:
    profile = build_sandbox_create_profile(
        web_request_allowed_hosts=(),
        mcp_urls=(),
        model_endpoint=None,
        telemetry_endpoint=None,
        environment={
            "APPLICATIONINSIGHTS_CONNECTION_STRING": (
                "InstrumentationKey=key;IngestionEndpoint=https://telemetry.example"
            )
        },
    )

    assert any(
        rule.host == "telemetry.example" and rule.action == "Allow"
        for rule in profile.egress_policy.host_rules
    )


def test_create_profile_uses_explicit_prefixed_model_endpoint_for_egress() -> None:
    profile = build_sandbox_create_profile(
        web_request_allowed_hosts=[],
        mcp_urls=(),
        model_endpoint=None,
        telemetry_endpoint=None,
        environment={f"{SANDBOX_ENV_PREFIX}AZURE_OPENAI_ENDPOINT": "https://sandbox.example/openai"},
    )

    assert any(
        rule.host == "sandbox.example" and rule.action == "Allow"
        for rule in profile.egress_policy.host_rules
    )


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("azure_openai", [("api-key", "azure-key")]),
        ("openai", [("Authorization", "Bearer " + "openai-key")]),
        ("foundry", []),
    ],
)
def test_create_profile_uses_only_the_resolved_provider_key(
    provider: str,
    expected: list[tuple[str, str]],
) -> None:
    profile = build_sandbox_create_profile(
        web_request_allowed_hosts=(),
        mcp_urls=(),
        model_endpoint="https://models.example.com",
        telemetry_endpoint=None,
        environment={
            "AZURE_FUNCTIONS_AGENTS_PROVIDER": provider,
            "AZURE_OPENAI_API_KEY": "azure-key",
            "OPENAI_API_KEY": "openai-key",
        },
    )

    model_rules = [rule for rule in profile.egress_policy.rules if rule.name == "model-auth"]
    assert [
        (header.name, header.value)
        for rule in model_rules
        for header in rule.action.headers
    ] == expected
