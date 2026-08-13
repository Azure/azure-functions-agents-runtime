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


def test_deployed_runtime_matrix_uses_compile_time_two_phase_selection() -> None:
    root = Path(__file__).parents[1]
    pipeline = (root / "eng" / "templates" / "official" / "jobs" / "e2e-tests.yml").read_text()
    targets = (root / "eng" / "ci" / "variables" / "aca-deployed-runtime-targets.yml").read_text()
    e2e_pipeline = (root / "eng" / "ci" / "e2e-tests.yml").read_text()
    official_pipeline = (root / "eng" / "ci" / "official-build.yml").read_text()
    python313_if = (
        "${{ if or(eq(parameters.acaRuntimeTarget, 'both'), "
        "eq(parameters.acaRuntimeTarget, 'python313')) }}:"
    )
    python314_if = (
        "${{ if or(eq(parameters.acaRuntimeTarget, 'both'), "
        "eq(parameters.acaRuntimeTarget, 'python314')) }}:"
    )

    assert "- name: acaRuntimeTarget" in pipeline
    assert "default: both" in pipeline
    assert all(value in pipeline for value in ("- both", "- python313", "- python314"))
    assert "ACA_DEPLOYED_PY313_FUNCTION_BASE_URL" in targets
    assert "https://func-afar-u3q-6k9m2p7.azurewebsites.net/api" in targets
    assert "ACA_DEPLOYED_PY313_APP_SITE_NAME: 'func-afar-u3q-6k9m2p7'" in targets
    assert "https://func-afar-u3q314-6k9m2p7.azurewebsites.net/api" in targets
    assert "ACA_DEPLOYED_PY314_APP_SITE_NAME: 'func-afar-u3q314-6k9m2p7'" in targets
    assert "variables/aca-deployed-runtime-targets.yml" in e2e_pipeline
    assert "variables/aca-deployed-runtime-targets.yml" in official_pipeline

    for job_name, next_job_name in (
        ("ACADeployedAgentTurn", "ACADeployedColdStart"),
        ("ACADeployedColdStart", None),
    ):
        job = _deployed_job(pipeline, job_name, next_job_name)
        assert "in(variables['Build.Reason'], 'Manual', 'Schedule')" in job
        assert "PullRequest" not in job
        assert "continueOnError: true" in job
        assert "maxParallel: 2" in job
        assert python313_if in job
        assert python314_if in job
        assert "Python313:" in job
        assert "Python314:" in job
        assert "ACA_DEPLOYED_FUNCTION_BASE_URL: $(ACA_DEPLOYED_PY313_FUNCTION_BASE_URL)" in job
        assert "ACA_DEPLOYED_FUNCTION_BASE_URL: $(ACA_DEPLOYED_PY314_FUNCTION_BASE_URL)" in job
        assert "ACA_DEPLOYED_APP_SITE_NAME: $(ACA_DEPLOYED_PY313_APP_SITE_NAME)" in job
        assert "ACA_DEPLOYED_APP_SITE_NAME: $(ACA_DEPLOYED_PY314_APP_SITE_NAME)" in job
        assert "ACA_DEPLOYED_RUNTIME_TARGET" not in job
        assert "ACA_DEPLOYED_EXPECTED_RUNTIME_LABEL" not in job
        assert _compile_matrix_legs(job, "both") == ("Python313", "Python314")
        assert _compile_matrix_legs(job, "python313") == ("Python313",)
        assert _compile_matrix_legs(job, "python314") == ("Python314",)

    turn_job = _deployed_job(pipeline, "ACADeployedAgentTurn", "ACADeployedColdStart")
    assert "dependsOn: ACADeployedColdStart" in turn_job


def test_entrypoints_forward_shared_connection_and_human_only_parameters() -> None:
    root = Path(__file__).parents[1]
    template = (root / "eng" / "templates" / "official" / "jobs" / "e2e-tests.yml").read_text()

    for pipeline_path in ("eng/ci/e2e-tests.yml", "eng/ci/official-build.yml"):
        pipeline = (root / pipeline_path).read_text()
        assert "acaServiceConnection" in pipeline
        assert "default: 'larohra-sandboxgroup-test'" in pipeline
        assert "acaServiceConnection: ${{ parameters.acaServiceConnection }}" in pipeline
        assert "acaRuntimeTarget: ${{ parameters.acaRuntimeTarget }}" in pipeline
        assert "acaLoadConcurrency: ${{ parameters.acaLoadConcurrency }}" in pipeline
        assert "acaProvisionConcurrency: ${{ parameters.acaProvisionConcurrency }}" in pipeline

    assert "default: 'larohra-sandboxgroup-test'" in template
    assert "- name: acaLoadConcurrency" in template
    assert "default: '5'" in template
    assert "- name: acaProvisionConcurrency" in template
    assert "default: '1'" in template
    assert all(value in template for value in ("- '1'", "- '2'", "- '4'", "- '100'"))


def test_n100_and_provisioning_preflights_protect_shared_group_without_affecting_cold() -> None:
    root = Path(__file__).parents[1]
    pipeline = (root / "eng" / "templates" / "official" / "jobs" / "e2e-tests.yml").read_text()
    turn_job = _deployed_job(pipeline, "ACADeployedAgentTurn", "ACADeployedColdStart")
    cold_job = _deployed_job(pipeline, "ACADeployedColdStart")

    assert "ACA_DEPLOYED_CONFIGURED_LOAD_CONCURRENCY: ${{ parameters.acaLoadConcurrency }}" in turn_job
    assert "ACA_DEPLOYED_CONFIGURED_PROVISION_CONCURRENCY: ${{ parameters.acaProvisionConcurrency }}" in turn_job
    assert 'ACA_DEPLOYED_CONFIGURED_LOAD_CONCURRENCY}" = "100"' in turn_job
    assert 'ACA_DEPLOYED_CONFIGURED_PROVISION_CONCURRENCY}" -gt 1' in turn_job
    assert turn_job.count('[ "${{ parameters.acaRuntimeTarget }}" = "both" ]') == 2
    assert "acaLoadConcurrency=100 requires acaRuntimeTarget=python313 or python314" in turn_job
    assert "acaProvisionConcurrency above 1 requires acaRuntimeTarget=python313 or python314" in turn_job
    assert "AZURE_FUNCTIONS_AGENTS_ACA_PROVISION_CONCURRENCY" in turn_job
    assert "ACA_DEPLOYED_LOAD_CONCURRENCY" not in turn_job
    assert "ACA_DEPLOYED_LOAD_CONCURRENCY" not in cold_job
    assert "AZURE_FUNCTIONS_AGENTS_ACA_PROVISION_CONCURRENCY" not in cold_job
    assert "tests/live/test_aca_deployed_load.py" not in cold_job
