from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def qualification_module() -> ModuleType:
    path = Path(__file__).parents[1] / "eng" / "scripts" / "aca_deployed_qualification.py"
    spec = importlib.util.spec_from_file_location("aca_deployed_qualification", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _environment(module: ModuleType) -> dict[str, str]:
    return {
        **{name: "configured" for name in module._DEPLOYED_ENVIRONMENT},
        "BUILD_REASON": "Manual",
    }


@pytest.mark.parametrize("value", ["0", "101", "five", "2.5", "$(ACA_LOAD_CONCURRENCY)"])
def test_load_concurrency_rejects_non_integer_or_out_of_range_values(
    qualification_module: ModuleType, value: str
) -> None:
    with pytest.raises(qualification_module.QualificationError):
        qualification_module.validate_deployed_environment(
            _environment(qualification_module),
            runtime_target="python313",
            load_concurrency=value,
            provision_concurrency="1",
        )


def test_environment_validation_rejects_missing_and_unresolved_values(
    qualification_module: ModuleType,
) -> None:
    environment = _environment(qualification_module)
    environment["AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TABLE_NAME"] = "$(ACA_DEPLOYED_TABLE_NAME)"

    with pytest.raises(qualification_module.QualificationError, match="required_environment_invalid"):
        qualification_module.validate_deployed_environment(
            environment,
            runtime_target="python313",
            load_concurrency="5",
            provision_concurrency="1",
        )


def test_shared_group_dual_runtime_guards_allow_only_n5_and_one_provision_slot(
    qualification_module: ModuleType,
) -> None:
    environment = _environment(qualification_module)
    assert qualification_module.validate_deployed_environment(
        environment,
        runtime_target="both",
        load_concurrency="5",
        provision_concurrency="1",
    ) == (5, 1)

    with pytest.raises(
        qualification_module.QualificationError,
        match="dual_runtime_load_concurrency_requires_single_runtime",
    ):
        qualification_module.validate_deployed_environment(
            environment,
            runtime_target="both",
            load_concurrency="6",
            provision_concurrency="1",
        )
    with pytest.raises(
        qualification_module.QualificationError,
        match="provision_concurrency_requires_single_runtime",
    ):
        qualification_module.validate_deployed_environment(
            environment,
            runtime_target="both",
            load_concurrency="5",
            provision_concurrency="2",
        )


def test_single_runtime_allows_the_human_n100_configuration(
    qualification_module: ModuleType,
) -> None:
    assert qualification_module.validate_deployed_environment(
        _environment(qualification_module),
        runtime_target="python314",
        load_concurrency="100",
        provision_concurrency="4",
    ) == (100, 4)


@pytest.mark.parametrize("build_reason", ["PullRequest", "Schedule"])
def test_formal_n100_rejects_nonmanual_builds(
    qualification_module: ModuleType,
    build_reason: str,
) -> None:
    environment = _environment(qualification_module)
    environment["BUILD_REASON"] = build_reason

    with pytest.raises(
        qualification_module.QualificationError,
        match="formal_n100_requires_manual_build",
    ):
        qualification_module.validate_deployed_environment(
            environment,
            runtime_target="python314",
            load_concurrency="100",
            provision_concurrency="4",
        )


@pytest.mark.parametrize("value", ["0", "4", "two", "$(ACA_DEPLOYED_COLD_START_SAMPLES)"])
def test_cold_start_pipeline_cap_rejects_invalid_values(
    qualification_module: ModuleType, value: str
) -> None:
    with pytest.raises(qualification_module.QualificationError):
        qualification_module.validate_cold_start_samples({"ACA_DEPLOYED_COLD_START_SAMPLES": value})


def test_auth_preflight_redacts_token_failures(
    monkeypatch: pytest.MonkeyPatch, qualification_module: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    async def failing_token(_: str) -> None:
        raise RuntimeError("token-and-claims-must-not-appear")

    monkeypatch.setattr(qualification_module, "_get_easy_auth_token", failing_token)

    assert (
        qualification_module.main(
            [
                "preflight-auth",
                "--runtime-target",
                "python313",
                "--load-concurrency",
                "5",
                "--provision-concurrency",
                "1",
            ],
            _environment(qualification_module),
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "ACA qualification failed: auth_preflight_failed\n"
    assert "token-and-claims" not in captured.err


def test_deployed_suite_uses_the_redacted_preflight_and_expected_command(
    monkeypatch: pytest.MonkeyPatch, qualification_module: ModuleType
) -> None:
    observed: dict[str, object] = {}
    monkeypatch.setattr(qualification_module, "preflight_auth", lambda _: observed.setdefault("auth", True))

    def run(paths: tuple[str, ...], environment: dict[str, str]) -> int:
        observed["paths"] = paths
        observed["environment"] = environment
        return 7

    monkeypatch.setattr(qualification_module, "_run_pytest", run)

    assert (
        qualification_module.run_deployed_suite(
            _environment(qualification_module),
            runtime_target="python313",
            load_concurrency="5",
            provision_concurrency="1",
        )
        == 7
    )
    assert observed["auth"] is True
    assert observed["paths"] == (
        "tests/live/test_aca_deployed_agent_turn.py",
        "tests/live/test_aca_deployed_lifecycle.py",
        "tests/live/test_aca_deployed_loss.py",
        "tests/live/test_aca_deployed_load.py",
    )
    environment = observed["environment"]
    assert isinstance(environment, dict)
    assert environment["AZURE_FUNCTIONS_AGENTS_ACA_LOAD_CONCURRENCY"] == "5"
    assert environment["AZURE_FUNCTIONS_AGENTS_ACA_PROVISION_CONCURRENCY"] == "1"


def test_all_ci_yaml_is_parseable_and_deployed_jobs_use_the_shared_steps_template() -> None:
    import yaml

    root = Path(__file__).parents[1]
    for path in root.glob("**/*.y*ml"):
        yaml.safe_load(path.read_text())

    pipeline = (root / "eng" / "templates" / "official" / "jobs" / "aca-smoke-tests.yml").read_text()
    steps_template = (
        root / "eng" / "templates" / "official" / "steps" / "aca-deployed-qualification.yml"
    ).read_text()
    assert pipeline.count("aca-deployed-qualification.yml@self") == 4
    assert "python - <<'PY'" not in pipeline
    assert "az account show" not in pipeline
    assert "ACA_DEPLOYED_CONFIGURED_LOAD_CONCURRENCY" not in pipeline
    assert "PipAuthenticate@1" in steps_template
    assert "UsePythonVersion@0" in steps_template
    assert "AzureCLI@2" in steps_template
    assert "BUILD_REASON: $(Build.Reason)" in steps_template
    assert "aca_deployed_qualification.py" in steps_template
