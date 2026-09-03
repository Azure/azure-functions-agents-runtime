from pathlib import Path
from types import SimpleNamespace

import pytest

from azure_functions_agents.execution.aca_composition import compose_aca_application
from azure_functions_agents.experimental.hybrid_config import (
    HYBRID_SANDBOX_GROUP_ENV,
    HYBRID_SANDBOX_REGION_ENV,
    HybridConfigurationError,
    HybridSandboxSettings,
    resolve_hybrid_apim_settings,
    validate_hybrid_application,
)
from azure_functions_agents.harness import SANDBOX_MARKER_ENV_VAR


def test_hybrid_settings_are_absent_without_private_gate() -> None:
    assert HybridSandboxSettings.from_environment({}) is None


def test_hybrid_settings_require_reaper_outside_live_bound() -> None:
    settings = HybridSandboxSettings.from_environment(
        {
            HYBRID_SANDBOX_GROUP_ENV: (
                "/subscriptions/00000000-0000-0000-0000-000000000000/"
                "resourceGroups/rg/providers/Microsoft.App/sandboxGroups/group"
            ),
            HYBRID_SANDBOX_REGION_ENV: "westus2",
            "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_ORPHAN_AGE_SECONDS": "100",
        }
    )
    assert settings is not None
    with pytest.raises(HybridConfigurationError, match="must exceed"):
        settings.validate_reaper_bound(95)


def test_hybrid_apim_requires_exactly_one_auth_mode() -> None:
    environment = {
        "AZURE_FUNCTIONS_AGENTS_APIM_MODEL_BASE_URL": "https://example.test/openai/v1",
        "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_APIM_AUDIENCE": "api://gateway",
        "AZURE_FUNCTIONS_AGENTS_APIM_SUBSCRIPTION_KEY": "secret",
    }
    with pytest.raises(HybridConfigurationError, match="exactly one"):
        resolve_hybrid_apim_settings(environment)


def test_hybrid_application_rejects_web_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HYBRID_SANDBOX_GROUP_ENV, "configured")
    resolved = SimpleNamespace(
        source_file="main.agent.md",
        sandbox_config=None,
        web_request_config=object(),
        subagents=[],
        workflows=None,
        enabled_skills_names=[],
    )
    with pytest.raises(HybridConfigurationError, match="web_request"):
        validate_hybrid_application(
            SimpleNamespace(session_runtime=None),
            [resolved],
        )


def test_hybrid_composition_never_imports_customer_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(HYBRID_SANDBOX_GROUP_ENV, "configured")
    (tmp_path / "agents.config.yaml").write_text(
        "system_tools:\n  web_request: false\n",
        encoding="utf-8",
    )
    (tmp_path / "main.agent.md").write_text(
        "---\nname: Main\ndescription: Test\nbuiltin_endpoints: true\n---\nTest.",
        encoding="utf-8",
    )
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "must_not_import.py").write_text(
        "raise RuntimeError('worker imported customer code')\n",
        encoding="utf-8",
    )

    composition = compose_aca_application(tmp_path)

    assert composition.tool_result.user_tools == []


def test_hybrid_sample_guard_uses_canonical_sandbox_marker() -> None:
    source = (
        Path(__file__).parents[1]
        / "samples"
        / "hybrid-sandbox-apim-spike"
        / "src"
        / "tools"
        / "customer_probe.py"
    ).read_text(encoding="utf-8")

    assert f'os.environ.get("{SANDBOX_MARKER_ENV_VAR}")' in source
    assert "AZURE_FUNCTIONS_AGENTS_IN_ACA_SANDBOX" not in source
