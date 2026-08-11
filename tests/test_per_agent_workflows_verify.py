"""Pure tests for the per-agent workflow sample verifier."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest

SAMPLE_ROOT = Path(__file__).resolve().parents[1] / "samples" / "per-agent-workflows"
VERIFY_SCRIPT = SAMPLE_ROOT / "scripts" / "verify.py"


def _load_verify_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("per_agent_workflows_verify", VERIFY_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_emulator_commands_is_isolated_and_backend_specific() -> None:
    verify = _load_verify_module()

    storage = verify.build_emulator_commands("test-run", "storage")
    dts = verify.build_emulator_commands("test-run", "dts")

    assert "engineering-ops-azurite-test-run" in storage.azurite
    assert storage.dts is None
    assert "127.0.0.1::10000" in storage.azurite
    assert dts.dts is not None
    assert "engineering-ops-dts-test-run" in dts.dts
    assert "DTS_TASK_HUB_NAMES=engineeringopshub" in dts.dts
    assert "127.0.0.1::8080" in dts.dts
    assert "127.0.0.1::8082" in dts.dts


def test_host_environment_prepends_current_checkout_without_dropping_pythonpath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verify = _load_verify_module()
    existing = os.pathsep.join(("first-existing", "second-existing"))
    monkeypatch.setenv("PYTHONPATH", existing)

    environment = verify.build_host_environment()

    paths = environment["PYTHONPATH"].split(os.pathsep)
    assert Path(paths[0]).resolve() == (verify.REPO_ROOT / "src").resolve()
    assert paths[1:] == ["first-existing", "second-existing"]
    assert Path(environment["AZURE_FUNCTIONS_AGENTS_EXPECTED_ROOT"]).resolve() == (
        verify.REPO_ROOT / "src"
    ).resolve()


def _clear_provider_environment(
    monkeypatch: pytest.MonkeyPatch,
    verify: ModuleType,
) -> None:
    for key in verify.PROVIDER_KEYS:
        monkeypatch.delenv(key, raising=False)


def _use_template_settings(
    monkeypatch: pytest.MonkeyPatch,
    verify: ModuleType,
    tmp_path: Path,
) -> None:
    template = verify.SAMPLE_SRC / "local.settings.template.json"
    (tmp_path / template.name).write_text(
        template.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setattr(verify, "SAMPLE_SRC", tmp_path)


@pytest.mark.parametrize(
    ("values", "provider"),
    [
        (
            {
                "FOUNDRY_PROJECT_ENDPOINT": "https://example.test/foundry",
                "FOUNDRY_MODEL": "foundry-model",
            },
            "foundry",
        ),
        (
            {
                "AZURE_OPENAI_ENDPOINT": "https://example.test/openai",
                "AZURE_OPENAI_DEPLOYMENT": "azure-deployment",
                "AZURE_OPENAI_API_VERSION": "2026-01-01",
            },
            "azure_openai",
        ),
        (
            {
                "OPENAI_API_KEY": "not-a-real-secret",
                "OPENAI_CHAT_MODEL_ID": "openai-model",
            },
            "openai",
        ),
    ],
)
def test_provider_values_select_environment_provider_over_template_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    values: dict[str, str],
    provider: str,
) -> None:
    verify = _load_verify_module()
    _clear_provider_environment(monkeypatch, verify)
    _use_template_settings(monkeypatch, verify, tmp_path)
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    resolved = verify._provider_values()

    assert resolved["AZURE_FUNCTIONS_AGENTS_PROVIDER"] == provider


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ("AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT"),
        ("AZURE_OPENAI_API_VERSION", "AZURE_OPENAI_API_VERSION"),
    ],
)
def test_provider_values_requires_complete_azure_openai_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    missing: str,
    message: str,
) -> None:
    verify = _load_verify_module()
    _clear_provider_environment(monkeypatch, verify)
    _use_template_settings(monkeypatch, verify, tmp_path)
    values = {
        "AZURE_OPENAI_ENDPOINT": "https://example.test/openai",
        "AZURE_OPENAI_DEPLOYMENT": "azure-deployment",
        "AZURE_OPENAI_API_VERSION": "2026-01-01",
    }
    values.pop(missing)
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(RuntimeError, match=message):
        verify._provider_values()


def test_provider_values_requires_openai_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    verify = _load_verify_module()
    _clear_provider_environment(monkeypatch, verify)
    _use_template_settings(monkeypatch, verify, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-secret")

    with pytest.raises(RuntimeError, match="OPENAI_CHAT_MODEL_ID"):
        verify._provider_values()


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_calls": [{"result": '{"workflow_id":"%s"}'}]},
        {"response": "Started workflow `%s`."},
        {"nested": [{"workflow_id": "%s"}]},
    ],
)
def test_extract_workflow_id_handles_nested_and_text_responses(
    payload: dict[str, object],
) -> None:
    verify = _load_verify_module()
    workflow_id = "0123456789abcdef0123456789abcdef-12345678123412341234123456789abc"
    rendered = str(payload).replace("%s", workflow_id)

    assert verify.extract_workflow_id(rendered) == workflow_id


def test_extract_workflow_id_rejects_legacy_hyphenated_uuid_suffix() -> None:
    verify = _load_verify_module()

    with pytest.raises(RuntimeError, match="valid workflow ID"):
        verify.extract_workflow_id(
            "0123456789abcdef0123456789abcdef-12345678-1234-1234-1234-123456789abc"
        )


def test_validate_terminal_result_checks_marker_structure_and_capabilities() -> None:
    verify = _load_verify_module()
    workflow_id = "0123456789abcdef0123456789abcdef-12345678123412341234123456789abc"
    envelope = {
        "workflow_id": workflow_id,
        "runtime_status": "Completed",
        "output": {
            "results": {
                "logs": {
                    "capability": "get_incident_logs",
                    "incident_id": "INC-4821",
                    "service": "checkout-api",
                },
                "metrics": {
                    "capability": "get_incident_metrics",
                    "incident_id": "INC-4821",
                    "service": "checkout-api",
                },
                "deployments": {
                    "capability": "get_incident_deployments",
                    "incident_id": "INC-4821",
                    "service": "checkout-api",
                },
                "analysis": {
                    "agent": "incident_evidence_analyst",
                    "text": "correlated",
                },
                "report": {
                    "marker": "INCIDENT_REPORT_READY",
                    "report_type": "incident",
                    "incident_id": "INC-4821",
                    "service": "checkout-api",
                    "decision": "ROLLBACK",
                    "capability": "compile_incident_report",
                },
            }
        },
    }

    verify.validate_terminal_result("incident_commander", envelope)

    envelope["output"]["results"]["wrong"] = {  # type: ignore[index]
        "capability": "get_release_vulnerabilities"
    }
    with pytest.raises(RuntimeError, match="unauthorized capabilities"):
        verify.validate_terminal_result("incident_commander", envelope)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("incident_id", "INC-WRONG"),
        ("service", "inventory-api"),
    ],
)
def test_validate_terminal_result_rejects_incident_evidence_identity_mismatch(
    field: str,
    value: str,
) -> None:
    verify = _load_verify_module()
    envelope = {
        "runtime_status": "Completed",
        "output": {
            "results": {
                "logs": {
                    "capability": "get_incident_logs",
                    "incident_id": "INC-4821",
                    "service": "checkout-api",
                },
                "metrics": {
                    "capability": "get_incident_metrics",
                    "incident_id": "INC-4821",
                    "service": "checkout-api",
                },
                "deployments": {
                    "capability": "get_incident_deployments",
                    "incident_id": "INC-4821",
                    "service": "checkout-api",
                },
                "analysis": {"agent": "incident_evidence_analyst", "text": "correlated"},
                "report": {
                    "marker": "INCIDENT_REPORT_READY",
                    "report_type": "incident",
                    "incident_id": "INC-4821",
                    "service": "checkout-api",
                    "decision": "ROLLBACK",
                    "capability": "compile_incident_report",
                },
            }
        },
    }
    envelope["output"]["results"]["metrics"][field] = value  # type: ignore[index]

    with pytest.raises(RuntimeError, match=r"evidence.*identity"):
        verify.validate_terminal_result("incident_commander", envelope)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("release_id", "REL-WRONG"),
        ("service", "inventory-api"),
    ],
)
def test_validate_terminal_result_rejects_release_evidence_identity_mismatch(
    field: str,
    value: str,
) -> None:
    verify = _load_verify_module()
    evidence_names = (
        "get_release_pull_requests",
        "get_release_test_results",
        "get_release_vulnerabilities",
        "get_release_change_window",
    )
    results = {
        name: {
            "capability": name,
            "release_id": "REL-2026.08.11",
            "service": "checkout-api",
        }
        for name in evidence_names
    }
    results["review"] = {"agent": "release_risk_reviewer", "text": "blocked"}
    results["dossier"] = {
        "marker": "RELEASE_DOSSIER_READY",
        "report_type": "release_readiness",
        "release_id": "REL-2026.08.11",
        "service": "checkout-api",
        "decision": "NO_GO",
        "capability": "compile_release_dossier",
    }
    results["get_release_vulnerabilities"][field] = value

    envelope = {
        "runtime_status": "Completed",
        "output": {"results": results},
    }
    with pytest.raises(RuntimeError, match=r"evidence.*identity"):
        verify.validate_terminal_result("release_manager", envelope)


def test_validate_owner_list_rejects_cross_owner_exposure() -> None:
    verify = _load_verify_module()
    incident_id = (
        "0123456789abcdef0123456789abcdef-12345678123412341234123456789abc"
    )
    release_id = (
        "fedcba9876543210fedcba9876543210-12345678123412341234123456789abc"
    )

    verify.validate_owner_list({"workflows": [{"workflow_id": incident_id}]}, incident_id, release_id)
    with pytest.raises(RuntimeError, match="exposed the other owner"):
        verify.validate_owner_list(
            {"workflows": [{"workflow_id": incident_id}, {"workflow_id": release_id}]},
            incident_id,
            release_id,
        )
