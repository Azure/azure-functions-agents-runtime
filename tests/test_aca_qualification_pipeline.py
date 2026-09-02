"""Tests for the deployed ACA qualification tooling.

``eng/`` is outside the CI lint and type-check paths (`ruff check src tests`,
`mypy src`), so these tests are the only automated coverage the qualification
tooling gets. They deliberately exercise the pure decision functions -- marker
construction, marker comparison, and package assembly -- rather than the network
commands, because those are where a wrong answer would silently attribute
qualification evidence to the wrong deployment.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import subprocess
import zipfile
from argparse import Namespace
from pathlib import Path

import pytest
from azure.identity import aio as azure_identity_aio
from eng.scripts import aca_deployed_qualification, aca_qualification_pipeline
from eng.scripts.aca_qualification_pipeline import (
    _DEPLOY_CONFIGURATION_TIMEOUT_SECONDS,
    _DEPLOY_TIMEOUT_SECONDS,
    _NOT_READY_STATUSES,
    QualificationPipelineError,
    _redacted_reason,
    _run_az,
    _write_deployment_archive,
    assemble_upload_directory,
    build_marker,
    compare_marker,
    content_report,
    deploy_preflight_failure_message,
    fetch_build_info,
    render_requirements,
    run_deploy,
    select_runtime_wheel,
    stamp_marker,
)

_BUILD_ID = "12345"
_COMMIT = "2415708287d1ce719e8380208d0ba52a8df9c080"


def _marker_payload(**overrides: object) -> dict[str, object]:
    build: dict[str, object] = {
        "marker": "present",
        "schema": 1,
        "build_id": _BUILD_ID,
        "commit_sha": _COMMIT,
        "branch": "refs/heads/main",
        "runtime_version": "1.2.3",
    }
    build.update(overrides)
    return {
        "build": build,
        "runtime": {"python_version": "3.13", "python_micro": 2},
        "content": {"entry_count": 5968, "total_bytes": 79_600_000, "truncated": False},
    }


class TestBuildMarker:
    def test_marker_carries_build_identity(self) -> None:
        marker = build_marker(
            commit_sha=_COMMIT,
            build_id=_BUILD_ID,
            branch="refs/heads/main",
            runtime_version="1.2.3",
        )
        assert marker["schema"] == 1
        assert marker["build_id"] == _BUILD_ID
        assert marker["commit_sha"] == _COMMIT

    @pytest.mark.parametrize("field", ["commit_sha", "build_id", "branch", "runtime_version"])
    def test_empty_field_is_rejected(self, field: str) -> None:
        kwargs = {
            "commit_sha": _COMMIT,
            "build_id": _BUILD_ID,
            "branch": "refs/heads/main",
            "runtime_version": "1.2.3",
        }
        kwargs[field] = "   "
        with pytest.raises(QualificationPipelineError, match=f"marker_field_empty:{field}"):
            build_marker(**kwargs)  # type: ignore[arg-type]

    def test_stamp_writes_readable_json(self, tmp_path: Path) -> None:
        marker = build_marker(
            commit_sha=_COMMIT,
            build_id=_BUILD_ID,
            branch="refs/heads/main",
            runtime_version="1.2.3",
        )
        target = stamp_marker(tmp_path, marker)
        assert json.loads(target.read_text(encoding="utf-8")) == marker

    def test_stamp_rejects_missing_app_root(self, tmp_path: Path) -> None:
        with pytest.raises(QualificationPipelineError, match="app_root_missing"):
            stamp_marker(tmp_path / "absent", {"schema": 1})


@pytest.mark.parametrize("status", [500, 501, 505, 599])
def test_build_attestation_retries_every_server_error(status: int) -> None:
    assert status in _NOT_READY_STATUSES


@pytest.mark.parametrize("status", [400, 401, 403, 404, 600])
def test_build_attestation_does_not_retry_definitive_responses(status: int) -> None:
    assert status not in _NOT_READY_STATUSES


class TestCompareMarker:
    def test_matching_build_passes(self) -> None:
        result = compare_marker(
            _marker_payload(),
            expected_build_id=_BUILD_ID,
            expected_commit_sha=_COMMIT,
            expected_python="3.13",
        )
        assert result.matches

    def test_stale_build_is_detected(self) -> None:
        result = compare_marker(
            _marker_payload(build_id="99999"),
            expected_build_id=_BUILD_ID,
            expected_commit_sha=_COMMIT,
            expected_python="3.13",
        )
        assert not result.matches
        assert "build_id" in result.mismatches

    def test_wrong_python_leg_is_detected(self) -> None:
        """The 3.14 package deployed onto the 3.13 app must not pass."""
        result = compare_marker(
            _marker_payload(),
            expected_build_id=_BUILD_ID,
            expected_commit_sha=_COMMIT,
            expected_python="3.14",
        )
        assert "python_version" in result.mismatches

    def test_absent_marker_never_passes(self) -> None:
        """An app with no marker must fail, not be treated as unverified-but-fine."""
        payload = _marker_payload()
        payload["build"] = {"marker": "absent"}
        result = compare_marker(
            payload,
            expected_build_id=_BUILD_ID,
            expected_commit_sha=_COMMIT,
            expected_python="3.13",
        )
        assert not result.matches
        assert result.mismatches == ("marker_absent",)

    def test_missing_build_section_never_passes(self) -> None:
        result = compare_marker(
            {"runtime": {"python_version": "3.13"}},
            expected_build_id=_BUILD_ID,
            expected_commit_sha=_COMMIT,
            expected_python="3.13",
        )
        assert result.mismatches == ("build_section_missing",)

    def test_mismatch_output_carries_no_values(self) -> None:
        """Field names only: this lands in a pipeline log."""
        result = compare_marker(
            _marker_payload(build_id="99999", commit_sha="deadbeef"),
            expected_build_id=_BUILD_ID,
            expected_commit_sha=_COMMIT,
            expected_python="3.13",
        )
        rendered = ",".join(result.mismatches)
        assert "99999" not in rendered
        assert "deadbeef" not in rendered


@pytest.mark.asyncio
async def test_build_info_retries_total_timeout_within_readiness_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _marker_payload()
    attempts = 0

    class FakeCredential:
        async def get_token(self, _: str) -> object:
            return type("Token", (), {"token": "credential-material"})()

        async def close(self) -> None:
            return None

    class FakeResponse:
        status = 200

        async def json(self, *, content_type: object) -> dict[str, object]:
            del content_type
            return payload

    class FakeRequest:
        async def __aenter__(self) -> FakeResponse:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TimeoutError
            return FakeResponse()

        async def __aexit__(self, *_: object) -> None:
            return None

    class FakeSession:
        def __init__(self, *, timeout: object) -> None:
            del timeout

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def get(self, *_: object, **__: object) -> FakeRequest:
            return FakeRequest()

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(azure_identity_aio, "DefaultAzureCredential", FakeCredential)
    monkeypatch.setattr("aiohttp.ClientSession", FakeSession)
    monkeypatch.setattr(aca_qualification_pipeline.asyncio, "sleep", no_sleep)

    assert await fetch_build_info("https://example.test", "api://scope") == payload
    assert attempts == 2


class TestContentReport:
    def test_reports_usage_against_caps(self) -> None:
        rendered = content_report(_marker_payload())
        assert "content_entries=5968" in rendered
        assert "75.9MiB" in rendered

    def test_missing_content_is_explicit(self) -> None:
        assert content_report({}) == "content=unavailable"


class TestQualificationEnvironmentContract:
    """Validate what the qualification tooling authors against the enforced bounds.

    The fixture and the deploy command must agree with the live-test support
    module, which validates its environment at fixture time. Nothing else
    compares the two, so a drift here would surface only as an
    ``AcaSmokeEnvironmentError`` during a paid deployed run.
    """

    def test_fixture_authors_the_required_region(self) -> None:
        root = Path(__file__).resolve().parents[1]
        source = (
            root
            / "tests"
            / "live"
            / "apps"
            / "aca-qualification"
            / "agents.config.yaml"
        ).read_text(encoding="utf-8")
        assert "region: $AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_REGION" in source

    def test_deploy_configures_the_region_app_setting(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "eng" / "scripts" / "aca_qualification_pipeline.py").read_text(
            encoding="utf-8"
        )
        assert "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_REGION={region}" in script

    def test_the_deployed_timeout_bound_is_the_platform_ceiling(self) -> None:
        """Fail if the support module's bound moves away from the platform limit.

        230 is not arbitrary: it is the Azure Functions platform HTTP request
        ceiling, so any larger authored timeout is unreachable regardless of
        what the client asks for.
        """
        source = (
            Path(__file__).resolve().parents[1]
            / "tests"
            / "live"
            / "aca_deployed_agent_support.py"
        ).read_text(encoding="utf-8")
        assert "1 <= timeout <= 230" in source, (
            "The enforced timeout bound changed; re-check the deployed timeout "
            "any qualification run passes."
        )


class TestCombinedDeployedSuite:
    def test_cold_start_is_the_first_module_in_the_single_suite(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: list[str] = []
        monkeypatch.setattr(
            aca_deployed_qualification,
            "validate_deployed_environment",
            lambda *args, **kwargs: (5, 1),
        )
        monkeypatch.setattr(aca_deployed_qualification, "preflight_auth", lambda _: None)
        monkeypatch.setattr(
            aca_deployed_qualification,
            "_run_pytest",
            lambda paths, _: captured.extend(paths) or 0,
        )

        result = aca_deployed_qualification.run_deployed_suite(
            {},
            runtime_target="python313",
            load_concurrency="5",
            provision_concurrency="1",
        )

        assert result == 0
        assert captured == [
            "tests/live/test_aca_deployed_cold_start.py",
            "tests/live/test_aca_deployed_agent_turn.py",
            "tests/live/test_aca_deployed_lifecycle.py",
            "tests/live/test_aca_deployed_loss.py",
            "tests/live/test_aca_deployed_load.py",
        ]

    def test_the_expected_identity_environment_is_required(self) -> None:
        """Every provenance input must be required before a deployed run starts."""
        for name in (
            "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EXPECTED_BUILD_ID",
            "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EXPECTED_COMMIT_SHA",
            "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_EXPECTED_PYTHON_VERSION",
        ):
            assert name in aca_deployed_qualification._DEPLOYED_ENVIRONMENT


class TestDeploymentCommand:
    def test_azure_cli_failure_diagnostics_are_omitted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def failed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            del args, kwargs
            return subprocess.CompletedProcess(
                args=["az"],
                returncode=1,
                stdout="",
                stderr="request failed with Bearer secret-token-value",
            )

        monkeypatch.setattr(subprocess, "run", failed)

        with pytest.raises(QualificationPipelineError) as caught:
            _run_az(["functionapp", "show"])

        assert "secret-token-value" not in str(caught.value)
        assert str(caught.value) == "az_failed:functionapp:show:exit_1"

    def test_azure_cli_timeout_is_a_typed_redacted_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def timed_out(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise subprocess.TimeoutExpired("az", 30)

        monkeypatch.setattr(subprocess, "run", timed_out)

        with pytest.raises(
            QualificationPipelineError,
            match=r"az_timeout:functionapp:show:30s",
        ):
            _run_az(["functionapp", "show"])

    def test_archive_contains_staged_files_relative_to_its_root(self, tmp_path: Path) -> None:
        staging = tmp_path / "staging"
        (staging / "nested").mkdir(parents=True)
        (staging / "host.json").write_text("{}", encoding="utf-8")
        (staging / "nested" / "file.txt").write_text("content", encoding="utf-8")
        archive = tmp_path / "fixture.zip"

        _write_deployment_archive(staging, archive)

        with zipfile.ZipFile(archive) as package:
            assert package.namelist() == ["host.json", "nested/file.txt"]

    def test_deploy_combines_preflight_region_upload_and_best_effort_tag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "host.json").write_text("{}", encoding="utf-8")
        archive = tmp_path / "fixture.zip"
        commands: list[tuple[list[str], float]] = []

        def fake_run_az(args: list[str], *, timeout_seconds: float = 30.0) -> None:
            commands.append((args, timeout_seconds))

        monkeypatch.setattr(
            "eng.scripts.aca_qualification_pipeline._run_az",
            fake_run_az,
        )

        result = run_deploy(
            Namespace(
                staging_root=str(staging),
                archive_path=str(archive),
                app_name="qualification-app",
                resource_group="qualification-rg",
                region="westus3",
                build_id="12345",
                commit_sha=_COMMIT,
            )
        )

        assert result == 0
        assert [command[:3] for command, _ in commands] == [
            ["functionapp", "show", "--name"],
            ["functionapp", "config", "appsettings"],
            ["functionapp", "deployment", "source"],
            ["tag", "update", "--operation"],
        ]
        assert [timeout for _, timeout in commands] == [
            30.0,
            _DEPLOY_CONFIGURATION_TIMEOUT_SECONDS,
            _DEPLOY_TIMEOUT_SECONDS,
            30.0,
        ]
        assert archive.is_file()

    def test_deploy_continues_when_redacted_metadata_tag_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "host.json").write_text("{}", encoding="utf-8")

        def fake_run_az(args: list[str], *, timeout_seconds: float = 30.0) -> None:
            del timeout_seconds
            if args[:2] == ["tag", "update"]:
                raise QualificationPipelineError(
                    "tag_failed: Bearer credential-material"
                )

        monkeypatch.setattr(
            "eng.scripts.aca_qualification_pipeline._run_az",
            fake_run_az,
        )

        result = run_deploy(
            Namespace(
                staging_root=str(staging),
                archive_path=str(tmp_path / "fixture.zip"),
                app_name="qualification-app",
                resource_group="qualification-rg",
                region="westus3",
                build_id="12345",
                commit_sha=_COMMIT,
            )
        )

        output = capsys.readouterr().out
        assert result == 0
        assert "warning: Function App metadata tag failed" in output
        assert "credential-material" not in output
        assert "<redacted>" in output


class TestFixtureRouteBinding:
    """Guard the fixture's HTTP types against the worker's binding validation.

    The route originally annotated ``azure.functions.HttpRequest``. The runtime
    registers every route with the FastAPI ``Request``/response types instead,
    and the worker rejects the mismatch at indexing time. Indexing is
    all-or-nothing, so that single bad function took down the whole deployed
    app -- every agent route included -- surfacing only as the generic
    "No job functions found".

    The fixture cannot be imported here (it requires a Linux host, and fails
    closed on Windows by design), so this reads the source instead.
    """

    def _build_info_signature(self) -> tuple[str, str]:
        source = (
            Path(__file__).resolve().parents[1]
            / "tests"
            / "live"
            / "apps"
            / "aca-qualification"
            / "function_app.py"
        )
        if not source.is_file():
            source = (
                Path(__file__).resolve().parent
                / "live"
                / "apps"
                / "aca-qualification"
                / "function_app.py"
            )
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "build_info":
                annotation = node.args.args[0].annotation
                returns = node.returns
                return (ast.unparse(annotation), ast.unparse(returns))
        raise AssertionError("build_info not found in the fixture app.")

    def test_uses_the_fastapi_request_type(self) -> None:
        parameter, _ = self._build_info_signature()
        assert parameter == "Request", (
            "The worker rejects azure.functions.HttpRequest here and refuses to "
            "index the entire app."
        )

    def test_returns_a_fastapi_response(self) -> None:
        _, returns = self._build_info_signature()
        assert "Response" in returns
        assert "HttpResponse" not in returns

    def test_registers_the_expected_binding_shape(self) -> None:
        """The corrected annotation must produce httpTrigger + http $return."""
        import azure.functions as func
        from azurefunctions.extensions.http.fastapi import JSONResponse, Request

        probe = func.FunctionApp()

        @probe.route(route="__buildinfo", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
        def build_info(req: Request) -> JSONResponse:  # pragma: no cover - shape probe
            return JSONResponse(content={}, status_code=200)

        bindings = {
            (binding.type, binding.name)
            for function in probe.get_functions()
            for binding in function.get_bindings()
        }
        assert ("httpTrigger", "req") in bindings
        assert ("http", "$return") in bindings


class TestDeployedAcaQualificationFixtureContract:
    """Keep the deployed fixture aligned with the live suites that invoke it."""

    _LIVE_SUITE_NAMES = (
        "test_aca_deployed_agent_turn.py",
        "test_aca_deployed_cold_start.py",
        "test_aca_deployed_lifecycle.py",
        "test_aca_deployed_load.py",
        "test_aca_deployed_loss.py",
    )
    _CALL_EXACTLY_ONCE = re.compile(r"\bCall\s+([a-z][a-z0-9_]*)\s+exactly once\b")

    def _repo_root(self) -> Path:
        return Path(__file__).resolve().parents[1]

    def _fixture_root(self) -> Path:
        return self._repo_root() / "tests" / "live" / "apps" / "aca-qualification"

    def _live_suite_paths(self) -> tuple[Path, ...]:
        live_root = self._repo_root() / "tests" / "live"
        return tuple(live_root / name for name in self._LIVE_SUITE_NAMES)

    def _suite_agent_slugs(self) -> dict[str, set[str]]:
        slugs: dict[str, set[str]] = {}
        for path in self._live_suite_paths():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
                    continue
                names = [target.id for target in node.targets if isinstance(target, ast.Name)]
                if any(name.endswith("_AGENT_SLUG") for name in names):
                    slugs.setdefault(path.name, set()).add(node.value.value)
        return slugs

    def _suite_tool_names(self) -> dict[str, set[str]]:
        tools: dict[str, set[str]] = {}
        for path in self._live_suite_paths():
            source = path.read_text(encoding="utf-8")
            names = set(self._CALL_EXACTLY_ONCE.findall(source))
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Compare):
                    continue
                compared = [node.left, *node.comparators]
                constants = {
                    item.value
                    for item in compared
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                }
                if not constants:
                    continue
                calls = [item for item in compared if isinstance(item, ast.Call)]
                if any(self._is_tool_name_get(call) for call in calls):
                    names.update(constants)
            if names:
                tools[path.name] = names
        return tools

    @staticmethod
    def _is_tool_name_get(call: ast.Call) -> bool:
        return (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "get"
            and bool(call.args)
            and isinstance(call.args[0], ast.Constant)
            and call.args[0].value == "tool_name"
        )

    def _fixture_agent_slugs(self) -> set[str]:
        return {
            path.name.removesuffix(".agent.md")
            for path in self._fixture_root().glob("*.agent.md")
            if path.is_file()
        }

    def _fixture_tool_names(self) -> set[str]:
        tool_names: set[str] = set()
        tools_root = self._fixture_root() / "tools"
        for path in tools_root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and not node.name.startswith(
                    "_"
                ):
                    tool_names.add(node.name)
        return tool_names

    def test_live_suite_agent_slugs_exist_in_the_deployed_fixture(self) -> None:
        required = {
            slug
            for slugs in self._suite_agent_slugs().values()
            for slug in slugs
            if slug.startswith("deployed_")
        }
        assert required, "No deployed agent slugs were derived from the live suites."

        missing = required - self._fixture_agent_slugs()
        assert not missing, f"Missing deployed fixture agent(s): {sorted(missing)}"

    def test_live_suite_tool_names_exist_in_the_deployed_fixture(self) -> None:
        required = {
            tool
            for tools in self._suite_tool_names().values()
            for tool in tools
            if tool.startswith("qualification_")
        }
        assert required, "No qualification tool names were derived from the live suites."

        missing = required - self._fixture_tool_names()
        assert not missing, f"Missing deployed fixture tool(s): {sorted(missing)}"


class TestRedactedReason:
    """A reason must be diagnosable without carrying credential material."""

    def test_keeps_the_operational_cause(self) -> None:
        reason = _redacted_reason(OSError("Cannot connect to host aca.example: [Errno -2]"))
        assert "Cannot connect to host" in reason

    def test_strips_bearer_tokens_and_sas_signatures(self) -> None:
        reason = _redacted_reason(
            RuntimeError("failed https://x/y?sv=2024-01-01&sig=AbCdEf123 Bearer eyJhbGciOiJIUzI1NiJ9abcdefghijk")
        )
        assert "AbCdEf123" not in reason
        assert "eyJhbGciOiJIUzI1NiJ9" not in reason
        assert "<redacted>" in reason

    def test_falls_back_to_type_when_empty(self) -> None:
        assert _redacted_reason(ValueError()) == "ValueError"

    def test_truncates_runaway_messages(self) -> None:
        assert len(_redacted_reason(RuntimeError("x" * 5000))) < 500


class TestAdapterRegionContract:
    """Bind the qualification assets to the real adapter signature.

    The deployed suites and the qualification fixture both depend on the
    authored Sandbox Group region being required, keyword-only, and free of ARM
    discovery. Copying the real signature here makes a future drift fail during
    the normal gate instead of in a paid deployed run.
    """

    def test_real_adapter_requires_an_authored_region(self) -> None:
        from azure_functions_agents.transport.aca_sdk import AcaSandboxAdapter

        parameters = inspect.signature(AcaSandboxAdapter.open).parameters
        region = parameters["region"]
        assert region.kind is inspect.Parameter.KEYWORD_ONLY
        assert region.default is inspect.Parameter.empty


class TestPackageAssemblyHelpers:
    def test_select_runtime_wheel_returns_the_only_wheel_name(self) -> None:
        selected = select_runtime_wheel(["azure_functions_agents-1.0.0-py3-none-any.whl"])
        assert selected == "azure_functions_agents-1.0.0-py3-none-any.whl"

    def test_select_runtime_wheel_rejects_no_candidates(self) -> None:
        with pytest.raises(QualificationPipelineError, match="runtime_wheel_missing"):
            select_runtime_wheel([])

    def test_select_runtime_wheel_rejects_ambiguous_candidates(self) -> None:
        with pytest.raises(QualificationPipelineError, match="runtime_wheel_ambiguous"):
            select_runtime_wheel(["a.whl", "b.whl"])

    def test_render_requirements_installs_actual_wheel_and_pinned_deps(self, tmp_path: Path) -> None:
        constraints = tmp_path / "aca-fixture-py313.txt"
        constraints.write_text("azure-functions==1.23.0\npydantic==2.11.0\n", encoding="utf-8")
        rendered = render_requirements(
            wheel_filename="azure_functions_agents-1.2.3-py3-none-any.whl",
            constraints_path=constraints,
        )
        assert rendered.splitlines() == [
            "./azure_functions_agents-1.2.3-py3-none-any.whl",
            "",
            "azure-functions==1.23.0",
            "pydantic==2.11.0",
        ]

    def test_render_requirements_uses_template_placeholder_once(self, tmp_path: Path) -> None:
        constraints = tmp_path / "aca-fixture-py314.txt"
        constraints.write_text("azure-functions==1.24.0\n", encoding="utf-8")
        template = tmp_path / "requirements.txt"
        template.write_text("{{RUNTIME_WHEEL}}\n", encoding="utf-8")
        rendered = render_requirements(
            wheel_filename="runtime-1.0.0-py3-none-any.whl",
            constraints_path=constraints,
            template_path=template,
        )
        assert rendered.splitlines().count("./runtime-1.0.0-py3-none-any.whl") == 1
        assert "azure-functions==1.24.0" in rendered

    def test_render_requirements_replaces_generated_template_placeholder(self, tmp_path: Path) -> None:
        constraints = tmp_path / "aca-fixture-py313.txt"
        constraints.write_text("azure-functions==1.23.0\n", encoding="utf-8")
        template = tmp_path / "requirements.txt"
        template.write_text(
            "# generated template\n"
            "./azurefunctions_agents_runtime-BUILD_WHEEL_PLACEHOLDER-py3-none-any.whl\n"
            "# pins follow\n"
            "stale-pin==0.0.1\n",
            encoding="utf-8",
        )
        rendered = render_requirements(
            wheel_filename="azurefunctions_agents_runtime-1.0.0-py3-none-any.whl",
            constraints_path=constraints,
            template_path=template,
        )
        assert "BUILD_WHEEL_PLACEHOLDER" not in rendered
        assert "./azurefunctions_agents_runtime-1.0.0-py3-none-any.whl" in rendered
        assert "stale-pin==0.0.1" not in rendered
        assert "azure-functions==1.23.0" in rendered

    def test_render_requirements_rejects_missing_constraints_file(self, tmp_path: Path) -> None:
        with pytest.raises(QualificationPipelineError, match="constraints_file_missing"):
            render_requirements(
                wheel_filename="runtime-1.0.0-py3-none-any.whl",
                constraints_path=tmp_path / "missing.txt",
            )

    def test_assemble_upload_materializes_fixture_wheel_marker_and_requirements(
        self,
        tmp_path: Path,
    ) -> None:
        artifact_root = tmp_path / "artifact"
        dist_root = artifact_root / "dist"
        dist_root.mkdir(parents=True)
        wheel = dist_root / "azurefunctions_agents_runtime-1.0.0-py3-none-any.whl"
        wheel.write_bytes(b"wheel")
        fixture_root = tmp_path / "fixture"
        fixture_root.mkdir()
        (fixture_root / "host.json").write_text("{}", encoding="utf-8")
        (fixture_root / "requirements.txt").write_text(
            "{{RUNTIME_WHEEL}}\n",
            encoding="utf-8",
        )
        ignored = fixture_root / "__pycache__"
        ignored.mkdir()
        (ignored / "fixture.pyc").write_bytes(b"ignored")
        constraints = tmp_path / "constraints.txt"
        constraints.write_text("azure-functions==1.24.0\n", encoding="utf-8")
        staging_root = tmp_path / "staging"

        result = assemble_upload_directory(
            Namespace(
                artifact_root=str(artifact_root),
                fixture_root=str(fixture_root),
                staging_root=str(staging_root),
                requirements_template=None,
                constraints_file=str(constraints),
                commit_sha=_COMMIT,
                build_id=_BUILD_ID,
                branch="refs/heads/main",
                runtime_version="1.0.0",
            )
        )

        assert result == staging_root
        assert (staging_root / wheel.name).read_bytes() == b"wheel"
        assert json.loads(
            (staging_root / "BUILD_INFO.json").read_text(encoding="utf-8")
        ) == build_marker(
            commit_sha=_COMMIT,
            build_id=_BUILD_ID,
            branch="refs/heads/main",
            runtime_version="1.0.0",
        )
        assert (staging_root / "requirements.txt").read_text(
            encoding="utf-8"
        ).splitlines() == [
            f"./{wheel.name}",
            "",
            "azure-functions==1.24.0",
        ]
        assert not (staging_root / "__pycache__").exists()


class TestDeployPreflightHelpers:
    def test_preflight_failure_names_actionable_role(self) -> None:
        rendered = deploy_preflight_failure_message(
            app_name="aca-app",
            resource_group="rg",
            check_name="publishing_config_read",
        )
        assert "functionapp_deploy_preflight_failed:publishing_config_read" in rendered
        assert "grant Website Contributor on aca-app" in rendered


class TestMonitorDependencyContract:
    """Prevent silent removal of the monitor extra from the ACA fixture."""

    def _repo_root(self) -> Path:
        return Path(__file__).resolve().parents[1]

    def test_fixture_requirements_input_requests_monitor_extra(self) -> None:
        source = (
            self._repo_root() / "eng" / "constraints" / "aca-fixture-requirements.in"
        ).read_text(encoding="utf-8")
        requirements = {
            line.strip()
            for line in source.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        assert requirements == {".[aca_sandbox,monitor]"}

    def test_compiled_constraints_contain_azure_monitor_opentelemetry(self) -> None:
        constraints_dir = self._repo_root() / "eng" / "constraints"
        for lock_name in ("aca-fixture-py313.txt", "aca-fixture-py314.txt"):
            content = (constraints_dir / lock_name).read_text(encoding="utf-8")
            assert "azure-monitor-opentelemetry==" in content, (
                f"{lock_name} must pin azure-monitor-opentelemetry"
            )

    @pytest.mark.parametrize("python_minor", ["313", "314"])
    def test_assembled_requirements_include_the_monitor_distribution(
        self, python_minor: str
    ) -> None:
        root = self._repo_root()
        rendered = render_requirements(
            wheel_filename="azurefunctions_agents_runtime-test.whl",
            constraints_path=(
                root / "eng" / "constraints" / f"aca-fixture-py{python_minor}.txt"
            ),
            template_path=(
                root / "tests" / "live" / "apps" / "aca-qualification" / "requirements.txt"
            ),
        )
        assert "./azurefunctions_agents_runtime-test.whl" in rendered
        assert "azure-monitor-opentelemetry==1.8.8" in rendered
