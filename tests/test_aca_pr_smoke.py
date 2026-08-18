from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from types import ModuleType

import pytest

from azure_functions_agents.execution.aca_composition import compose_aca_application
from azure_functions_agents.execution.aca_sandbox import AcaSandboxExecutionBackend
from azure_functions_agents.session_state import (
    AppIdentity,
    FunctionAppOwnerContext,
    FunctionAppPrincipal,
    owner_partition,
)
from azure_functions_agents.transport.transport_models import SandboxProvisioningLabels
from tests.live import aca_smoke_support


@pytest.fixture
def smoke_module() -> ModuleType:
    path = Path(__file__).parents[1] / "eng" / "scripts" / "aca_pr_smoke.py"
    spec = importlib.util.spec_from_file_location("aca_pr_smoke", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_guest_identity_requires_one_user_assigned_identity(smoke_module: ModuleType) -> None:
    group = {
        "identity": {
            "type": "UserAssigned",
            "userAssignedIdentities": {"/uamis/model": {"principalId": "guest-object-id"}},
        }
    }

    smoke_module.assert_guest_identity(group)


@pytest.mark.parametrize(
    "group",
    [
        {"identity": {"type": "SystemAssigned", "userAssignedIdentities": {}}},
        {
            "identity": {
                "type": "SystemAssigned, UserAssigned",
                "userAssignedIdentities": {"/uamis/model": {}},
            }
        },
        {
            "identity": {
                "type": "UserAssigned",
                "userAssignedIdentities": {"/uamis/one": {}, "/uamis/two": {}},
            }
        },
        {"identity": {"type": "UserAssigned", "userAssignedIdentities": {}}},
    ],
)
def test_guest_identity_rejects_system_missing_or_ambiguous_uami(
    smoke_module: ModuleType, group: dict[str, object]
) -> None:
    with pytest.raises(smoke_module.PreflightError, match="sandbox_group_identity_ambiguous"):
        smoke_module.assert_guest_identity(group)


@pytest.mark.parametrize(
    "name",
    [
        "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_AZURE_OPENAI_ENDPOINT",
        "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_AZURE_OPENAI_DEPLOYMENT",
    ],
)
def test_preflight_rejects_unresolved_or_missing_model_variables(
    smoke_module: ModuleType, name: str
) -> None:
    with pytest.raises(smoke_module.PreflightError) as error:
        smoke_module.required({name: "$(PROTECTED_VALUE)"}, name)

    assert str(error.value) == f"required_environment_invalid:{name}"
    assert "PROTECTED_VALUE" not in str(error.value)


def test_preflight_rejects_explicit_host_client_identity(smoke_module: ModuleType) -> None:
    with pytest.raises(smoke_module.PreflightError, match="forbidden_environment_set:AZURE_CLIENT_ID"):
        smoke_module.forbidden({"AZURE_CLIENT_ID": "sensitive-client-id"}, "AZURE_CLIENT_ID")


def test_preflight_reads_only_the_sandbox_group_identity(
    monkeypatch: pytest.MonkeyPatch,
    smoke_module: ModuleType,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_az_json(arguments: list[str] | tuple[str, ...]) -> object:
        calls.append(tuple(arguments))
        return {
            "identity": {
                "type": "UserAssigned",
                "userAssignedIdentities": {"/uamis/model": {"principalId": "guest-object-id"}},
            }
        }

    monkeypatch.setattr(smoke_module, "az_json", fake_az_json)
    smoke_module.preflight(
        {
            "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID": "/sandbox-group",
            "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_DISK": "python-3.13",
            "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_AZURE_OPENAI_ENDPOINT": "https://model.example.net",
            "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_AZURE_OPENAI_DEPLOYMENT": "smoke-model",
        }
    )

    assert calls == [
        (
            "rest",
            "--method",
            "get",
            "--url",
            "/sandbox-group?api-version=2026-02-01-preview",
        )
    ]


def test_preflight_redacts_azure_cli_failures(
    monkeypatch: pytest.MonkeyPatch, smoke_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail() -> object:
        raise smoke_module.PreflightError("arm_audit_query_failed")

    monkeypatch.setattr(smoke_module, "preflight", fail)
    assert smoke_module.main() == 1
    assert capsys.readouterr().err == "ACA PR smoke preflight failed: arm_audit_query_failed\n"


def test_composition_binds_a_function_app_owner_without_azure_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = (
        Path(__file__).parents[1]
        / "tests"
        / "fixtures"
        / "config_scenarios"
        / "29_aca_sandbox_valid"
    )
    shutil.copytree(fixture, tmp_path, dirs_exist_ok=True)
    monkeypatch.setattr(
        "azure_functions_agents.transport.aca_sdk.validate_aca_sandbox_dependency",
        lambda: None,
    )
    monkeypatch.setattr(
        "azure_functions_agents.config.validation._validate_platform_capability",
        lambda: None,
    )
    identity = AppIdentity.create(
        subscription_id="00000000-0000-0000-0000-000000000000",
        site_name="aca-pr-smoke-123",
    )

    backend = compose_aca_application(tmp_path, app_identity=identity).backend_for(
        "main",
        owner=FunctionAppPrincipal(),
    )

    assert isinstance(backend, AcaSandboxExecutionBackend)
    assert isinstance(backend._owner, FunctionAppOwnerContext)
    assert backend._owner.app_identity == identity


def test_production_backend_labels_use_build_derived_identity_without_website_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WEBSITE_OWNER_NAME", raising=False)
    monkeypatch.delenv("WEBSITE_SITE_NAME", raising=False)
    monkeypatch.setenv(
        "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID",
        "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/"
        "providers/Microsoft.App/sandboxGroups/group",
    )
    monkeypatch.setenv(aca_smoke_support.ACA_SMOKE_RUN_ID_ENV_VAR, "123456")
    identity = aca_smoke_support.production_smoke_app_identity()
    partition = owner_partition(FunctionAppOwnerContext.create(identity, "model_turn"))
    backend_labels = SandboxProvisioningLabels.create(
        owner_hash_version=partition.owner_hash_version,
        owner_kind=partition.owner_kind,
        owner_hash=partition.owner_hash,
        app_hash=partition.app_hash,
        session_id="a-real-production-session-id",
        operation_label="op-1",
    ).to_provider_labels()

    selector = aca_smoke_support.production_smoke_reaper_labels()

    assert identity.site_name == "aca-pr-smoke-123456"
    assert all(backend_labels[key] == value for key, value in selector.items())
    monkeypatch.setenv(aca_smoke_support.ACA_SMOKE_RUN_ID_ENV_VAR, "654321")
    other_build_selector = aca_smoke_support.production_smoke_reaper_labels()
    assert other_build_selector["app_hash"] != selector["app_hash"]
    assert not all(backend_labels.get(key) == value for key, value in other_build_selector.items())
