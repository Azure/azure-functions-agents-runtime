from __future__ import annotations

from pathlib import Path

import yaml


def test_e2e_template_has_one_aca_only_python313_smoke_job() -> None:
    root = Path(__file__).parents[1]
    pipeline = (root / "eng/ci/e2e-tests.yml").read_text()
    template = (root / "eng/templates/official/jobs/e2e-tests.yml").read_text()
    normal = template.split('- job: "ACACurrentCheckoutSmoke"', maxsplit=1)[0]
    smoke = template.split('- job: "ACACurrentCheckoutSmoke"', maxsplit=1)[1]

    assert "azureSubscription: 'saf-foundry-connection'" in normal
    assert template.count("ACACurrentCheckoutSmoke") == 1
    assert "versionSpec: '3.13'" in smoke
    assert "timeoutInMinutes: 30" in smoke
    assert "continueOnError: true" in smoke
    assert "Build.Reason" not in smoke
    assert "System.PullRequest.IsFork" in smoke
    assert "${{ parameters.acaServiceConnection }}" in smoke
    assert "saf-foundry-connection" not in smoke
    assert "test_aca_harness_entrypoint_smoke.py" in smoke
    assert "test_aca_run_journal_acceptance.py" in smoke
    assert "test_aca_real_agent_turn.py" in smoke
    assert "reap_aca_smoke_sandboxes.py" in smoke
    assert "name: acaServiceConnection" in pipeline
    assert "default: 'larohra-sandboxgroup-test'" in pipeline
    assert "acaServiceConnection: ${{ parameters.acaServiceConnection }}" in pipeline


def test_removed_predeployed_automation_and_targets_stay_removed() -> None:
    root = Path(__file__).parents[1]
    for relative_path in (
        "eng/ci/aca-smoke-tests.yml",
        "eng/templates/official/jobs/aca-smoke-tests.yml",
        "eng/ci/variables/aca-deployed-runtime-targets.yml",
        "eng/templates/official/steps/aca-deployed-qualification.yml",
    ):
        assert not (root / relative_path).exists()
    assert "pr: none" in (root / "eng/ci/official-build.yml").read_text()


def test_all_ci_yaml_is_parseable() -> None:
    root = Path(__file__).parents[1]
    for path in (root / "eng").glob("**/*.y*ml"):
        yaml.safe_load(path.read_text())
