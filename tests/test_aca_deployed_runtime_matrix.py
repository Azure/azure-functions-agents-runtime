from __future__ import annotations

from pathlib import Path


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


def test_deployed_runtime_matrix_is_pr_eligible_and_nonblocking() -> None:
    root = Path(__file__).parents[1]
    pipeline = (root / "eng" / "templates" / "official" / "jobs" / "e2e-tests.yml").read_text()
    official = (root / "eng" / "ci" / "official-build.yml").read_text()

    assert "pr:\n  branches:" in official
    for job_name, next_job_name in (
        ("ACADeployedAgentTurn", "ACADeployedColdStart"),
        ("ACADeployedColdStart", None),
    ):
        job = _deployed_job(pipeline, job_name, next_job_name)
        assert "PullRequest" in job
        assert "Manual" in job
        assert "Schedule" in job
        assert "continueOnError: true" in job
        assert "maxParallel: 2" in job
        assert _compile_matrix_legs(job, "both") == ("Python313", "Python314")
        assert _compile_matrix_legs(job, "python313") == ("Python313",)
        assert _compile_matrix_legs(job, "python314") == ("Python314",)

    turn_job = _deployed_job(pipeline, "ACADeployedAgentTurn", "ACADeployedColdStart")
    assert "dependsOn: ACADeployedColdStart" in turn_job


def test_connection_defaults_and_numeric_load_parameter_keep_dedicated_connection_explicit() -> None:
    root = Path(__file__).parents[1]
    template = (root / "eng" / "templates" / "official" / "jobs" / "e2e-tests.yml").read_text()

    for pipeline_path in ("eng/ci/e2e-tests.yml", "eng/ci/official-build.yml"):
        pipeline = (root / pipeline_path).read_text()
        assert "default: 'saf-foundry-connection'" in pipeline
        assert "acaServiceConnection: ${{ parameters.acaServiceConnection }}" in pipeline
        assert "type: number\n    default: 5" in pipeline

    assert "default: 'saf-foundry-connection'" in template
    assert "- name: acaLoadConcurrency\n    displayName: 'Deployed ACA load concurrency'\n    type: number\n    default: 5" in template
    assert "acaLoadConcurrency\n    displayName: 'Deployed ACA load concurrency'\n    type: number\n    default: 5\n    values:" not in template
    assert "- name: acaProvisionConcurrency" in template
    assert all(value in template for value in ("- '1'", "- '2'", "- '4'"))


def test_deployed_smoke_avoids_pr_artifact_attestation_claims() -> None:
    root = Path(__file__).parents[1]
    pipeline = (root / "eng" / "templates" / "official" / "jobs" / "e2e-tests.yml").read_text()
    runbook = (root / "tests" / "live" / "README.md").read_text()

    assert "predeployed-environment" in pipeline
    assert "does not deploy,\n  # inspect, or attest the pull request's artifact" in pipeline
    assert "does not attest the pull request artifact or remote Python runtime" in runbook
