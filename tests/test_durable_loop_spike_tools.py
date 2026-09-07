"""Tests for the private durable-loop infrastructure and qualification tooling."""

from __future__ import annotations

import json
import os
import time
import urllib.request
import zipfile
from argparse import Namespace
from pathlib import Path

import pytest
from eng.scripts import durable_loop_spike, durable_loop_spike_qualification
from eng.scripts.durable_loop_spike import (
    DurableLoopDeploymentError,
    deploy,
    stage_application,
    write_deterministic_archive,
)
from eng.scripts.durable_loop_spike_qualification import (
    QualificationRequestError,
    QualificationResult,
    _auth_headers,
    parse_field_selections,
    parse_template_values,
    perform_request,
    render_result,
    render_route,
    select_response_fields,
)

_COMMIT = "3429e2ccce77fa0ad0a86c9dde9cdb7c58e66916"


def _write_source(root: Path) -> None:
    root.mkdir()
    (root / "function_app.py").write_text("app = object()\n", encoding="utf-8")
    (root / "host.json").write_text('{"version":"2.0"}\n', encoding="utf-8")
    (root / "local.settings.json").write_text('{"secret":"value"}\n', encoding="utf-8")
    (root / "local.settings.template.json").write_text("{}\n", encoding="utf-8")
    (root / "ASSEMBLY_SLOT.md").write_text("placeholder\n", encoding="utf-8")


def _write_wheel(root: Path) -> Path:
    root.mkdir()
    wheel = root / "azurefunctions_agents_runtime-0.0.0-py3-none-any.whl"
    wheel.write_bytes(b"wheel-bytes")
    return wheel


def test_stage_requires_final_function_app(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    wheel = _write_wheel(tmp_path / "dist")

    with pytest.raises(
        DurableLoopDeploymentError,
        match=r"application_source_incomplete:function_app\.py",
    ):
        stage_application(
            source_root=source,
            wheel_path=wheel,
            staging_root=tmp_path / "staging",
            commit_sha=_COMMIT,
        )


def test_stage_excludes_local_content_and_writes_manifest(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_source(source)
    wheel = _write_wheel(tmp_path / "dist")
    staging = tmp_path / "staging"

    stage_application(
        source_root=source,
        wheel_path=wheel,
        staging_root=staging,
        commit_sha=_COMMIT,
    )

    assert (staging / "function_app.py").is_file()
    assert not (staging / "local.settings.json").exists()
    assert not (staging / "local.settings.template.json").exists()
    assert not (staging / "ASSEMBLY_SLOT.md").exists()
    requirements = (staging / "requirements.txt").read_text(encoding="utf-8")
    assert requirements == f"./{wheel.name}\n"
    manifest = json.loads((staging / "DEPLOYMENT_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["commit_sha"] == _COMMIT
    assert manifest["wheel"]["filename"] == wheel.name
    assert "local.settings.json" not in manifest["files"]


def test_deterministic_archive_ignores_file_mtimes(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    source = staging / "function_app.py"
    source.write_text("app = object()\n", encoding="utf-8")
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    first_digest = write_deterministic_archive(staging, first)
    changed_time = time.time() + 3600
    os.utime(source, (changed_time, changed_time))
    second_digest = write_deterministic_archive(staging, second)

    assert first_digest == second_digest
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == ["function_app.py"]
        assert archive.getinfo("function_app.py").date_time == (1980, 1, 1, 0, 0, 0)


def test_deploy_requires_exact_existing_app_acknowledgment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "app.zip"
    archive.write_bytes(b"zip")
    calls: list[tuple[str, ...]] = []

    def fake_run_az(arguments: tuple[str, ...], *, timeout_seconds: float) -> None:
        del timeout_seconds
        calls.append(arguments)

    monkeypatch.setattr(durable_loop_spike, "_run_az", fake_run_az)
    deploy(
        Namespace(
            archive_path=str(archive),
            resource_group="larohra-durable-agent-loop",
            app_name="func-durable-loop-0904",
            acknowledge_existing_app="func-durable-loop-0904",
        )
    )

    assert calls[0][:2] == ("functionapp", "show")
    assert calls[1][:4] == ("functionapp", "deployment", "source", "config-zip")
    assert calls[1][-2:] == ("--build-remote", "true")
    assert all("appsettings" not in call for call in calls)


def test_deploy_rejects_mismatched_acknowledgment(tmp_path: Path) -> None:
    archive = tmp_path / "app.zip"
    archive.write_bytes(b"zip")

    with pytest.raises(
        DurableLoopDeploymentError,
        match="existing_app_acknowledgment_mismatch",
    ):
        deploy(
            Namespace(
                archive_path=str(archive),
                resource_group="larohra-durable-agent-loop",
                app_name="func-durable-loop-0904",
                acknowledge_existing_app="another-app",
            )
        )


def test_route_templates_are_explicit_and_injection_safe() -> None:
    values = parse_template_values(("run_id=run-123", "request_id=request_7"))
    assert (
        render_route(
            "/api/status/{run_id}/{request_id}",
            values,
        )
        == "/api/status/run-123/request_7"
    )

    with pytest.raises(QualificationRequestError, match="route_value_invalid"):
        parse_template_values(("run_id=../secret",))
    with pytest.raises(QualificationRequestError, match="route_values_mismatch"):
        render_route("/api/status/{run_id}", {})


def test_response_selection_allows_only_control_metadata() -> None:
    body = json.dumps(
        {
            "run_id": "run-123",
            "status": "Waiting",
            "answer": "sensitive response body",
        }
    ).encode()
    selections = parse_field_selections(("run_id", "status"))

    assert select_response_fields(body, selections) == {
        "run_id": "run-123",
        "status": "Waiting",
    }
    with pytest.raises(
        QualificationRequestError,
        match="response_field_selection_invalid",
    ):
        parse_field_selections(("status=answer",))
    with pytest.raises(
        QualificationRequestError,
        match="response_field_selection_invalid",
    ):
        parse_field_selections(("credential",))


@pytest.mark.parametrize(
    ("field", "value", "error_kind"),
    [
        ("status", "sensitive response body", "status"),
        ("run_id", "contains private content", "id"),
        ("status_url", "https://user:credential@example.test/status", "url"),
        ("completed_model_count", -1, "count"),
        ("duration_ms", 999_999_999, "duration"),
    ],
)
def test_response_selection_rejects_invalid_or_sensitive_values(
    field: str,
    value: object,
    error_kind: str,
) -> None:
    with pytest.raises(
        QualificationRequestError,
        match=f"response_field_invalid:{error_kind}",
    ):
        select_response_fields(
            json.dumps({field: value}).encode(),
            parse_field_selections((field,)),
        )


def test_request_output_discards_unselected_response_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        status = 202

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, _: int) -> bytes:
            return b'{"answer":"never print this","run_id":"run-7"}'

    def fake_urlopen(
        request: urllib.request.Request,
        *,
        timeout: int,
    ) -> FakeResponse:
        assert timeout == 120
        assert request.get_header("X-functions-key") == "credential-material"
        return FakeResponse()

    monkeypatch.setattr(
        durable_loop_spike_qualification.urllib.request,
        "urlopen",
        fake_urlopen,
    )
    result = perform_request(
        command="start",
        method="POST",
        url="https://example.test/api/start",
        body=b'{"prompt":"private"}',
        headers={"x-functions-key": "credential-material"},
        timeout_seconds=120,
        maximum_response_bytes=1024,
        field_selections=(),
    )
    rendered = render_result(result)

    assert "never print this" not in rendered
    assert "credential-material" not in rendered
    assert "private" not in rendered
    assert json.loads(rendered)["response_bytes"] > 0


def test_bearer_auth_uses_environment_secret_without_logging_it() -> None:
    headers = _auth_headers(
        {"TOKEN_ENV": "credential-material"},
        header_name=None,
        header_secret_env=None,
        bearer_secret_env="TOKEN_ENV",
    )

    assert headers["Authorization"].startswith("Bearer ")
    assert headers["Authorization"].endswith("credential-material")
    assert "credential-material" not in render_result(
        QualificationResult(
            command="status",
            http_status=200,
            latency_ms=1,
            response_bytes=0,
            selected_fields={},
        )
    )


def test_render_result_contains_only_selected_metadata() -> None:
    rendered = render_result(
        QualificationResult(
            command="status",
            http_status=200,
            latency_ms=12.3456,
            response_bytes=89,
            selected_fields={"status": "Running"},
        )
    )

    assert json.loads(rendered) == {
        "command": "status",
        "http_status": 200,
        "latency_ms": 12.346,
        "response_bytes": 89,
        "selected_fields": {"status": "Running"},
    }


def test_infrastructure_contract_uses_exact_names_and_secure_key_flow() -> None:
    sample = Path("samples/durable-agent-loop-spike")
    main = (sample / "infra/main.bicep").read_text(encoding="utf-8")
    apim = (sample / "infra/modules/apim.bicep").read_text(encoding="utf-8")
    function_app = (sample / "infra/modules/function-app.bicep").read_text(encoding="utf-8")
    foundry = (sample / "infra/modules/foundry.bicep").read_text(encoding="utf-8")
    local_settings = json.loads(
        (sample / "src/local.settings.template.json").read_text(encoding="utf-8")
    )

    for exact_name in (
        "larohra-durable-agent-loop",
        "aidurableloop0904e2",
        "sbg-durable-loop-0904",
        "func-durable-loop-0904",
        "larohra-ai-gateway",
        "durable-agent-loop-model",
        "durable-agent-loop-model-control",
        "durable-agent-loop-mcp",
    ):
        assert exact_name in main
    assert "sharedApimSubscription.listSecrets().primaryKey" in main
    assert "@secure()\nparam apimSubscriptionKey string" in function_app
    assert "AZURE_FUNCTIONS_AGENTS_APIM_MODEL_CONTROL_URL" in function_app
    assert "disableLocalAuth: true" in foundry
    assert "urlTemplate: '/*'" not in apim
    assert "name: 'responses-get'" not in apim
    assert "name: 'responses-delete'" not in apim
    assert "name: 'responses-poll'" in apim
    assert "urlTemplate: '/responses'" in apim
    assert "urlTemplate: '/responses/cancel'" in apim
    assert 'name="x-af-response-id"' in apim
    assert "^resp_[A-Za-z0-9]{16,160}$" in apim
    assert 'exists-action="delete"' in apim
    assert apim.count('<set-header name="api-key" exists-action="delete" />') == 3
    assert apim.count('<set-query-parameter name="subscription-key" exists-action="delete" />') == 3
    assert "${modelBackend.name}" not in apim
    assert apim.count("__MODEL_BACKEND_NAME__") == 4
    assert "resource modelControlAzureMonitorDiagnostic" in apim
    assert "loggerId: azureMonitorLogger.id" in apim
    assert "percentage: 0" in apim
    assert "resource modelControlDiagnostic" not in apim
    assert apim.count("dataMasking:") == 4
    assert apim.count("value: '*'") == 4
    assert "primaryKey:" not in apim
    assert "secondaryKey:" not in apim
    assert "SCM_DO_BUILD_DURING_DEPLOYMENT" not in function_app
    assert local_settings["Values"]["AZURE_FUNCTIONS_AGENTS_APIM_SUBSCRIPTION_KEY"] == ""
    assert local_settings["Values"]["AZURE_FUNCTIONS_AGENTS_APIM_MODEL_CONTROL_URL"].endswith(
        "/durable-agent-loop-model-control"
    )


def test_cleanup_is_exact_and_never_deletes_shared_apim_or_group() -> None:
    readme = Path("samples/durable-agent-loop-spike/README.md").read_text(encoding="utf-8")

    assert "$apimId/apis/durable-agent-loop-model" in readme
    assert "$apimId/apis/durable-agent-loop-model-control" in readme
    assert "$apimId/apis/durable-agent-loop-mcp" in readme
    assert "$apimId/backends/durable-agent-loop-model" in readme
    assert "83.527 seconds" in readme
    assert "327.959 seconds" in readme
    assert "Explicit server-side delete remains the primary completion path" in readme
    assert "az apim delete" not in readme
    assert "az group delete" not in readme
