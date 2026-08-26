"""Tests for deployed ACA qualification admission gates."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[1] / "eng" / "scripts" / "aca_deployed_qualification.py"
_SPEC = importlib.util.spec_from_file_location("aca_deployed_qualification", _SCRIPT)
assert _SPEC and _SPEC.loader
qualification = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(qualification)


def _environment() -> dict[str, str]:
    return {
        name: "configured"
        for name in qualification._DEPLOYED_ENVIRONMENT
    } | {
        "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_FUNCTION_BASE_URL": "https://app.azurewebsites.net",
        "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_AGENT_SLUG": "agent",
        "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_TOKEN_SCOPE": "api://app/.default",
        "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EASY_AUTH_AUDIENCE": "app",
        "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TIMEOUT_SECONDS": "180",
        "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TABLE_SERVICE_URI": "https://table.table.core.windows.net",
        "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TABLE_NAME": "state",
        "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_APP_SUBSCRIPTION_ID": "subscription",
        "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_APP_SITE_NAME": "site",
        "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID": (
            "/subscriptions/s/resourceGroups/r/providers/Microsoft.App/sandboxGroups/g"
        ),
    }


def test_preflight_identity_is_an_explicit_qualification_command() -> None:
    parser = qualification._parser()
    args = parser.parse_args(["preflight-identity", "--runtime-target", "python313"])
    assert args.command == "preflight-identity"


def test_preflight_identity_rejects_insufficient_worker_population(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _probe(_environment: dict[str, str]) -> None:
        raise qualification.QualificationError("scale_out_population_insufficient:1:2")

    monkeypatch.setattr(qualification, "_run_deployed_identity_preflight", _probe)
    environment = _environment()
    environment[qualification._PREFLIGHT_WORKERS] = "2"
    with pytest.raises(qualification.QualificationError, match="scale_out_population"):
        qualification.preflight_deployed_identity(environment)
