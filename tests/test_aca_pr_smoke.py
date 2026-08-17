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
            "userAssignedIdentities": {
                "/subscriptions/x/uamis/model": {"principalId": "guest-object-id"}
            },
        }
    }
    assert smoke_module.guest_identity_principal_id(group) == "guest-object-id"

    with pytest.raises(smoke_module.PreflightError, match="identity_ambiguous"):
        smoke_module.guest_identity_principal_id({"identity": {"type": "SystemAssigned"}})


def test_preflight_rejects_broad_egress_and_non_model_roles(smoke_module: ModuleType) -> None:
    with pytest.raises(smoke_module.PreflightError, match="egress_not_model_only"):
        smoke_module.assert_model_only_egress(
            {
                "properties": {
                    "egressPolicy": {
                        "allowedHosts": ["model.example.net", "other.example.net"]
                    }
                }
            },
            "https://model.example.net",
        )
    with pytest.raises(smoke_module.PreflightError, match="role_assignment_ambiguous"):
        smoke_module.assert_model_only_roles(
            [
                {"roleDefinitionId": "model", "scope": "/models/model"},
                {"roleDefinitionId": "storage", "scope": "/subscriptions/sub"},
            ],
            "model",
            "/models/model",
        )


@pytest.mark.parametrize(
    "group",
    [
        {"identity": {"type": "SystemAssigned", "userAssignedIdentities": {}}},
        {
            "identity": {
                "type": "UserAssigned",
                "userAssignedIdentities": {
                    "/uamis/one": {"principalId": "one"},
                    "/uamis/two": {"principalId": "two"},
                },
            }
        },
        {"identity": {"type": "UserAssigned", "userAssignedIdentities": {}}},
    ],
)
def test_guest_identity_rejects_missing_or_ambiguous_uami(
    smoke_module: ModuleType, group: dict[str, object]
) -> None:
    with pytest.raises(smoke_module.PreflightError, match="identity_ambiguous"):
        smoke_module.guest_identity_principal_id(group)


def test_preflight_rejects_inherited_and_unenumerable_assignments(
    smoke_module: ModuleType,
) -> None:
    with pytest.raises(smoke_module.PreflightError, match="role_assignment_ambiguous"):
        smoke_module.assert_model_only_roles(
            [{"roleDefinitionId": "model", "scope": "/subscriptions/sub"}],
            "model",
            "/models/model",
        )
    with pytest.raises(smoke_module.PreflightError, match="role_assignment_ambiguous"):
        smoke_module.assert_model_only_roles([], "model", "/models/model")


def test_preflight_lists_all_inherited_assignments_by_guest_object_id(
    monkeypatch: pytest.MonkeyPatch,
    smoke_module: ModuleType,
) -> None:
    calls: list[tuple[str, ...]] = []
    responses = iter(
        (
            {
                "identity": {
                    "type": "UserAssigned",
                    "userAssignedIdentities": {
                        "/uamis/model": {"principalId": "guest-object-id"}
                    },
                },
                "properties": {"egressPolicy": {"allowedHosts": ["model.example.net"]}},
            },
            [{"roleDefinitionId": "model", "scope": "/models/model"}],
        )
    )

    def fake_az_json(arguments: list[str] | tuple[str, ...]) -> object:
        calls.append(tuple(arguments))
        return next(responses)

    monkeypatch.setattr(smoke_module, "az_json", fake_az_json)
    smoke_module.preflight(
        {
            "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID": "/sandbox-group",
            "ACA_SMOKE_MODEL_RESOURCE_ID": "/models/model",
            "ACA_SMOKE_MODEL_ROLE_DEFINITION_ID": "model",
            "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_AZURE_OPENAI_ENDPOINT": "https://model.example.net",
        }
    )

    assert calls[1] == (
        "role",
        "assignment",
        "list",
        "--assignee-object-id",
        "guest-object-id",
        "--all",
        "--include-inherited",
    )


def test_preflight_rejects_forbidden_inherited_assignment(smoke_module: ModuleType) -> None:
    with pytest.raises(smoke_module.PreflightError, match="role_assignment_ambiguous"):
        smoke_module.assert_model_only_roles(
            [
                {"roleDefinitionId": "model", "scope": "/models/model"},
                {
                    "roleDefinitionId": "storage-contributor",
                    "scope": "/subscriptions/sub",
                    "inheritedScope": "/subscriptions/sub",
                },
            ],
            "model",
            "/models/model",
        )


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


def test_production_backend_labels_match_current_build_reaper_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "WEBSITE_OWNER_NAME",
        "00000000-0000-0000-0000-000000000000+westuswebspace",
    )
    monkeypatch.setenv("WEBSITE_SITE_NAME", "aca-pr-smoke-123456")
    identity = aca_smoke_support.resolve_function_app_identity()
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

    assert all(backend_labels[key] == value for key, value in selector.items())
    monkeypatch.setenv("WEBSITE_SITE_NAME", "aca-pr-smoke-654321")
    other_build_selector = aca_smoke_support.production_smoke_reaper_labels()
    assert other_build_selector["app_hash"] != selector["app_hash"]
    assert not all(backend_labels.get(key) == value for key, value in other_build_selector.items())
