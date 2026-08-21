from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from azure_functions_agents.execution.foundry_application_content import (
    build_application_content_manifest,
    compute_application_content_digest,
    serialize_application_content_manifest,
)
from azure_functions_agents.execution.foundry_responses_binding import (
    FHA_APPLICATION_CONTENT_DIGEST_ENV,
    FHA_APPLICATION_CONTENT_MANIFEST_ENV,
    FHA_BINDING_ENV_NAMES,
    FHA_BINDING_FINGERPRINT_ENV,
    FHA_MANAGED_AGENT_NAME_ENV,
    FHA_MANAGED_AGENT_VERSION_ENV,
    FHA_PROJECT_ENDPOINT_ENV,
    FHA_PROJECT_RESOURCE_ID_ENV,
    FHA_WRAPPER_DIGEST_ENV,
    MAX_FHA_BINDING_ENV_VALUE_BYTES,
    FoundryResponsesBindingError,
    FoundryResponsesBindingState,
    compute_foundry_responses_binding_fingerprint,
    inspect_foundry_responses_runtime_binding,
    resolve_foundry_responses_runtime_binding,
)
from azure_functions_agents.foundry_responses import fha_resilient_responses_entrypoint
from azure_functions_agents.foundry_responses.fha_resilient_responses_entrypoint import (
    render_fha_hosted_responses_entrypoint,
)
from azure_functions_agents.foundry_responses.fha_runtime_projection import (
    FhaRuntimeProjection,
    compute_fha_wrapper_digest,
)
from azure_functions_agents.session_state import AppIdentity

_PROJECT_RESOURCE_ID = (
    "/subscriptions/11111111-2222-3333-4444-555555555555"
    "/resourceGroups/agents-rg"
    "/providers/Microsoft.CognitiveServices/accounts/agents/projects/hosted"
)
_APP_IDENTITY = AppIdentity.create(
    subscription_id="11111111-2222-3333-4444-555555555555",
    site_name="agent-app",
)


def _write(root: Path, relative_path: str, content: bytes) -> Path:
    path = root.joinpath(*relative_path.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _application_root(root: Path) -> None:
    _write(root, "main.agent.md", b"---\nname: Main\n---\n")
    _write(root, "agents.config.yaml", b"version: 1\n")


def _projection(*, default_model: str = "gpt-model") -> FhaRuntimeProjection:
    return FhaRuntimeProjection.create(
        project_endpoint="https://project.services.ai.azure.com/api/projects/demo",
        default_model=default_model,
        catalog=(),
    )


def _binding_environment(
    root: Path,
    *,
    projection: FhaRuntimeProjection | None = None,
) -> dict[str, str]:
    manifest = build_application_content_manifest(root)
    runtime_projection = projection or _projection()
    environment = {
        FHA_PROJECT_ENDPOINT_ENV: "https://project.services.ai.azure.com/api/projects/demo/",
        FHA_PROJECT_RESOURCE_ID_ENV: _PROJECT_RESOURCE_ID,
        FHA_MANAGED_AGENT_NAME_ENV: "functions-hosted-agent",
        FHA_MANAGED_AGENT_VERSION_ENV: "2026.08.14",
        FHA_APPLICATION_CONTENT_MANIFEST_ENV: serialize_application_content_manifest(manifest),
        FHA_APPLICATION_CONTENT_DIGEST_ENV: compute_application_content_digest(root, manifest),
        FHA_WRAPPER_DIGEST_ENV: compute_fha_wrapper_digest(
            runtime_projection,
            render_fha_hosted_responses_entrypoint(),
        ),
    }
    environment[FHA_BINDING_FINGERPRINT_ENV] = compute_foundry_responses_binding_fingerprint(
        app_identity=_APP_IDENTITY,
        project_endpoint=environment[FHA_PROJECT_ENDPOINT_ENV],
        project_resource_id=environment[FHA_PROJECT_RESOURCE_ID_ENV],
        managed_agent_name=environment[FHA_MANAGED_AGENT_NAME_ENV],
        managed_agent_version=environment[FHA_MANAGED_AGENT_VERSION_ENV],
        application_content_manifest=environment[FHA_APPLICATION_CONTENT_MANIFEST_ENV],
        application_content_digest=environment[FHA_APPLICATION_CONTENT_DIGEST_ENV],
        wrapper_digest=environment[FHA_WRAPPER_DIGEST_ENV],
    )
    return environment


def test_all_absent_binding_is_explicitly_disabled() -> None:
    resolution = inspect_foundry_responses_runtime_binding({"UNRELATED": "value"})

    assert resolution.state is FoundryResponsesBindingState.DISABLED
    assert resolution.binding is None
    assert resolution.error is None
    assert resolution.is_enabled is False
    assert resolve_foundry_responses_runtime_binding({"UNRELATED": "value"}) is None


def test_complete_binding_is_enabled_and_validates_current_application_content(
    tmp_path: Path,
) -> None:
    _application_root(tmp_path)
    environment = _binding_environment(tmp_path)

    resolution = inspect_foundry_responses_runtime_binding(environment)
    binding = resolve_foundry_responses_runtime_binding(environment)

    assert resolution.state is FoundryResponsesBindingState.ENABLED
    assert resolution.binding == binding
    assert binding is not None
    assert binding.project_endpoint == "https://project.services.ai.azure.com/api/projects/demo"
    assert binding.project_resource_id == _PROJECT_RESOURCE_ID
    assert (
        binding.application_content_manifest_json
        == environment[FHA_APPLICATION_CONTENT_MANIFEST_ENV]
    )
    binding.validate_application_content(tmp_path)
    binding.validate_runtime_projection(_projection())
    binding.validate_fingerprint(_APP_IDENTITY)


def test_partial_binding_is_explicitly_invalid_and_never_falls_back() -> None:
    environment = {
        FHA_PROJECT_ENDPOINT_ENV: "https://project.services.ai.azure.com/api/projects/demo"
    }

    resolution = inspect_foundry_responses_runtime_binding(environment)

    assert resolution.state is FoundryResponsesBindingState.INVALID
    assert resolution.binding is None
    assert resolution.error is not None
    assert resolution.error.fields == frozenset(FHA_BINDING_ENV_NAMES[1:])
    assert "project.services.ai.azure.com" not in str(resolution.error)
    with pytest.raises(FoundryResponsesBindingError):
        resolve_foundry_responses_runtime_binding(environment)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (FHA_PROJECT_ENDPOINT_ENV, "http://project.services.ai.azure.com/api/projects/demo"),
        (
            FHA_PROJECT_ENDPOINT_ENV,
            "https://project.services.ai.azure.com/api/projects/demo?token=x",
        ),
        (FHA_PROJECT_RESOURCE_ID_ENV, "/subscriptions/not-a-guid"),
        (FHA_PROJECT_RESOURCE_ID_ENV, _PROJECT_RESOURCE_ID.removesuffix("hosted") + ".."),
        (FHA_MANAGED_AGENT_NAME_ENV, "invalid name"),
        (FHA_MANAGED_AGENT_VERSION_ENV, " version"),
        (FHA_APPLICATION_CONTENT_MANIFEST_ENV, "{}"),
        (FHA_APPLICATION_CONTENT_DIGEST_ENV, "sha256:" + ("A" * 64)),
        (FHA_WRAPPER_DIGEST_ENV, "not-a-digest"),
        (FHA_BINDING_FINGERPRINT_ENV, "not-a-fingerprint"),
    ],
)
def test_invalid_complete_binding_is_fail_closed(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    _application_root(tmp_path)
    environment = _binding_environment(tmp_path)
    environment[name] = value

    resolution = inspect_foundry_responses_runtime_binding(environment)

    assert resolution.state is FoundryResponsesBindingState.INVALID
    assert resolution.binding is None
    assert resolution.error is not None
    assert resolution.error.fields == frozenset({name})
    with pytest.raises(FoundryResponsesBindingError):
        resolve_foundry_responses_runtime_binding(environment)


def test_binding_rejects_blank_and_oversized_environment_values(tmp_path: Path) -> None:
    _application_root(tmp_path)
    blank = _binding_environment(tmp_path)
    blank[FHA_MANAGED_AGENT_NAME_ENV] = " "

    blank_resolution = inspect_foundry_responses_runtime_binding(blank)

    assert blank_resolution.state is FoundryResponsesBindingState.INVALID
    assert blank_resolution.error is not None
    assert blank_resolution.error.fields == frozenset({FHA_MANAGED_AGENT_NAME_ENV})

    oversized = _binding_environment(tmp_path)
    oversized[FHA_PROJECT_ENDPOINT_ENV] = "https://" + ("a" * MAX_FHA_BINDING_ENV_VALUE_BYTES)
    oversized_resolution = inspect_foundry_responses_runtime_binding(oversized)

    assert oversized_resolution.state is FoundryResponsesBindingState.INVALID
    assert oversized_resolution.error is not None
    assert oversized_resolution.error.fields == frozenset({FHA_PROJECT_ENDPOINT_ENV})


def test_fha_and_aca_binding_coexistence_is_fail_closed(tmp_path: Path) -> None:
    _application_root(tmp_path)
    environment = _binding_environment(tmp_path)

    resolution = inspect_foundry_responses_runtime_binding(
        environment,
        aca_sandbox_configured=True,
    )

    assert resolution.state is FoundryResponsesBindingState.INVALID
    assert resolution.error is not None
    assert resolution.error.fields == frozenset({"session_runtime.aca_sandbox"})
    with pytest.raises(FoundryResponsesBindingError):
        resolve_foundry_responses_runtime_binding(
            environment,
            aca_sandbox_configured=True,
        )


def test_binding_detects_stale_application_content_without_reclassifying_it_as_disabled(
    tmp_path: Path,
) -> None:
    _application_root(tmp_path)
    binding = resolve_foundry_responses_runtime_binding(_binding_environment(tmp_path))
    assert binding is not None
    _write(tmp_path, "main.agent.md", b"---\nname: Test\n---\n")

    with pytest.raises(FoundryResponsesBindingError) as error:
        binding.validate_application_content(tmp_path)

    assert error.value.fields == frozenset({FHA_APPLICATION_CONTENT_DIGEST_ENV})


def test_wrapper_digest_is_bound_but_does_not_change_application_content_digest(
    tmp_path: Path,
) -> None:
    _application_root(tmp_path)
    first_environment = _binding_environment(tmp_path)
    second_environment = _binding_environment(tmp_path)
    second_environment[FHA_WRAPPER_DIGEST_ENV] = "sha256:" + ("c" * 64)

    first = resolve_foundry_responses_runtime_binding(first_environment)
    second = resolve_foundry_responses_runtime_binding(second_environment)

    assert first is not None
    assert second is not None
    assert first.application_content_digest == second.application_content_digest
    assert first.wrapper_digest != second.wrapper_digest
    with pytest.raises(FoundryResponsesBindingError) as runtime_projection_error:
        second.validate_runtime_projection(_projection())
    assert runtime_projection_error.value.fields == frozenset({FHA_WRAPPER_DIGEST_ENV})
    with pytest.raises(FoundryResponsesBindingError):
        second.validate_fingerprint(_APP_IDENTITY)


def test_binding_fingerprint_rejects_another_function_app_identity(tmp_path: Path) -> None:
    _application_root(tmp_path)
    binding = resolve_foundry_responses_runtime_binding(_binding_environment(tmp_path))
    other_identity = AppIdentity.create(
        subscription_id="11111111-2222-3333-4444-555555555555",
        site_name="other-app",
    )

    assert binding is not None
    binding.validate_fingerprint(_APP_IDENTITY)
    with pytest.raises(FoundryResponsesBindingError) as exc_info:
        binding.validate_fingerprint(other_identity)

    assert exc_info.value.fields == frozenset({FHA_BINDING_FINGERPRINT_ENV})


def test_binding_rejects_noncanonical_manifest_environment_value(tmp_path: Path) -> None:
    _application_root(tmp_path)
    environment = _binding_environment(tmp_path)
    environment[FHA_APPLICATION_CONTENT_MANIFEST_ENV] += " "

    resolution = inspect_foundry_responses_runtime_binding(environment)

    assert resolution.state is FoundryResponsesBindingState.INVALID
    assert resolution.error is not None
    assert resolution.error.fields == frozenset({FHA_APPLICATION_CONTENT_MANIFEST_ENV})


def test_runtime_projection_wrapper_digest_changes_with_projection_and_entrypoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _application_root(tmp_path)
    projection = _projection()
    changed_projection = _projection(default_model="another-model")
    binding = resolve_foundry_responses_runtime_binding(
        _binding_environment(tmp_path, projection=projection)
    )

    assert binding is not None
    assert compute_fha_wrapper_digest(projection, "entrypoint-a") != compute_fha_wrapper_digest(
        projection,
        "entrypoint-b",
    )
    assert compute_fha_wrapper_digest(projection, "entrypoint-a") != compute_fha_wrapper_digest(
        changed_projection,
        "entrypoint-a",
    )
    binding.validate_runtime_projection(projection)
    with pytest.raises(FoundryResponsesBindingError) as changed_error:
        binding.validate_runtime_projection(changed_projection)
    assert changed_error.value.fields == frozenset({FHA_WRAPPER_DIGEST_ENV})
    monkeypatch.setattr(
        fha_resilient_responses_entrypoint,
        "render_fha_hosted_responses_entrypoint",
        lambda: "entrypoint-changed",
    )
    with pytest.raises(FoundryResponsesBindingError) as entrypoint_error:
        binding.validate_runtime_projection(projection)
    assert entrypoint_error.value.fields == frozenset({FHA_WRAPPER_DIGEST_ENV})
    with pytest.raises(FoundryResponsesBindingError) as missing_error:
        binding.validate_runtime_projection(cast(FhaRuntimeProjection, None))
    assert missing_error.value.fields == frozenset({FHA_WRAPPER_DIGEST_ENV})


def test_binding_environment_surface_remains_eight_values() -> None:
    assert len(FHA_BINDING_ENV_NAMES) == 8
