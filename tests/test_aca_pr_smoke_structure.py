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
    assert "app_identity=resolve_function_app_identity()" in live_turn
    assert "AcaSandboxAdapter" not in live_turn
    assert "SandboxRunControl" not in live_turn
    assert "AZURE_CLIENT_ID" not in live_turn


def test_snapshot_cleanup_precedes_and_follows_sandbox_deletion() -> None:
    root = Path(__file__).parents[1]
    support = (root / "tests/live/aca_smoke_support.py").read_text()
    reaper = (root / "eng/scripts/reap_aca_smoke_sandboxes.py").read_text()

    assert support.count("await delete_snapshots()") >= 4
    assert "remaining_snapshots" in support
    assert "production_smoke_reaper_labels" in reaper
    assert "ci_smoke_reaper_labels" in reaper
    assert "reap_labelled_sandbox_family" in reaper
