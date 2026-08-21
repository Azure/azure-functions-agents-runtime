from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import cast

import pytest

from azure_functions_agents.foundry_responses.fha_hosted_source_staging import (
    FhaHostedDependencyPins,
    FhaHostedSourceStagingError,
    resolve_fha_runtime_pin,
    stage_fha_hosted_source,
)
from azure_functions_agents.foundry_responses.fha_runtime_projection import (
    FHA_RUNTIME_PROJECTION_FILENAME,
    FhaRuntimeProjection,
    serialize_fha_runtime_projection,
)


def _pins() -> FhaHostedDependencyPins:
    return FhaHostedDependencyPins.create(
        runtime="azurefunctions-agents-runtime==0.1.0",
        agentserver_core="azure-ai-agentserver-core==2.1.0b1",
        agentserver_responses="azure-ai-agentserver-responses==2.1.0b1",
    )


def _projection() -> FhaRuntimeProjection:
    return FhaRuntimeProjection.create(
        project_endpoint="https://project.services.ai.azure.com/api/projects/demo",
        default_model="gpt-model",
        catalog=(),
    )


def test_stage_hosted_source_copies_only_selected_files_and_generates_pinned_artifacts(
    tmp_path: Path,
) -> None:
    application_root = tmp_path / "application"
    application_root.mkdir()
    (application_root / "main.agent.md").write_text("agent", encoding="utf-8")
    package = application_root / "package"
    package.mkdir()
    (package / "worker.py").write_text("VALUE = 1\n", encoding="utf-8")
    projection = _projection()

    artifact = stage_fha_hosted_source(
        application_root=application_root,
        stage_root=tmp_path / "staged",
        selected_relative_paths=["package/worker.py", "main.agent.md"],
        dependency_pins=_pins(),
        projection=projection,
    )

    assert artifact.selected_relative_paths == (
        PurePosixPath("main.agent.md"),
        PurePosixPath("package/worker.py"),
    )
    assert (artifact.stage_root / "main.agent.md").read_text(encoding="utf-8") == "agent"
    assert (artifact.stage_root / "package" / "worker.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert artifact.requirements_path.read_text(encoding="utf-8") == (
        "azurefunctions-agents-runtime==0.1.0\n"
        "azure-ai-agentserver-core==2.1.0b1\n"
        "azure-ai-agentserver-responses==2.1.0b1\n"
    )
    entrypoint = artifact.entrypoint_path.read_text(encoding="utf-8")
    assert "create_fha_resilient_responses_host" in entrypoint
    assert "compile_fha_v0_project" in entrypoint
    assert "load_fha_runtime_projection" in entrypoint
    assert "AZURE_FUNCTIONS_AGENTS_FHA_" not in entrypoint
    assert artifact.projection_path.name == FHA_RUNTIME_PROJECTION_FILENAME
    assert artifact.projection_path.read_bytes() == serialize_fha_runtime_projection(projection).encode(
        "utf-8"
    )


def test_stage_hosted_source_merges_safe_application_requirements(tmp_path: Path) -> None:
    application = tmp_path / "application"
    application.mkdir()
    (application / "main.agent.md").write_text("agent", encoding="utf-8")
    (application / "requirements.txt").write_text(
        "httpx==0.28.1\n"
        "azurefunctions-agents-runtime==0.1.0\n",
        encoding="utf-8",
    )

    artifact = stage_fha_hosted_source(
        application_root=application,
        stage_root=tmp_path / "stage",
        selected_relative_paths=("main.agent.md", "requirements.txt"),
        dependency_pins=_pins(),
        projection=_projection(),
    )

    assert artifact.requirements_path.read_text(encoding="utf-8").endswith(
        "azure-ai-agentserver-responses==2.1.0b1\nhttpx==0.28.1\n"
    )


def test_runtime_pin_is_inferred_from_local_application_wheel(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "./wheels/azurefunctions_agents_runtime-0.1.0b11-py3-none-any.whl[monitor]\n",
        encoding="utf-8",
    )

    assert resolve_fha_runtime_pin(tmp_path) == (
        "azurefunctions-agents-runtime==0.1.0b11"
    )


@pytest.mark.parametrize(
    "requirement",
    [
        "--extra-index-url https://example.test/simple",
        "package @ https://example.test/package.whl",
        "-e ../package",
        "azure-ai-agentserver-core==9.9.9",
    ],
)
def test_stage_hosted_source_rejects_unsafe_or_conflicting_requirements(
    tmp_path: Path,
    requirement: str,
) -> None:
    application = tmp_path / "application"
    application.mkdir()
    (application / "main.agent.md").write_text("agent", encoding="utf-8")
    (application / "requirements.txt").write_text(requirement + "\n", encoding="utf-8")

    with pytest.raises(FhaHostedSourceStagingError):
        stage_fha_hosted_source(
            application_root=application,
            stage_root=tmp_path / "stage",
            selected_relative_paths=("main.agent.md", "requirements.txt"),
            dependency_pins=_pins(),
            projection=_projection(),
        )


@pytest.mark.parametrize(
    "selected_path",
    [".env", "local.settings.json", "cert.pem", FHA_RUNTIME_PROJECTION_FILENAME, "../outside.py"],
)
def test_stage_hosted_source_rejects_secret_and_escaping_paths(
    tmp_path: Path,
    selected_path: str,
) -> None:
    application_root = tmp_path / "application"
    application_root.mkdir()
    if not selected_path.startswith(".."):
        (application_root / selected_path).write_text("secret", encoding="utf-8")

    with pytest.raises(FhaHostedSourceStagingError):
        stage_fha_hosted_source(
            application_root=application_root,
            stage_root=tmp_path / "staged",
            selected_relative_paths=[selected_path],
            dependency_pins=_pins(),
            projection=_projection(),
        )


def test_stage_hosted_source_rejects_a_missing_runtime_projection(tmp_path: Path) -> None:
    application_root = tmp_path / "application"
    application_root.mkdir()
    (application_root / "main.agent.md").write_text("agent", encoding="utf-8")

    with pytest.raises(FhaHostedSourceStagingError, match="projection"):
        stage_fha_hosted_source(
            application_root=application_root,
            stage_root=tmp_path / "staged",
            selected_relative_paths=["main.agent.md"],
            dependency_pins=_pins(),
            projection=cast(FhaRuntimeProjection, None),
        )

    assert not (tmp_path / "staged").exists()


@pytest.mark.parametrize(
    "runtime_requirement",
    [
        "azurefunctions-agents-runtime>=0.1.0",
        "azurefunctions-agents-runtime==0.1.0#not-a-version",
    ],
)
def test_stage_hosted_source_requires_exact_dependency_pins(runtime_requirement: str) -> None:
    with pytest.raises(FhaHostedSourceStagingError, match="exact pin"):
        FhaHostedDependencyPins.create(
            runtime=runtime_requirement,
            agentserver_core="azure-ai-agentserver-core==2.1.0b1",
            agentserver_responses="azure-ai-agentserver-responses==2.1.0b1",
        )


def test_stage_hosted_source_rejects_a_nonempty_or_nested_destination(tmp_path: Path) -> None:
    application_root = tmp_path / "application"
    application_root.mkdir()
    (application_root / "main.agent.md").write_text("agent", encoding="utf-8")
    occupied_stage = tmp_path / "occupied"
    occupied_stage.mkdir()
    (occupied_stage / "old.txt").write_text("old", encoding="utf-8")

    with pytest.raises(FhaHostedSourceStagingError, match="must be empty"):
        stage_fha_hosted_source(
            application_root=application_root,
            stage_root=occupied_stage,
            selected_relative_paths=["main.agent.md"],
            dependency_pins=_pins(),
            projection=_projection(),
        )
    with pytest.raises(FhaHostedSourceStagingError, match="outside"):
        stage_fha_hosted_source(
            application_root=application_root,
            stage_root=application_root / "staged",
            selected_relative_paths=["main.agent.md"],
            dependency_pins=_pins(),
            projection=_projection(),
        )


def test_stage_hosted_source_copies_v0_capability_inputs_without_environment_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = tmp_path / "application"
    application.mkdir()
    (application / "main.agent.md").write_text("agent", encoding="utf-8")
    (application / "mcp.json").write_text('{"servers":{}}', encoding="utf-8")
    tool_path = application / "tools" / "calculator.py"
    tool_path.parent.mkdir()
    tool_path.write_text("def calculate() -> int:\n    return 1\n", encoding="utf-8")
    skill_path = application / "skills" / "writer" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("# Writer\n", encoding="utf-8")
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://untrusted.example.test/project")
    monkeypatch.setenv("FOUNDRY_MODEL", "untrusted-model")

    projection = _projection()
    artifact = stage_fha_hosted_source(
        application_root=application,
        stage_root=tmp_path / "staged",
        selected_relative_paths=(
            "main.agent.md",
            "mcp.json",
            "tools/calculator.py",
            "skills/writer/SKILL.md",
        ),
        dependency_pins=_pins(),
        projection=projection,
    )

    assert (artifact.stage_root / "mcp.json").read_text(encoding="utf-8") == '{"servers":{}}'
    assert (artifact.stage_root / "tools" / "calculator.py").exists()
    assert (artifact.stage_root / "skills" / "writer" / "SKILL.md").exists()
    assert artifact.projection_path.read_bytes() == projection.serialize().encode("utf-8")
    assert b"untrusted" not in artifact.projection_path.read_bytes()
