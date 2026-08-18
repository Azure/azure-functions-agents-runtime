from __future__ import annotations

from pathlib import Path


def test_app_registration_and_live_turn_share_the_production_composition_facade() -> None:
    root = Path(__file__).parents[1]
    app = (root / "src/azure_functions_agents/app.py").read_text()
    facade = (root / "src/azure_functions_agents/execution/aca_composition.py").read_text()
    live_turn = (root / "tests/live/test_aca_real_agent_turn.py").read_text()

    assert "compose_aca_application(" in app
    assert "compose_aca_application(" in facade
    assert "AcaSandboxExecutionBackend" in facade
    assert "compose_aca_application(" in live_turn
    assert "FunctionAppPrincipal()" in live_turn
    assert "app_identity=production_smoke_app_identity()" in live_turn
    assert "AcaSandboxAdapter" not in live_turn
    assert "SandboxRunControl" not in live_turn
    assert "AZURE_CLIENT_ID" not in live_turn


def test_low_level_smoke_uses_the_production_setup_budget_and_manifest_gate() -> None:
    support = (Path(__file__).parents[1] / "tests/live/aca_smoke_support.py").read_text()

    assert "setup_deadline = SetupBudget.start()" in support
    assert "remaining_setup_budget_seconds=setup_deadline.remaining_setup_seconds(" in support
    assert "build_sandbox_create_profile(" in support
    assert "create_profile.build_request(" in support
    assert "_wait_for_created_manifest(" in support
    assert "remaining_setup_budget_seconds=30.0" not in support
    assert "SandboxCreateRequest.create(" not in support


def test_snapshot_cleanup_precedes_and_follows_sandbox_deletion() -> None:
    root = Path(__file__).parents[1]
    support = (root / "tests/live/aca_smoke_support.py").read_text()
    reaper = (root / "eng/scripts/reap_aca_smoke_sandboxes.py").read_text()

    assert support.count("await delete_snapshots()") >= 4
    assert "remaining_snapshots" in support
    assert "production_smoke_reaper_labels" in reaper
    assert "ci_smoke_reaper_labels" in reaper
    assert "reap_labelled_sandbox_family" in reaper


def test_current_checkout_aca_smoke_uses_only_protected_model_endpoint_variables() -> None:
    root = Path(__file__).parents[1]
    e2e = (root / "eng/templates/official/jobs/e2e-tests.yml").read_text()

    assert "ACA_SMOKE_AZURE_OPENAI_ENDPOINT" in e2e
    assert "ACA_SMOKE_AZURE_OPENAI_DEPLOYMENT" in e2e
    assert "AZURE_FUNCTIONS_AGENTS_PROVIDER: 'azure_openai'" in e2e
    for removed in (
        "ACA_SMOKE_MODEL_PROVIDER",
        "ACA_SMOKE_MODEL_RESOURCE_ID",
        "ACA_SMOKE_MODEL_ROLE_DEFINITION_ID",
        "ACA_SMOKE_FUNCTION_APP_OWNER_NAME",
        "WEBSITE_OWNER_NAME",
        "WEBSITE_SITE_NAME",
    ):
        assert removed not in e2e
