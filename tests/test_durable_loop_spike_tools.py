"""Tests for the private durable-loop infrastructure and qualification tooling."""

from __future__ import annotations

import io
import json
import os
import time
import urllib.error
import urllib.request
import zipfile
from argparse import Namespace
from pathlib import Path

import pytest
from eng.scripts import durable_loop_spike, durable_loop_spike_qualification
from eng.scripts.durable_loop_spike import (
    DurableLoopDeploymentError,
    build_runtime_wheel,
    deploy,
    stage_application,
    write_deterministic_archive,
)
from eng.scripts.durable_loop_spike_qualification import (
    PollResult,
    QualificationRequestError,
    QualificationResult,
    _auth_headers,
    parse_field_selections,
    parse_template_values,
    perform_request,
    poll_status,
    render_result,
    render_route,
    select_response_fields,
    write_metrics,
)

_COMMIT = "3429e2ccce77fa0ad0a86c9dde9cdb7c58e66916"
_RUN_ID = "run-" + "a" * 32
_HUMAN_REQUEST_ID = "human-7-" + "b" * 16
_HUMAN_INPUT_URL = (
    f"/api/experimental/durable-agent-runs/{_RUN_ID}/input/{_HUMAN_REQUEST_ID}"
)


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
    requirement_lines = requirements.splitlines()
    assert requirement_lines[0] == f"./{wheel.name}[aca_sandbox,monitor]"
    assert requirement_lines[1].startswith("agent-framework-core @ https://github.com/")
    assert requirement_lines[2].startswith("agent-framework-openai @ https://github.com/")
    assert requirement_lines[3].startswith("agent-framework-foundry @ https://github.com/")
    manifest = json.loads((staging / "DEPLOYMENT_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["commit_sha"] == _COMMIT
    assert manifest["wheel"]["filename"] == wheel.name
    assert "local.settings.json" not in manifest["files"]


def test_stage_requirements_keep_fixed_runtime_extras_before_operator_dependencies(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write_source(source)
    wheel = _write_wheel(tmp_path / "dist")
    extra = tmp_path / "requirements.extra.txt"
    extra.write_text("operator-package==1.2.3\n", encoding="utf-8")
    staging = tmp_path / "staging"

    stage_application(
        source_root=source,
        wheel_path=wheel,
        staging_root=staging,
        commit_sha=_COMMIT,
        requirements_extra=extra,
    )

    assert (staging / "requirements.txt").read_text(encoding="utf-8") == (
        f"./{wheel.name}[aca_sandbox,monitor]\n"
        "agent-framework-core @ "
        "https://github.com/microsoft/agent-framework/releases/download/python-1.17.0/"
        "agent_framework_core-1.17.0-py3-none-any.whl"
        "#sha256=1c5c22232fd22cb50bceb61ed380cbefc8fc3c574e80b239d52e20a2cc803a2c\n"
        "agent-framework-openai @ "
        "https://github.com/microsoft/agent-framework/releases/download/python-1.17.0/"
        "agent_framework_openai-1.14.2-py3-none-any.whl"
        "#sha256=2559d923f64c559883d038ceaf66c6ab7250d61d1383cfa9cbbb0e401accccca\n"
        "agent-framework-foundry @ "
        "https://github.com/microsoft/agent-framework/releases/download/python-1.17.0/"
        "agent_framework_foundry-1.12.0-py3-none-any.whl"
        "#sha256=92e2aa2bfa5d9026cbdfb217ca3e7e19fe2c088c2b0592e4a08a191526dff78f\n\n"
        "operator-package==1.2.3\n"
    )


def test_wheel_build_rejects_dist_ancestor_of_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dist = tmp_path / "artifacts" / "dist"
    repo = dist / "repo"
    repo.mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    marker = repo / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    monkeypatch.setattr(
        durable_loop_spike,
        "_run_command",
        lambda *_args, **_kwargs: pytest.fail("build command must not run"),
    )

    with pytest.raises(DurableLoopDeploymentError, match="unsafe_generated_directory:dist"):
        build_runtime_wheel(repo, dist)

    assert marker.read_text(encoding="utf-8") == "keep"


def test_wheel_build_sets_reproducible_source_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    dist = tmp_path / "dist"
    environments: list[dict[str, str]] = []

    def fake_run_command(*_args: object, **kwargs: object) -> None:
        environment = kwargs["environment"]
        assert isinstance(environment, dict)
        environments.append(environment)
        (dist / "azurefunctions_agents_runtime-1.2.3-py3-none-any.whl").write_bytes(
            b"wheel-bytes"
        )

    monkeypatch.setattr(durable_loop_spike, "_run_command", fake_run_command)

    wheel = build_runtime_wheel(repo, dist)

    assert wheel.is_file()
    assert environments[0]["SOURCE_DATE_EPOCH"] == "315532800"


def test_stage_rejects_staging_ancestor_of_source(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    source = staging / "source"
    _write_source(source)
    wheel = _write_wheel(tmp_path / "dist")

    with pytest.raises(DurableLoopDeploymentError, match="unsafe_generated_directory:staging"):
        stage_application(
            source_root=source,
            wheel_path=wheel,
            staging_root=staging,
            commit_sha=_COMMIT,
        )

    assert (source / "function_app.py").is_file()


def test_stage_rejects_staging_ancestor_of_wheel(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_source(source)
    staging = tmp_path / "staging"
    staging.mkdir()
    wheel = _write_wheel(staging / "dist")

    with pytest.raises(DurableLoopDeploymentError, match="unsafe_generated_directory:staging"):
        stage_application(
            source_root=source,
            wheel_path=wheel,
            staging_root=staging,
            commit_sha=_COMMIT,
        )

    assert wheel.read_bytes() == b"wheel-bytes"


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


def test_azure_cli_resolves_windows_command_shim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    azure_cli = r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"

    monkeypatch.setattr(durable_loop_spike.shutil, "which", lambda name: azure_cli)
    monkeypatch.setattr(
        durable_loop_spike,
        "_run_command",
        lambda command, **_kwargs: calls.append(tuple(command)),
    )

    durable_loop_spike._run_az(("account", "show"), timeout_seconds=30)

    assert calls == [(azure_cli, "account", "show", "--only-show-errors", "--output", "none")]


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


def test_qualification_commands_use_exact_default_routes_and_allow_override() -> None:
    parser = durable_loop_spike_qualification._parser()
    expected = {
        "start": "/api/experimental/durable-agent-runs",
        "status": "/api/experimental/durable-agent-runs/{run_id}",
        "poll": "/api/experimental/durable-agent-runs/{run_id}",
        "result": "/api/experimental/durable-agent-runs/{run_id}/result",
        "cancel": "/api/experimental/durable-agent-runs/{run_id}/cancel",
        "human-detail": (
            "/api/experimental/durable-agent-runs/{run_id}/input/{request_id}"
        ),
        "human-answer": (
            "/api/experimental/durable-agent-runs/{run_id}/input/{request_id}"
        ),
    }

    for command, route in expected.items():
        assert parser.parse_args([command]).route_template == route
    assert (
        parser.parse_args(["status", "--route-template", "/controlled/{run_id}"])
        .route_template
        == "/controlled/{run_id}"
    )


def test_route_templates_are_injection_safe() -> None:
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
    with pytest.raises(QualificationRequestError, match="route_template_invalid"):
        render_route("/api/status/{run_id!r}", {"run_id": "run-123"})
    with pytest.raises(QualificationRequestError, match="route_template_invalid"):
        render_route("/api/status/{run_id}?answer=secret", {"run_id": "run-123"})


def test_response_selection_allows_fixed_top_level_and_nested_control_metadata() -> None:
    body = json.dumps(
        {
            "run_id": _RUN_ID,
            "session_id": "session-7",
            "status": "Waiting",
            "phase": "human_wait",
            "model_steps": 3,
            "tool_calls": 4,
            "human_waits": 1,
            "step_index": 7,
            "input_tokens": 100,
            "output_tokens": 20,
            "reasoning_tokens": 30,
            "cost_microunits": 400,
            "external_content_bytes": 500,
            "parked_seconds": 6.5,
            "delivery": "delivered",
            "disposition": "Ambiguous",
            "possibly_committed": False,
            "error": "human_input_not_pending",
            "human_input": {
                "request_id": _HUMAN_REQUEST_ID,
                "respond_url": _HUMAN_INPUT_URL,
                "detail_url": _HUMAN_INPUT_URL,
                "expires_at": "2026-09-08T12:00:00Z",
                "allow_free_text": True,
                "choice_count": 2,
                "schema_present": True,
            },
            "answer": "sensitive response body",
        }
    ).encode()
    selections = parse_field_selections(
        (
            "run_id",
            "session_id",
            "status",
            "phase",
            "model_steps",
            "tool_calls",
            "human_waits",
            "step_index",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cost_microunits",
            "external_content_bytes",
            "parked_seconds",
            "delivery",
            "disposition",
            "possibly_committed",
            "error_code",
            "human_request_id",
            "human_respond_url",
            "human_detail_url",
            "human_expires_at",
            "human_allow_free_text",
            "human_choice_count",
            "human_schema_present",
        )
    )

    selected = select_response_fields(body, selections)
    assert selected["human_request_id"] == _HUMAN_REQUEST_ID
    assert selected["human_choice_count"] == 2
    assert selected["human_detail_url"] == _HUMAN_INPUT_URL
    assert selected["human_schema_present"] is True
    assert selected["possibly_committed"] is False
    assert selected["model_steps"] == 3
    assert "question" not in selected
    assert "choices" not in selected
    assert "response_schema" not in selected
    assert "answer" not in selected


@pytest.mark.parametrize(
    "field",
    [
        "prompt",
        "answer",
        "final_response",
        "tool_data",
        "provider_id",
        "credential",
        "human_input.question",
        "human_input.response_schema",
        "completed_model_count",
        "human_answer_url",
    ],
)
def test_response_selection_rejects_content_fields_and_legacy_aliases(field: str) -> None:
    with pytest.raises(
        QualificationRequestError,
        match="response_field_selection_invalid",
    ):
        parse_field_selections((field,))
    with pytest.raises(
        QualificationRequestError,
        match="response_field_selection_invalid",
    ):
        parse_field_selections(("status=answer",))


@pytest.mark.parametrize(
    ("field", "value", "error_kind"),
    [
        ("status", "sensitive response body", "status"),
        ("run_id", "contains private content", "run_id"),
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
    if field in {"status_url", "completed_model_count", "duration_ms"}:
        with pytest.raises(
            QualificationRequestError,
            match="response_field_selection_invalid",
        ):
            parse_field_selections((field,))
        return
    with pytest.raises(
        QualificationRequestError,
        match=f"response_field_invalid:{error_kind}",
    ):
        select_response_fields(
            json.dumps({field: value}).encode(),
            parse_field_selections((field,)),
        )


@pytest.mark.parametrize(
    ("field", "payload", "error_kind"),
    [
        ("model_steps", {"model_steps": -1}, "model_steps"),
        ("input_tokens", {"input_tokens": 1_000_000_001}, "input_tokens"),
        ("parked_seconds", {"parked_seconds": 999_999_999}, "parked_seconds"),
        ("delivery", {"delivery": "contains private content"}, "delivery"),
        ("phase", {"phase": "sk-live-secret"}, "phase"),
        ("error_code", {"error": "sk-live-secret"}, "error_code"),
        ("possibly_committed", {"possibly_committed": "false"}, "bool"),
        (
            "human_respond_url",
            {
                "run_id": _RUN_ID,
                "human_input": {
                    "request_id": _HUMAN_REQUEST_ID,
                    "respond_url": "/api/respond/private-answer",
                },
            },
            "human_respond_url",
        ),
        (
            "human_expires_at",
            {"human_input": {"expires_at": "tomorrow"}},
            "timestamp",
        ),
        (
            "human_choice_count",
            {"human_input": {"choice_count": 101}},
            "human_choice_count",
        ),
    ],
)
def test_actual_response_selectors_reject_invalid_values(
    field: str,
    payload: object,
    error_kind: str,
) -> None:
    with pytest.raises(
        QualificationRequestError,
        match=f"response_field_invalid:{error_kind}",
    ):
        select_response_fields(
            json.dumps(payload).encode(),
            parse_field_selections((field,)),
        )


@pytest.mark.parametrize(
    ("body", "selections"),
    [
        (b'{"run_id":"first","run_id":"second"}', ()),
        (
            b'{"human_input":{"request_id":"first","request_id":"second"}}',
            ("human_request_id",),
        ),
    ],
)
def test_response_documents_reject_duplicate_keys(
    body: bytes,
    selections: tuple[str, ...],
) -> None:
    with pytest.raises(
        QualificationRequestError,
        match="response_body_duplicate_key",
    ):
        select_response_fields(body, selections)


def test_request_documents_reject_duplicate_keys_without_echoing_content(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    body = tmp_path / "private-start.json"
    body.write_text(
        '{"prompt":"first secret","prompt":"second secret","request_id":"request-1"}',
        encoding="utf-8",
    )

    exit_code = durable_loop_spike_qualification.main(
        ["start", "--body-file", str(body)],
        {},
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == (
        "Durable loop qualification failed: request_body_duplicate_key\n"
    )
    assert "secret" not in captured.err
    assert str(body) not in captured.err


def test_start_and_human_answer_enforce_idempotency_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    start_body = tmp_path / "start.json"
    start_body.write_text('{"prompt":"private prompt"}', encoding="utf-8")

    with pytest.raises(QualificationRequestError, match="start_idempotency_required"):
        durable_loop_spike_qualification._validate_request_payload(
            "start",
            {"prompt": "private prompt"},
            has_idempotency_key=False,
        )
    with pytest.raises(
        QualificationRequestError,
        match="human_answer_idempotency_required",
    ):
        durable_loop_spike_qualification._validate_request_payload(
            "human-answer",
            {"answer": "private answer"},
            has_idempotency_key=False,
        )

    class FakeResponse:
        status = 202

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, _: int) -> bytes:
            return json.dumps({"run_id": _RUN_ID}).encode()

    def fake_urlopen(
        request: urllib.request.Request,
        *,
        timeout: int,
    ) -> FakeResponse:
        assert timeout == 120
        assert request.get_header("Idempotency-key") == "request-header-1"
        assert request.full_url.endswith("/api/experimental/durable-agent-runs")
        return FakeResponse()

    monkeypatch.setattr(
        durable_loop_spike_qualification,
        "_urlopen",
        fake_urlopen,
    )
    assert (
        durable_loop_spike_qualification.main(
            [
                "start",
                "--body-file",
                str(start_body),
                "--idempotency-key-env",
                "IDEMPOTENCY_ENV",
                "--extract",
                "run_id",
            ],
            {"IDEMPOTENCY_ENV": "request-header-1"},
        )
        == 0
    )
    rendered = capsys.readouterr().out
    assert "private prompt" not in rendered
    assert "request-header-1" not in rendered


@pytest.mark.parametrize(
    ("payload", "error_code"),
    [
        ({"prompt": "private", "request_id": None}, "start_request_id_invalid"),
        (
            {"prompt": "private", "request_id": "request-1", "session_id": None},
            "start_session_id_invalid",
        ),
        ({"prompt": "private", "unexpected": True}, "start_body_field_invalid"),
        ({"answer": None}, "human_answer_invalid"),
        (
            {"answer": "private", "unexpected": True},
            "human_answer_body_invalid",
        ),
    ],
)
def test_request_contract_rejects_invalid_shapes(
    payload: dict[str, object],
    error_code: str,
) -> None:
    command = "human-answer" if "answer" in payload else "start"
    with pytest.raises(QualificationRequestError, match=error_code):
        durable_loop_spike_qualification._validate_request_payload(
            command,
            payload,
            has_idempotency_key=True,
        )


def test_missing_body_file_error_is_content_free(
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_path = str(Path.cwd() / "private-prompt-does-not-exist.json")

    assert (
        durable_loop_spike_qualification.main(
            ["start", "--body-file", secret_path],
            {},
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.err == (
        "Durable loop qualification failed: request_body_read_failed\n"
    )
    assert secret_path not in captured.err


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
        durable_loop_spike_qualification,
        "_urlopen",
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


def test_http_error_is_counted_and_only_safe_error_code_is_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_urlopen(
        request: urllib.request.Request,
        *,
        timeout: int,
    ) -> None:
        del request, timeout
        raise urllib.error.HTTPError(
            "https://example.test",
            409,
            "private upstream message",
            {},
            io.BytesIO(
                b'{"error":"session_busy","detail":"private response"}'
            ),
        )

    monkeypatch.setattr(
        durable_loop_spike_qualification,
        "_urlopen",
        fake_urlopen,
    )
    result = perform_request(
        command="start",
        method="POST",
        url="https://example.test/api/experimental/durable-agent-runs",
        body=b'{"prompt":"private","request_id":"request-1"}',
        headers={},
        timeout_seconds=120,
        maximum_response_bytes=1024,
        field_selections=("error_code",),
    )

    assert result.http_status == 409
    assert result.selected_fields == {"error_code": "session_busy"}
    assert "private" not in render_result(result)


def test_redirect_handler_rejects_cross_origin_and_downgrade_redirects() -> None:
    handler = durable_loop_spike_qualification._RejectRedirectHandler()
    request = urllib.request.Request(
        "https://example.test/api/status",
        headers={
            "Authorization": "Bearer credential-material",
            "Idempotency-Key": "request-1",
        },
    )

    with pytest.raises(QualificationRequestError, match="redirect_rejected"):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "http://attacker.test/collect",
        )


def test_human_detail_probe_discards_question_choices_and_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        status = 200

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, _: int) -> bytes:
            return json.dumps(
                {
                    "question": "private question",
                    "choices": ["private choice"],
                    "response_schema": {"type": "string"},
                }
            ).encode()

    monkeypatch.setattr(
        durable_loop_spike_qualification,
        "_urlopen",
        lambda *_args, **_kwargs: FakeResponse(),
    )

    rendered = render_result(
        perform_request(
            command="human-detail",
            method="GET",
            url=f"https://example.test{_HUMAN_INPUT_URL}",
            body=None,
            headers={},
            timeout_seconds=120,
            maximum_response_bytes=1024,
            field_selections=(),
        )
    )

    assert "private question" not in rendered
    assert "private choice" not in rendered
    assert "response_schema" not in rendered


class _FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def test_poll_stops_on_completed_and_reports_aggregate_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        (
            QualificationResult(
                command="poll",
                http_status=200,
                latency_ms=10,
                response_bytes=100,
                selected_fields={"status": "Running", "model_steps": 1},
            ),
            QualificationResult(
                command="poll",
                http_status=200,
                latency_ms=30,
                response_bytes=120,
                selected_fields={
                    "status": "Completed",
                    "phase": "terminal",
                    "model_steps": 2,
                    "tool_calls": 1,
                },
            ),
        )
    )
    monkeypatch.setattr(
        durable_loop_spike_qualification,
        "perform_request",
        lambda **_: next(responses),
    )
    clock = _FakeClock()

    result = poll_status(
        url="https://example.test/api/status/run-1",
        headers={},
        timeout_seconds=120,
        maximum_response_bytes=1024,
        interval_seconds=0.5,
        deadline_seconds=10,
        field_selections=(),
        sleeper=clock.sleep,
        clock=clock,
    )

    assert result == PollResult(
        command="poll",
        http_status=200,
        attempts=2,
        total_elapsed_ms=500,
        response_bytes=220,
        request_latency_p50_ms=10,
        request_latency_p95_ms=30,
        timed_out=False,
        selected_fields={
            "status": "Completed",
            "phase": "terminal",
            "model_steps": 2,
            "tool_calls": 1,
        },
    )


def test_poll_stops_on_waiting_and_exposes_only_safe_human_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        durable_loop_spike_qualification,
        "perform_request",
        lambda **_: QualificationResult(
            command="poll",
            http_status=200,
            latency_ms=15,
            response_bytes=150,
            selected_fields={
                "status": "Waiting",
                "run_id": _RUN_ID,
                "human_request_id": _HUMAN_REQUEST_ID,
                "human_respond_url": _HUMAN_INPUT_URL,
                "human_detail_url": _HUMAN_INPUT_URL,
                "human_expires_at": "2026-09-08T12:00:00Z",
                "human_allow_free_text": False,
                "human_choice_count": 2,
                "human_schema_present": True,
            },
        ),
    )
    clock = _FakeClock()

    rendered = render_result(
        poll_status(
            url="https://example.test/api/status/run-1",
            headers={},
            timeout_seconds=120,
            maximum_response_bytes=1024,
            interval_seconds=0.5,
            deadline_seconds=10,
            field_selections=(),
            sleeper=clock.sleep,
            clock=clock,
        )
    )

    assert "question" not in rendered
    assert "response_schema" not in rendered
    assert json.loads(rendered)["selected_fields"]["human_choice_count"] == 2
    assert "request_latency_p95_ms" not in rendered


def test_poll_timeout_is_bounded_and_returns_content_free_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        durable_loop_spike_qualification,
        "perform_request",
        lambda **_: QualificationResult(
            command="poll",
            http_status=200,
            latency_ms=5,
            response_bytes=50,
            selected_fields={"status": "Running"},
        ),
    )
    clock = _FakeClock()

    result = poll_status(
        url="https://example.test/api/status/run-1",
        headers={},
        timeout_seconds=120,
        maximum_response_bytes=1024,
        interval_seconds=0.5,
        deadline_seconds=1,
        field_selections=(),
        sleeper=clock.sleep,
        clock=clock,
    )

    assert result.timed_out is True
    assert result.attempts == 2
    assert result.total_elapsed_ms == 1000
    assert result.selected_fields == {"status": "Running"}


@pytest.mark.parametrize("status", ["Failed", "Cancelled"])
def test_poll_stops_on_other_terminal_statuses(
    status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        durable_loop_spike_qualification,
        "perform_request",
        lambda **_: QualificationResult(
            command="poll",
            http_status=200,
            latency_ms=5,
            response_bytes=50,
            selected_fields={"status": status},
        ),
    )
    clock = _FakeClock()

    result = poll_status(
        url="https://example.test/api/status/run-1",
        headers={},
        timeout_seconds=120,
        maximum_response_bytes=1024,
        interval_seconds=0.5,
        deadline_seconds=10,
        field_selections=(),
        sleeper=clock.sleep,
        clock=clock,
    )

    assert result.attempts == 1
    assert result.selected_fields == {"status": status}


def test_metrics_outputs_are_content_free_and_refuse_repository_paths(
    tmp_path: Path,
) -> None:
    result = QualificationResult(
        command="result",
        http_status=200,
        latency_ms=12.5,
        response_bytes=321,
        selected_fields={"status": "Completed", "tool_calls": 2},
    )
    jsonl_path = tmp_path / "qualification.jsonl"
    json_path = tmp_path / "qualification.json"

    write_metrics(str(jsonl_path), "jsonl", result)
    write_metrics(str(jsonl_path), "jsonl", result)
    write_metrics(str(json_path), "json", result)

    assert len(jsonl_path.read_text(encoding="utf-8").splitlines()) == 2
    json_record = json.loads(json_path.read_text(encoding="utf-8"))
    assert json_record["selected_fields"]["tool_calls"] == 2
    evidence = jsonl_path.read_text(encoding="utf-8")
    assert "prompt" not in evidence
    assert "answer" not in evidence
    assert "credential" not in evidence
    assert "response_body" not in evidence
    with pytest.raises(QualificationRequestError, match="metrics_path_inside_repository"):
        write_metrics(
            str(Path.cwd() / "qualification.jsonl"),
            "jsonl",
            result,
        )
    with pytest.raises(QualificationRequestError, match="result_field_invalid"):
        write_metrics(
            str(tmp_path / "unsafe.jsonl"),
            "jsonl",
            QualificationResult(
                command="result",
                http_status=200,
                latency_ms=1,
                response_bytes=1,
                selected_fields={"prompt": "private"},
            ),
        )


def test_metrics_path_is_validated_before_network_request(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        durable_loop_spike_qualification,
        "_urlopen",
        lambda *_args, **_kwargs: pytest.fail("network request must not start"),
    )

    exit_code = durable_loop_spike_qualification.main(
        [
            "status",
            "--value",
            f"run_id={_RUN_ID}",
            "--metrics-output",
            str(Path.cwd() / "unsafe.jsonl"),
        ],
        {},
    )

    assert exit_code == 1
    assert capsys.readouterr().err == (
        "Durable loop qualification failed: metrics_path_inside_repository\n"
    )


def test_post_request_metrics_failure_has_distinct_exit_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeResponse:
        status = 200

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, _: int) -> bytes:
            return b'{"status":"Running"}'

    monkeypatch.setattr(
        durable_loop_spike_qualification,
        "_urlopen",
        lambda *_args, **_kwargs: FakeResponse(),
    )
    monkeypatch.setattr(
        durable_loop_spike_qualification,
        "_write_metrics_path",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            QualificationRequestError("metrics_write_failed")
        ),
    )

    exit_code = durable_loop_spike_qualification.main(
        [
            "status",
            "--value",
            f"run_id={_RUN_ID}",
            "--metrics-output",
            str(tmp_path / "status.jsonl"),
        ],
        {},
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert json.loads(captured.out)["http_status"] == 200
    assert captured.err == (
        "Durable loop qualification metrics failed after request: "
        "metrics_write_failed\n"
    )


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


def _assert_private_runtime_settings(
    *,
    main: str,
    function_app: str,
    host: dict[str, object],
    local_settings: dict[str, object],
) -> None:
    expected = {
        "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_BACKGROUND_MODEL_ENABLED": "false",
        "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_RETAINED_SANDBOX_ENABLED": "false",
        "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_FAULT_INJECTION_ENABLED": "false",
        "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_MAX_APP_OWNED_SANDBOXES": "10",
        "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_RETAINED_SANDBOX_AUTO_DELETE_SECONDS": "86400",
        "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_SANDBOX_REAPER_AGE_SECONDS": "600",
    }
    values = local_settings["Values"]
    assert isinstance(values, dict)
    for setting, value in expected.items():
        assert f"{setting}: '{value}'" in function_app
        assert values[setting] == value
    assert "param featureGateEnabled bool = false" in main
    assert "'${featureGateSettingName}': string(featureGateEnabled)" in function_app
    assert "FUNCTIONS_WORKER_RUNTIME:" not in function_app
    assert host["functionTimeout"] == "00:30:00"


def test_infrastructure_contract_uses_exact_names_and_secure_key_flow() -> None:
    sample = Path("samples/durable-agent-loop-spike")
    main = (sample / "infra/main.bicep").read_text(encoding="utf-8")
    apim = (sample / "infra/modules/apim.bicep").read_text(encoding="utf-8")
    function_app = (sample / "infra/modules/function-app.bicep").read_text(encoding="utf-8")
    foundry = (sample / "infra/modules/foundry.bicep").read_text(encoding="utf-8")
    storage = (sample / "infra/modules/storage.bicep").read_text(encoding="utf-8")
    local_settings = json.loads(
        (sample / "src/local.settings.template.json").read_text(encoding="utf-8")
    )
    host = json.loads((sample / "src/host.json").read_text(encoding="utf-8"))

    for exact_name in (
        "larohra-durable-agent-loop",
        "aidurableloop0904e2",
        "sbg-durable-loop-0904",
        "func-durable-loop-0904",
        "larohra-ai-gateway",
        "durable-agent-loop-model",
        "durable-agent-loop-model-control",
        "durable-agent-loop-mcp",
        "durable-loop-content",
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
    assert (
        "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_ALLOWED_HOSTS:"
        " '${replace(replace(environment().resourceManager, 'https://', ''), '/', '')},"
        "www.example.com'" in function_app
    )
    assert (
        "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_TOOL_BUNDLE_ROOT:"
        " 'sandbox_bundle'" in function_app
    )
    assert "resource durableContentContainer" in storage
    assert "publicAccess: 'None'" in storage
    assert (
        "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_CONTENT_BLOB_URI:"
        " durableContentBlobUri" in function_app
    )
    assert (
        "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_CONTENT_CONTAINER:"
        " durableContentContainerName" in function_app
    )
    assert (
        "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_CONTENT_CLIENT_ID:"
        " functionIdentityClientId" in function_app
    )
    _assert_private_runtime_settings(
        main=main,
        function_app=function_app,
        host=host,
        local_settings=local_settings,
    )
    assert local_settings["Values"]["AZURE_FUNCTIONS_AGENTS_APIM_SUBSCRIPTION_KEY"] == ""
    assert (
        local_settings["Values"][
            "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_ALLOWED_HOSTS"
        ]
        == "management.azure.com,www.example.com"
    )
    assert (
        local_settings["Values"][
            "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_HYBRID_TOOL_BUNDLE_ROOT"
        ]
        == "sandbox_bundle"
    )
    assert local_settings["Values"]["AZURE_FUNCTIONS_AGENTS_APIM_MODEL_CONTROL_URL"].endswith(
        "/durable-agent-loop-model-control"
    )
    assert (
        local_settings["Values"][
            "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_CONTENT_BLOB_URI"
        ]
        == "https://stdurableloop0904e2.blob.core.windows.net"
    )
    assert (
        local_settings["Values"][
            "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_CONTENT_CONTAINER"
        ]
        == "durable-loop-content"
    )
    assert (
        local_settings["Values"][
            "AZURE_FUNCTIONS_AGENTS_EXPERIMENTAL_DURABLE_AGENT_LOOP_CONTENT_CLIENT_ID"
        ]
        == "<function-uami-client-id>"
    )


def test_cleanup_is_exact_and_never_deletes_shared_apim_or_group() -> None:
    readme = Path("samples/durable-agent-loop-spike/README.md").read_text(encoding="utf-8")

    assert "uv run --with pip python eng\\scripts\\durable_loop_spike.py assemble" in readme
    assert (
        "uv run --with pip python eng\\scripts\\durable_loop_spike_qualification.py poll"
        in readme
    )
    assert "\npython eng\\scripts\\durable_loop_spike" not in readme
    assert "$apimId/apis/durable-agent-loop-model" in readme
    assert "$apimId/apis/durable-agent-loop-model-control" in readme
    assert "$apimId/apis/durable-agent-loop-mcp" in readme
    assert "$apimId/backends/durable-agent-loop-model" in readme
    assert "83.527 seconds" in readme
    assert "327.959 seconds" in readme
    assert "Explicit server-side delete remains the primary completion path" in readme
    assert "az apim delete" not in readme
    assert "az group delete" not in readme
