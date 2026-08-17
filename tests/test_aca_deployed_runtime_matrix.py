from __future__ import annotations

from pathlib import Path

import yaml


def _deployed_job(pipeline: str, job_name: str, next_job_name: str | None = None) -> str:
    job = pipeline.split(f'- job: "{job_name}"', maxsplit=1)[1]
    if next_job_name is not None:
        job = job.split(f'- job: "{next_job_name}"', maxsplit=1)[0]
    return job


def _compile_matrix_legs(job: str, target: str) -> tuple[str, ...]:
    conditions = {
        "Python313": (
            "${{ if or(eq(parameters.acaRuntimeTarget, 'both'), "
            "eq(parameters.acaRuntimeTarget, 'python313')) }}:"
        ),
        "Python314": (
            "${{ if or(eq(parameters.acaRuntimeTarget, 'both'), "
            "eq(parameters.acaRuntimeTarget, 'python314')) }}:"
        ),
    }
    targets = {"Python313": "python313", "Python314": "python314"}
    return tuple(
        leg for leg, condition in conditions.items() if condition in job and target in {"both", targets[leg]}
    )


def test_required_e2e_pipelines_exclude_aca_and_keep_foundry_connection() -> None:
    root = Path(__file__).parents[1]
    prohibited = (
        "ACA",
        "acaServiceConnection",
        "acaRuntimeTarget",
        "acaLoadConcurrency",
        "acaProvisionConcurrency",
        "aca-deployed-runtime-targets.yml",
    )

    for path in (
        "eng/ci/e2e-tests.yml",
        "eng/ci/official-build.yml",
        "eng/templates/official/jobs/e2e-tests.yml",
    ):
        content = (root / path).read_text()
        assert not any(value in content for value in prohibited)

    e2e_template = (root / "eng/templates/official/jobs/e2e-tests.yml").read_text()
    assert "azureSubscription: 'saf-foundry-connection'" in e2e_template


def test_optional_aca_pipeline_is_pr_triggered_and_uses_aca_only_connection() -> None:
    root = Path(__file__).parents[1]
    pipeline = (root / "eng/ci/aca-smoke-tests.yml").read_text()

    assert "trigger: none" in pipeline
    assert "pr:\n  branches:" in pipeline
    assert "- main" in pipeline
    assert "- feature/*" in pipeline
    assert "schedules:" in pipeline
    assert "variables/aca-deployed-runtime-targets.yml" in pipeline
    assert "default: 'larohra-sandboxgroup-test'" in pipeline
    assert "default: 'saf-foundry-connection'" not in pipeline
    assert "build-artifacts.yml" not in pipeline
    assert "aca-smoke-tests.yml@self" in pipeline


def test_optional_aca_template_preserves_guards_dependencies_and_nonblocking_jobs() -> None:
    root = Path(__file__).parents[1]
    template = (root / "eng/templates/official/jobs/aca-smoke-tests.yml").read_text()
    pipeline = (root / "eng/ci/aca-smoke-tests.yml").read_text()

    for job_name, next_job_name in (
        ("ACAHarnessEntrypointSmoke", "ACADeployedColdStart"),
        ("ACADeployedColdStart", "ACADeployedAgentTurn"),
        ("ACADeployedAgentTurn", None),
    ):
        job = _deployed_job(template, job_name, next_job_name)
        assert "continueOnError: true" in job

    harness_job = _deployed_job(template, "ACAHarnessEntrypointSmoke", "ACADeployedColdStart")
    assert "Schedule" in harness_job
    assert "Manual" in harness_job
    assert "PullRequest" not in harness_job

    for job_name, next_job_name in (
        ("ACADeployedColdStart", "ACADeployedAgentTurn"),
        ("ACADeployedAgentTurn", None),
    ):
        job = _deployed_job(template, job_name, next_job_name)
        assert "Build.Reason" not in job
        assert "maxParallel: 2" in job
        assert _compile_matrix_legs(job, "both") == ("Python313", "Python314")
        assert _compile_matrix_legs(job, "python313") == ("Python313",)
        assert _compile_matrix_legs(job, "python314") == ("Python314",)

    turn_job = _deployed_job(template, "ACADeployedAgentTurn")
    assert "dependsOn: ACADeployedColdStart" in turn_job
    assert "pr:" in pipeline
    assert "- main" in pipeline
    assert "- feature/*" in pipeline
    assert "default: 5" in template
    assert "--load-concurrency ${{ parameters.acaLoadConcurrency }}" in template
    assert "--provision-concurrency ${{ parameters.acaProvisionConcurrency }}" in template


def test_all_ci_yaml_is_parseable_and_aca_wiring_is_shared() -> None:
    root = Path(__file__).parents[1]
    for path in root.glob("**/*.y*ml"):
        yaml.safe_load(path.read_text())

    template = (root / "eng/templates/official/jobs/aca-smoke-tests.yml").read_text()
    steps_template = (
        root / "eng/templates/official/steps/aca-deployed-qualification.yml"
    ).read_text()
    assert template.count("aca-deployed-qualification.yml@self") == 4
    assert "python - <<'PY'" not in template
    assert "az account show" not in template
    assert "PipAuthenticate@1" in steps_template
    assert "UsePythonVersion@0" in steps_template
    assert "AzureCLI@2" in steps_template
    assert "BUILD_REASON: $(Build.Reason)" in steps_template
    assert "aca_deployed_qualification.py" in steps_template
