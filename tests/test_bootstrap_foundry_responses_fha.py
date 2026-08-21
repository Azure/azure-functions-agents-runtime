from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from azure_functions_agents.execution.foundry_responses_binding import FHA_BINDING_ENV_NAMES
from azure_functions_agents.foundry_responses.fha_runtime_projection import (
    FHA_RUNTIME_PROJECTION_FILENAME,
    compute_fha_wrapper_digest,
    serialize_fha_runtime_projection,
)

_RUNTIME_PRINCIPAL_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_INSTANCE_PRINCIPAL_ID = "11111111-aaaa-bbbb-cccc-222222222222"
_BLUEPRINT_PRINCIPAL_ID = "33333333-aaaa-bbbb-cccc-444444444444"
_APP_INSIGHTS_RESOURCE_ID = (
    "/subscriptions/11111111-2222-3333-4444-555555555555"
    "/resourceGroups/agents-rg/providers/Microsoft.Insights/components/agent-app-insights"
)


def _bootstrap_module():
    path = Path(__file__).parents[1] / "eng" / "scripts" / "bootstrap_foundry_responses_fha.py"
    spec = importlib.util.spec_from_file_location("bootstrap_foundry_responses_fha", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _system_identity_payload() -> str:
    return json.dumps({"principalId": _RUNTIME_PRINCIPAL_ID})


def _user_assigned_identity_payload() -> str:
    return json.dumps(
        {
            "type": "UserAssigned",
            "userAssignedIdentities": {
                "/subscriptions/example/resourceGroups/example/providers/"
                "Microsoft.ManagedIdentity/userAssignedIdentities/runtime": {
                    "clientId": "11111111-2222-3333-4444-555555555555",
                    "principalId": _RUNTIME_PRINCIPAL_ID,
                }
            },
        }
    )


def _hosted_agent_payload(
    plan,
    *,
    version: str = "7",
    status: str = "active",
    instance_principal_id: str | None = _INSTANCE_PRINCIPAL_ID,
    blueprint_principal_id: str | None = _BLUEPRINT_PRINCIPAL_ID,
) -> str:
    document: dict[str, object] = {
        "name": plan.managed_agent_name,
        "version": version,
        "status": status,
    }
    if instance_principal_id is not None:
        document["instance_identity"] = {"principal_id": instance_principal_id}
    if blueprint_principal_id is not None:
        document["blueprint"] = {"principal_id": blueprint_principal_id}
    return json.dumps(document)


def _app_insights_connection_payload(
    *,
    target: str = _APP_INSIGHTS_RESOURCE_ID,
    auth_type: str = "ProjectManagedIdentity",
) -> str:
    return json.dumps(
        {
            "value": [
                {
                    "id": (
                        "/subscriptions/11111111-2222-3333-4444-555555555555"
                        "/resourceGroups/agents-rg/providers/Microsoft.CognitiveServices"
                        "/accounts/project/projects/demo/connections/default-app-insights"
                    ),
                    "name": "default-app-insights",
                    "type": "Microsoft.CognitiveServices/accounts/projects/connections",
                    "properties": {
                        "authType": auth_type,
                        "category": "AppInsights",
                        "isDefault": True,
                        "isSharedToAll": False,
                        "target": target,
                    },
                }
            ]
        }
    )


def _observability_role_assignment_payload(module, command) -> str:
    principal_id = command[command.index("--assignee-object-id") + 1]
    scope = command[command.index("--scope") + 1]
    role_definition_id = (
        module._READER_ROLE_DEFINITION_ID
        if "/microsoft.cognitiveservices/accounts/" in scope.casefold()
        else module._MONITORING_METRICS_PUBLISHER_ROLE_DEFINITION_ID
    )
    return json.dumps(
        [
            {
                "principalId": principal_id,
                "scope": scope,
                "roleDefinitionId": (
                    "/providers/Microsoft.Authorization/roleDefinitions/"
                    f"{role_definition_id}"
                ),
            }
        ]
    )


def _arguments(module, tmp_path: Path):
    application = tmp_path / "application"
    application.mkdir()
    (application / "main.agent.md").write_text(
        """---
name: Model only
description: Test agent.
trigger:
  type: http_trigger
  args:
    route: model
tools: false
mcp: false
skills: false
system_tools:
  web_request: false
---
Answer the request.
""",
        encoding="utf-8",
    )
    return module.BootstrapArguments(
        application_root=application,
        stage_root=tmp_path / "staged",
        subscription_id="11111111-2222-3333-4444-555555555555",
        function_app_name="agent-app",
        function_app_slot=None,
        resource_group="agents-rg",
        setup_principal_id="bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
        project_endpoint="https://project.services.ai.azure.com/api/projects/demo",
        project_resource_id=(
            "/subscriptions/11111111-2222-3333-4444-555555555555"
            "/resourceGroups/agents-rg/providers/Microsoft.CognitiveServices/accounts/project/projects/demo"
        ),
        model_deployment_name="gpt-model",
        runtime_pin="azurefunctions-agents-runtime==0.1.0",
        agentserver_core_pin="azure-ai-agentserver-core==2.1.0b1",
        agentserver_responses_pin="azure-ai-agentserver-responses==2.1.0b1",
    )


class _BootstrapRunner:
    def __init__(
        self,
        module,
        plan,
        *,
        agent_payloads: list[str] | None = None,
        app_insights_target: str = _APP_INSIGHTS_RESOURCE_ID,
        app_insights_auth_type: str = "ProjectManagedIdentity",
        preexisting_assignments: bool = False,
        deny_role_assignment_writes: bool = False,
        drop_created_assignments: bool = False,
    ) -> None:
        self.module = module
        self.plan = plan
        self.calls: list[tuple[str, ...]] = []
        self._agent_payloads = agent_payloads
        self._agent_payload_index = 0
        self._app_insights_target = app_insights_target
        self._app_insights_auth_type = app_insights_auth_type
        self._deny_role_assignment_writes = deny_role_assignment_writes
        self._drop_created_assignments = drop_created_assignments
        self._assignment_keys: set[tuple[str, str, str]] = set()
        if preexisting_assignments:
            self._assignment_keys.update(self._required_assignment_keys())

    def set_plan(self, plan) -> None:
        self.plan = plan

    def run(self, command, *, cwd=None):
        command = tuple(command)
        self.calls.append(command)
        if command[0:3] == ("az", "account", "show"):
            return self._result('{"user":{"name":"signed-in@example.com","type":"user"}}')
        if command[0:4] == ("az", "functionapp", "identity", "show"):
            return self._result(_system_identity_payload())
        if command[0:3] == ("az", "functionapp", "show"):
            return self._result(
                json.dumps(
                    {
                        "tags": {
                            "hidden-link:/app-insights-resource-id": _APP_INSIGHTS_RESOURCE_ID,
                        }
                    }
                )
            )
        if command[0:4] == ("az", "role", "assignment", "list"):
            if "--assignee-object-id" in command:
                return self._result(self._matching_assignments(command))
            role = (
                "Foundry Agent Consumer"
                if command[command.index("--assignee") + 1] == _RUNTIME_PRINCIPAL_ID
                else "Foundry Project Manager"
            )
            return self._result(f'[{{"roleDefinitionName":"{role}"}}]')
        if command[0:4] == ("az", "role", "assignment", "create"):
            if self._deny_role_assignment_writes:
                return self._result(
                    "",
                    returncode=1,
                    stderr=(
                        "AuthorizationFailed: does not have authorization to perform action "
                        "Microsoft.Authorization/roleAssignments/write"
                    ),
                )
            assignment_key = (
                command[command.index("--assignee-object-id") + 1].casefold(),
                command[command.index("--scope") + 1].rstrip("/").casefold(),
                command[command.index("--role") + 1].casefold(),
            )
            if not self._drop_created_assignments:
                self._assignment_keys.add(assignment_key)
            return self._result("{}")
        if command[0:3] == ("az", "rest", "--method"):
            url = command[command.index("--url") + 1]
            if (
                "/connections?category=AppInsights&api-version=2025-09-01"
                "&includeAll=false"
            ) in url:
                return self._result(
                    _app_insights_connection_payload(
                        target=self._app_insights_target,
                        auth_type=self._app_insights_auth_type,
                    )
                )
            return self._result("", returncode=1, stderr="404 Not Found")
        if command[0:4] == ("azd", "ai", "agent", "init"):
            assert cwd is not None
            Path(cwd, "azure.yaml").write_text(
                "services:\n  hosted:\n    host: azure.ai.agent\n",
                encoding="utf-8",
            )
            return self._result("{}")
        if command[0:4] == ("azd", "ai", "agent", "show"):
            if self._agent_payloads is None:
                return self._result(_hosted_agent_payload(self.plan))
            index = min(self._agent_payload_index, len(self._agent_payloads) - 1)
            self._agent_payload_index += 1
            return self._result(self._agent_payloads[index])
        if command[0:4] == ("azd", "ai", "agent", "invoke"):
            return self._result("READY")
        if command[0:3] == ("azd", "deploy", "--all"):
            return self._result("{}")
        return self._result("{}")

    def _result(self, stdout: str, *, returncode: int = 0, stderr: str = ""):
        return self.module.CommandResult(returncode=returncode, stdout=stdout, stderr=stderr)

    def _required_assignment_keys(self) -> set[tuple[str, str, str]]:
        deployment = self.module.HostedAgentDeployment(
            agent_name=self.plan.managed_agent_name,
            agent_version="7",
            instance_principal_id=_INSTANCE_PRINCIPAL_ID,
            blueprint_principal_id=_BLUEPRINT_PRINCIPAL_ID,
        )
        return {
            (
                assignment.principal_id.casefold(),
                assignment.scope.rstrip("/").casefold(),
                assignment.role_definition_id.casefold(),
            )
            for assignment in self.module._build_observability_role_assignments(
                self.plan,
                deployment=deployment,
                app_insights_resource_id=_APP_INSIGHTS_RESOURCE_ID,
            )
        }

    def _matching_assignments(self, command: tuple[str, ...]) -> str:
        principal_id = command[command.index("--assignee-object-id") + 1]
        scope = command[command.index("--scope") + 1]
        matching = [
            {
                "principalId": principal_id,
                "scope": scope,
                "roleDefinitionId": (
                    "/providers/Microsoft.Authorization/roleDefinitions/"
                    f"{role_definition_id}"
                ),
            }
            for candidate_principal_id, candidate_scope, role_definition_id in sorted(
                self._assignment_keys
            )
            if candidate_principal_id == principal_id.casefold()
            and candidate_scope == scope.rstrip("/").casefold()
        ]
        return json.dumps(matching)


def test_subprocess_runner_resolves_azure_cli_batch_shim_to_native_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _bootstrap_module()
    shim = tmp_path / "wbin" / "az.cmd"
    shim.parent.mkdir()
    shim.touch()
    azure_cli_python = shim.parent.parent / "python.exe"
    azure_cli_python.touch()
    captured: dict[str, object] = {}
    setting = 'FHA_MANIFEST={"entries":[{"path":"literal & value"}]}'

    def run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return module.subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(module.shutil, "which", lambda executable: str(shim))
    monkeypatch.setattr(module, "_is_windows_batch_wrapper", lambda path: True)
    monkeypatch.setattr(module.subprocess, "run", run)

    result = module.SubprocessCommandRunner().run(
        ("az", "functionapp", "config", "appsettings", "set", "--settings", setting),
        cwd=tmp_path,
    )

    assert result == module.CommandResult(returncode=0, stdout="ok", stderr="")
    assert captured["command"] == [
        str(azure_cli_python.resolve()),
        "-IBm",
        "azure.cli",
        "functionapp",
        "config",
        "appsettings",
        "set",
        "--settings",
        setting,
    ]
    kwargs = captured["kwargs"]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["shell"] is False
    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"


def test_subprocess_runner_resolves_native_executable_and_preserves_azd_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _bootstrap_module()
    azd = tmp_path / "azd.exe"
    azd.touch()
    captured: dict[str, object] = {}

    def run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return module.subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(module.shutil, "which", lambda executable: str(azd))
    monkeypatch.setattr(module.subprocess, "run", run)

    module.SubprocessCommandRunner().run(("azd", "ai", "agent", "--help"), cwd=tmp_path)

    assert captured["command"] == [str(azd.resolve()), "ai", "agent", "--help"]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["env"]["AZURE_DEV_USER_AGENT"] == "microsoft_foundry_skill"


def test_subprocess_runner_reports_missing_required_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _bootstrap_module()
    monkeypatch.setattr(module.shutil, "which", lambda executable: None)

    with pytest.raises(
        module.BootstrapCommandError,
        match="unavailable",
    ):
        module.SubprocessCommandRunner().run(("az", "account", "show"))


def test_main_reports_missing_executable_without_a_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _bootstrap_module()
    arguments = _arguments(module, tmp_path)
    plan = module.build_bootstrap_plan(arguments)
    monkeypatch.setattr(module, "build_bootstrap_plan", lambda _: plan)
    monkeypatch.setattr(module.shutil, "which", lambda executable: None)

    exit_code = module.main(
        [
            "--application-root",
            str(arguments.application_root),
            "--stage-root",
            str(arguments.stage_root),
            "--subscription-id",
            arguments.subscription_id,
            "--function-app-name",
            arguments.function_app_name,
            "--resource-group",
            arguments.resource_group,
            "--project-endpoint",
            arguments.project_endpoint,
            "--project-resource-id",
            arguments.project_resource_id,
            "--model-deployment-name",
            arguments.model_deployment_name,
            "--execute",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Bootstrap failed: Required bootstrap executable 'az' is unavailable." in captured.err
    assert "Traceback" not in captured.err


def test_subprocess_runner_rejects_unapproved_executable() -> None:
    module = _bootstrap_module()

    with pytest.raises(module.BootstrapCommandError, match="not permitted"):
        module.SubprocessCommandRunner().run(("python", "--version"))


def test_azd_failure_diagnostic_exposes_redacted_error(
    tmp_path: Path,
) -> None:
    module = _bootstrap_module()

    class Runner:
        def run(self, command, *, cwd=None):
            del command, cwd
            return module.CommandResult(
                returncode=1,
                stdout="Error: model deployment gpt-5.4-nano was not found.",
                stderr="ERROR: client_secret=should-not-appear",
            )

    with pytest.raises(module.BootstrapCommandError) as error:
        module._run_required(
            Runner(),
            ("azd", "ai", "agent", "init"),
            cwd=tmp_path,
            operation="agent init",
        )

    message = str(error.value)
    assert "model deployment gpt-5.4-nano was not found" in message
    assert "<redacted>" in message
    assert "should-not-appear" not in message


def test_function_principal_resolution_uses_single_user_assigned_identity() -> None:
    module = _bootstrap_module()

    assert (
        module._resolve_function_principal_id(_user_assigned_identity_payload())
        == _RUNTIME_PRINCIPAL_ID
    )


def test_function_principal_resolution_rejects_ambiguous_user_assigned_identities() -> None:
    module = _bootstrap_module()
    payload = json.dumps(
        {
            "userAssignedIdentities": {
                "first": {"principalId": _RUNTIME_PRINCIPAL_ID},
                "second": {"principalId": "ffffffff-eeee-dddd-cccc-bbbbbbbbbbbb"},
            }
        }
    )

    with pytest.raises(module.BootstrapCommandError, match="managed identity principal"):
        module._resolve_function_principal_id(payload)


def test_bootstrap_plan_stages_source_and_generates_all_binding_settings(tmp_path: Path) -> None:
    module = _bootstrap_module()

    plan = module.build_bootstrap_plan(_arguments(module, tmp_path))

    assert plan.application_content_digest.startswith("sha256:")
    assert plan.wrapper_digest.startswith("sha256:")
    assert plan.artifact.entrypoint_path.exists()
    assert plan.preflight_commands[1][-4:] == (
        "--subscription",
        plan.arguments.subscription_id,
        "--output",
        "json",
    )
    assert plan.artifact.projection_path.read_bytes() == serialize_fha_runtime_projection(
        plan.projection
    ).encode("utf-8")
    assert plan.manifest.runtime_projection == serialize_fha_runtime_projection(
        plan.projection
    )
    assert plan.wrapper_digest == compute_fha_wrapper_digest(
        plan.projection,
        plan.artifact.rendered_entrypoint,
    )
    assert FHA_RUNTIME_PROJECTION_FILENAME not in {entry.path for entry in plan.manifest.entries}
    assert (plan.artifact.stage_root / "azure_functions_agents" / "__init__.py").exists()
    assert plan.workspace_root.parent == tmp_path / "staged"
    assert plan.managed_agent_name.startswith("afa-v2-a1-")


def test_bootstrap_plan_compiles_raw_source_once_with_argument_owned_projection_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _bootstrap_module()
    arguments = _arguments(module, tmp_path)
    calls: list[tuple[Path, str, str]] = []
    compile_project = module.compile_fha_v0_project
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://untrusted.example.test/project")
    monkeypatch.setenv("FOUNDRY_MODEL", "untrusted-model")

    def compiled_once(
        application_root: Path,
        *,
        project_endpoint: str,
        default_model: str,
        expected_projection=None,
    ):
        calls.append((application_root, project_endpoint, default_model))
        return compile_project(
            application_root,
            project_endpoint=project_endpoint,
            default_model=default_model,
            expected_projection=expected_projection,
        )

    monkeypatch.setattr(module, "compile_fha_v0_project", compiled_once)

    plan = module.build_bootstrap_plan(arguments)

    assert calls == [
        (
            arguments.application_root.resolve(),
            arguments.project_endpoint,
            arguments.model_deployment_name,
        )
    ]
    assert plan.projection.project_endpoint == "https://project.services.ai.azure.com/api/projects/demo"
    assert plan.projection.default_model == "gpt-model"


def test_bootstrap_rejects_unsafe_projection_inputs_before_staging(tmp_path: Path) -> None:
    module = _bootstrap_module()
    arguments = _arguments(module, tmp_path)
    unsafe_arguments = replace(arguments, model_deployment_name="$MODEL_DEPLOYMENT")

    with pytest.raises(module.BootstrapCommandError, match="compilation"):
        module.build_bootstrap_plan(unsafe_arguments)

    assert not unsafe_arguments.stage_root.exists()


def test_bootstrap_rejects_raw_authoring_placeholders_before_staging(tmp_path: Path) -> None:
    module = _bootstrap_module()
    arguments = _arguments(module, tmp_path)
    (arguments.application_root / "main.agent.md").write_text(
        (arguments.application_root / "main.agent.md").read_text(encoding="utf-8")
        + "\nUse $UNSAFE_VALUE.\n",
        encoding="utf-8",
    )

    with pytest.raises(module.BootstrapCommandError, match="compilation"):
        module.build_bootstrap_plan(arguments)

    assert not arguments.stage_root.exists()


def test_bootstrap_rejects_secret_inputs_before_staging(tmp_path: Path) -> None:
    module = _bootstrap_module()
    arguments = _arguments(module, tmp_path)
    (arguments.application_root / ".env").write_text("API_KEY=secret", encoding="utf-8")

    with pytest.raises(module.BootstrapCommandError, match="content"):
        module.build_bootstrap_plan(arguments)

    assert not arguments.stage_root.exists()


def test_execute_bootstrap_deploys_smokes_and_publishes_binding(tmp_path: Path) -> None:
    module = _bootstrap_module()
    arguments = replace(_arguments(module, tmp_path), setup_principal_id=None)
    plan = module.build_bootstrap_plan(arguments)
    calls: list[tuple[str, ...]] = []

    class Runner:
        def run(self, command, *, cwd=None):
            calls.append(tuple(command))
            if command[0:3] == ("az", "account", "show"):
                stdout = '{"user":{"name":"signed-in@example.com","type":"user"}}'
            elif command[0:4] == ("azd", "ai", "agent", "show"):
                stdout = _hosted_agent_payload(plan)
            elif command[0:4] == ("az", "functionapp", "identity", "show"):
                stdout = _user_assigned_identity_payload()
            elif command[0:3] == ("az", "functionapp", "show"):
                stdout = json.dumps(
                    {
                        "tags": {
                            "hidden-link:/app-insights-resource-id": _APP_INSIGHTS_RESOURCE_ID,
                        }
                    }
                )
            elif command[0:4] == ("az", "role", "assignment", "list"):
                if "--assignee-object-id" in command:
                    stdout = _observability_role_assignment_payload(module, command)
                else:
                    role = (
                        "Foundry Agent Consumer"
                        if command[5] == _RUNTIME_PRINCIPAL_ID
                        else "Foundry Project Manager"
                    )
                    stdout = f'[{{"roleDefinitionName":"{role}"}}]'
            elif command[0:3] == ("az", "rest", "--method"):
                url = command[command.index("--url") + 1]
                if (
                    "/connections?category=AppInsights&api-version=2025-09-01"
                    "&includeAll=false"
                ) in url:
                    stdout = _app_insights_connection_payload()
                else:
                    return module.CommandResult(
                        returncode=1,
                        stdout="",
                        stderr="404 Not Found",
                    )
            elif command[0:4] == ("azd", "ai", "agent", "init"):
                assert cwd is not None
                assert "--src" not in command
                assert Path(cwd, "fha_hosted_responses_entrypoint.py").exists()
                Path(cwd, "azure.yaml").write_text(
                    "services:\n"
                    "  hosted:\n"
                    "    host: azure.ai.agent\n",
                    encoding="utf-8",
                )
                stdout = "{}"
            elif command[0:4] == ("azd", "ai", "agent", "invoke"):
                stdout = "READY"
            else:
                stdout = "{}"
            return module.CommandResult(
                returncode=0,
                stdout=stdout,
                stderr="",
            )

    result = module.execute_bootstrap(plan, Runner())

    assert calls[: len(plan.preflight_commands)] == list(plan.preflight_commands)
    assert any(
        command[0:6]
        == (
            "az",
            "role",
            "assignment",
            "list",
            "--assignee",
            "signed-in@example.com",
        )
        for command in calls
    )
    assert ("azd", "deploy", "--all", "--no-prompt") in calls
    assert all(
        "--all" not in command
        for command in calls
        if command[0:4] == ("az", "role", "assignment", "list")
        and "--assignee-object-id" in command
    )
    assert any(command[0:4] == ("azd", "ai", "agent", "invoke") for command in calls)
    assert any(command[0:4] == ("az", "functionapp", "config", "appsettings") for command in calls)
    assert calls[-1][0:3] == ("az", "functionapp", "restart")
    assert result.managed_agent_version == "7"
    assert result.managed_agent_name == plan.managed_agent_name
    assert len(result.app_settings) == 8
    assert set(result.app_settings) == set(FHA_BINDING_ENV_NAMES)
    assert result.app_settings["AZURE_FUNCTIONS_AGENTS_FHA_MANAGED_AGENT_VERSION"] == "7"
    document = yaml.safe_load((plan.workspace_root / "azure.yaml").read_text(encoding="utf-8"))
    service = document["services"]["hosted"]
    hosted_variables = {
        item["name"]: item["value"] for item in service["environmentVariables"]
    }
    assert service["metadata"] == {
        module._PROVENANCE_METADATA_KEY: plan.hosted_agent_spec["provenance_tag"]
    }
    assert hosted_variables == {
        "FOUNDRY_PROJECT_ENDPOINT": plan.projection.project_endpoint,
        "FOUNDRY_MODEL": plan.projection.default_model,
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": plan.projection.default_model,
        "APPLICATIONINSIGHTS_AUTH_MODE": "entra",
        "AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING": "true",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "false",
        "OTEL_TRACES_SAMPLER": "always_on",
    }


def test_stage_azd_workspace_copies_hosted_source_into_empty_workspace(
    tmp_path: Path,
) -> None:
    module = _bootstrap_module()
    source = tmp_path / "hosted-source"
    source.mkdir()
    (source / "fha_hosted_responses_entrypoint.py").write_text(
        "print('ready')\n",
        encoding="utf-8",
    )
    (source / "nested").mkdir()
    (source / "nested" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    workspace = tmp_path / "azd-workspace"
    workspace.mkdir()

    module._stage_azd_workspace(source, workspace)

    assert (workspace / "fha_hosted_responses_entrypoint.py").read_text(
        encoding="utf-8"
    ) == "print('ready')\n"
    assert (workspace / "nested" / "module.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_trace_content_capture_requires_explicit_opt_in(tmp_path: Path) -> None:
    module = _bootstrap_module()
    plan = module.build_bootstrap_plan(_arguments(module, tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "azure.yaml").write_text(
        "services:\n  hosted:\n    host: azure.ai.agent\n",
        encoding="utf-8",
    )

    module._stamp_azd_provenance(
        workspace,
        plan.hosted_agent_spec["provenance_tag"],
        projection=plan.projection,
        capture_trace_content=True,
    )

    document = yaml.safe_load((workspace / "azure.yaml").read_text(encoding="utf-8"))
    variables = {
        item["name"]: item["value"]
        for item in document["services"]["hosted"]["environmentVariables"]
    }
    assert variables["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "true"


def test_stage_azd_workspace_rejects_nonempty_destination(tmp_path: Path) -> None:
    module = _bootstrap_module()
    source = tmp_path / "hosted-source"
    source.mkdir()
    (source / "entrypoint.py").write_text("pass\n", encoding="utf-8")
    workspace = tmp_path / "azd-workspace"
    workspace.mkdir()
    (workspace / "existing.txt").write_text("must not overwrite\n", encoding="utf-8")

    with pytest.raises(module.BootstrapCommandError, match="not empty"):
        module._stage_azd_workspace(source, workspace)

    assert (workspace / "existing.txt").read_text(encoding="utf-8") == "must not overwrite\n"


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("READY\n", True),
        ("Agent: deployed\n[managed-agent] READY\n", True),
        ("[other-agent] READY\n", False),
        ("readiness=READY\n", False),
    ],
)
def test_smoke_output_accepts_azd_agent_prefix_only_for_expected_agent(
    output: str,
    expected: bool,
) -> None:
    module = _bootstrap_module()

    assert module._smoke_output_is_ready(output, agent_name="managed-agent") is expected


def test_bootstrap_defaults_dependency_pins_and_stage_root(tmp_path: Path) -> None:
    module = _bootstrap_module()
    arguments = _arguments(module, tmp_path)
    (arguments.application_root / "requirements.txt").write_text(
        "./wheels/azurefunctions_agents_runtime-0.1.0b11-py3-none-any.whl[monitor]\n",
        encoding="utf-8",
    )
    plan = module.build_bootstrap_plan(
        replace(
            arguments,
            runtime_pin=None,
            agentserver_core_pin=None,
            agentserver_responses_pin=None,
        )
    )

    assert plan.artifact.requirements_path.read_text(encoding="utf-8").startswith(
        "azurefunctions-agents-runtime==0.1.0b11\n"
        "azure-ai-agentserver-core==2.1.0b1\n"
        "azure-ai-agentserver-responses==2.1.0b1\n"
    )

    namespace = module._parser().parse_args(
        [
            "--application-root",
            str(arguments.application_root),
            "--subscription-id",
            arguments.subscription_id,
            "--function-app-name",
            arguments.function_app_name,
            "--resource-group",
            arguments.resource_group,
            "--project-endpoint",
            arguments.project_endpoint,
            "--project-resource-id",
            arguments.project_resource_id,
            "--model-deployment-name",
            arguments.model_deployment_name,
        ]
    )
    defaults = module._arguments_from_namespace(namespace)
    assert defaults.stage_root == module._DEFAULT_STAGE_ROOT
    assert defaults.setup_principal_id is None
    assert defaults.runtime_pin is None
    assert defaults.agentserver_core_pin is None
    assert defaults.agentserver_responses_pin is None
    assert defaults.app_insights_resource_id is None
    assert defaults.rbac_mode == "auto"


def test_execute_bootstrap_fails_before_mutation_without_required_roles(tmp_path: Path) -> None:
    module = _bootstrap_module()
    plan = module.build_bootstrap_plan(_arguments(module, tmp_path))
    calls: list[tuple[str, ...]] = []

    class Runner:
        def run(self, command, *, cwd=None):
            calls.append(tuple(command))
            if command[0:4] == ("az", "functionapp", "identity", "show"):
                stdout = _system_identity_payload()
            else:
                stdout = "[]"
            return module.CommandResult(returncode=0, stdout=stdout, stderr="")

    with pytest.raises(module.BootstrapCommandError, match="role assignment"):
        module.execute_bootstrap(plan, Runner())

    assert all(command[0:2] != ("azd", "deploy") for command in calls)


def test_build_bootstrap_plan_is_repeatable_with_fresh_local_workspaces(tmp_path: Path) -> None:
    module = _bootstrap_module()
    arguments = _arguments(module, tmp_path)

    first = module.build_bootstrap_plan(arguments)
    second = module.build_bootstrap_plan(arguments)

    assert first.application_content_digest == second.application_content_digest
    assert first.wrapper_digest == second.wrapper_digest
    assert first.managed_agent_name == second.managed_agent_name
    assert first.artifact.stage_root != second.artifact.stage_root
    assert first.workspace_root != second.workspace_root


def test_bootstrap_rejects_same_name_agent_without_matching_provenance(tmp_path: Path) -> None:
    module = _bootstrap_module()
    plan = module.build_bootstrap_plan(_arguments(module, tmp_path))

    class Runner:
        def run(self, command, *, cwd=None):
            del cwd
            if command[0:4] == ("az", "functionapp", "identity", "show"):
                stdout = _system_identity_payload()
            elif command[0:4] == ("az", "role", "assignment", "list"):
                role = (
                    "Foundry Agent Consumer"
                    if command[5] == _RUNTIME_PRINCIPAL_ID
                    else "Foundry Project Manager"
                )
                stdout = f'[{{"roleDefinitionName":"{role}"}}]'
            elif command[0:3] == ("az", "rest", "--method"):
                stdout = '{"versions":{"latest":{"metadata":{"unrelated":"value"}}}}'
            else:
                stdout = "{}"
            return module.CommandResult(returncode=0, stdout=stdout, stderr="")

    with pytest.raises(module.BootstrapCommandError, match="not owned"):
        module.execute_bootstrap(plan, Runner())


def test_existing_agent_provenance_accepts_active_version_metadata(tmp_path: Path) -> None:
    module = _bootstrap_module()
    provenance_tag = "afa-provenance:afa-v2-a1-example"

    class Runner:
        def run(self, command, *, cwd=None):
            del command, cwd
            return module.CommandResult(
                returncode=0,
                stdout=json.dumps(
                    {
                        "versions": {
                            "latest": {
                                "metadata": {
                                    module._PROVENANCE_METADATA_KEY: provenance_tag,
                                }
                            }
                        }
                    }
                ),
                stderr="",
            )

    module._validate_existing_agent_provenance(
        Runner(),
        project_endpoint="https://project.services.ai.azure.com/api/projects/demo",
        agent_name="afa-v2-a1-example",
        provenance_tag=provenance_tag,
        subscription_id="11111111-2222-3333-4444-555555555555",
        cwd=tmp_path,
    )


@pytest.mark.parametrize(
    "failure_prefix",
    [
        ("azd", "deploy"),
        ("azd", "ai", "agent", "show"),
        ("azd", "ai", "agent", "invoke"),
    ],
)
def test_bootstrap_never_publishes_binding_after_deploy_or_smoke_failure(
    tmp_path: Path,
    failure_prefix: tuple[str, ...],
) -> None:
    module = _bootstrap_module()
    plan = module.build_bootstrap_plan(_arguments(module, tmp_path))
    calls: list[tuple[str, ...]] = []

    class Runner:
        def run(self, command, *, cwd=None):
            command = tuple(command)
            calls.append(command)
            if command[: len(failure_prefix)] == failure_prefix:
                return module.CommandResult(returncode=1, stdout="", stderr="failed")
            if command[0:4] == ("az", "functionapp", "identity", "show"):
                stdout = _system_identity_payload()
            elif command[0:3] == ("az", "functionapp", "show"):
                stdout = json.dumps(
                    {
                        "tags": {
                            "hidden-link:/app-insights-resource-id": _APP_INSIGHTS_RESOURCE_ID,
                        }
                    }
                )
            elif command[0:4] == ("az", "role", "assignment", "list"):
                if "--assignee-object-id" in command:
                    stdout = _observability_role_assignment_payload(module, command)
                else:
                    role = (
                        "Foundry Agent Consumer"
                        if command[5] == _RUNTIME_PRINCIPAL_ID
                        else "Foundry Project Manager"
                    )
                    stdout = f'[{{"roleDefinitionName":"{role}"}}]'
            elif command[0:3] == ("az", "rest", "--method"):
                url = command[command.index("--url") + 1]
                if (
                    "/connections?category=AppInsights&api-version=2025-09-01"
                    "&includeAll=false"
                ) in url:
                    stdout = _app_insights_connection_payload()
                else:
                    return module.CommandResult(
                        returncode=1,
                        stdout="",
                        stderr="404 Not Found",
                    )
            elif command[0:4] == ("azd", "ai", "agent", "init"):
                assert cwd is not None
                Path(cwd, "azure.yaml").write_text(
                    "services:\n  hosted:\n    host: azure.ai.agent\n",
                    encoding="utf-8",
                )
                stdout = "{}"
            elif command[0:4] == ("azd", "ai", "agent", "show"):
                stdout = _hosted_agent_payload(plan)
            elif command[0:4] == ("azd", "ai", "agent", "invoke"):
                stdout = "READY"
            else:
                stdout = "{}"
            return module.CommandResult(returncode=0, stdout=stdout, stderr="")

    with pytest.raises(module.BootstrapCommandError):
        module.execute_bootstrap(plan, Runner())

    assert not any(
        command[0:4] == ("az", "functionapp", "config", "appsettings")
        for command in calls
    )


@pytest.mark.parametrize(
    "smoke_output",
    [
        "NOT READY",
        "diagnostic: READY marker was not produced",
        "readiness=READY",
    ],
)
def test_bootstrap_rejects_zero_exit_false_positive_smoke_output(
    tmp_path: Path,
    smoke_output: str,
) -> None:
    module = _bootstrap_module()
    plan = module.build_bootstrap_plan(_arguments(module, tmp_path))
    calls: list[tuple[str, ...]] = []

    class Runner:
        def run(self, command, *, cwd=None):
            command = tuple(command)
            calls.append(command)
            if command[0:4] == ("az", "functionapp", "identity", "show"):
                stdout = _system_identity_payload()
            elif command[0:3] == ("az", "functionapp", "show"):
                stdout = json.dumps(
                    {
                        "tags": {
                            "hidden-link:/app-insights-resource-id": _APP_INSIGHTS_RESOURCE_ID,
                        }
                    }
                )
            elif command[0:4] == ("az", "role", "assignment", "list"):
                if "--assignee-object-id" in command:
                    stdout = _observability_role_assignment_payload(module, command)
                else:
                    role = (
                        "Foundry Agent Consumer"
                        if command[5] == _RUNTIME_PRINCIPAL_ID
                        else "Foundry Project Manager"
                    )
                    stdout = f'[{{"roleDefinitionName":"{role}"}}]'
            elif command[0:3] == ("az", "rest", "--method"):
                url = command[command.index("--url") + 1]
                if (
                    "/connections?category=AppInsights&api-version=2025-09-01"
                    "&includeAll=false"
                ) in url:
                    stdout = _app_insights_connection_payload()
                else:
                    return module.CommandResult(
                        returncode=1,
                        stdout="",
                        stderr="404 Not Found",
                    )
            elif command[0:4] == ("azd", "ai", "agent", "init"):
                assert cwd is not None
                Path(cwd, "azure.yaml").write_text(
                    "services:\n  hosted:\n    host: azure.ai.agent\n",
                    encoding="utf-8",
                )
                stdout = "{}"
            elif command[0:4] == ("azd", "ai", "agent", "show"):
                stdout = _hosted_agent_payload(plan)
            elif command[0:4] == ("azd", "ai", "agent", "invoke"):
                stdout = smoke_output
            else:
                stdout = "{}"
            return module.CommandResult(returncode=0, stdout=stdout, stderr="")

    with pytest.raises(module.BootstrapCommandError, match="smoke test"):
        module.execute_bootstrap(plan, Runner())

    assert not any(
        command[0:4] == ("az", "functionapp", "config", "appsettings")
        for command in calls
    )


def test_active_agent_waits_for_delayed_deployment_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _bootstrap_module()
    plan = module.build_bootstrap_plan(_arguments(module, tmp_path))
    runner = _BootstrapRunner(
        module,
        plan,
        agent_payloads=[
            _hosted_agent_payload(
                plan,
                instance_principal_id=None,
                blueprint_principal_id=None,
            ),
            _hosted_agent_payload(plan),
        ],
    )
    pauses: list[int] = []
    monkeypatch.setattr(module.time, "sleep", pauses.append)

    deployment = module._wait_for_exact_active_agent(
        runner,
        expected_name=plan.managed_agent_name,
        cwd=plan.workspace_root,
    )

    assert deployment.instance_principal_id == _INSTANCE_PRINCIPAL_ID
    assert deployment.blueprint_principal_id == _BLUEPRINT_PRINCIPAL_ID
    assert pauses == [module._HOSTED_AGENT_WAIT_SECONDS]
    assert sum(command[0:4] == ("azd", "ai", "agent", "show") for command in runner.calls) == 2


def test_active_agent_wait_rejects_a_changed_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _bootstrap_module()
    plan = module.build_bootstrap_plan(_arguments(module, tmp_path))
    runner = _BootstrapRunner(
        module,
        plan,
        agent_payloads=[
            _hosted_agent_payload(
                plan,
                status="creating",
                instance_principal_id=None,
                blueprint_principal_id=None,
            ),
            _hosted_agent_payload(plan, version="8"),
        ],
    )
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    with pytest.raises(module.BootstrapCommandError, match="version changed"):
        module._wait_for_exact_active_agent(
            runner,
            expected_name=plan.managed_agent_name,
            cwd=plan.workspace_root,
        )


def test_default_app_insights_connection_requires_matching_project_managed_identity() -> None:
    module = _bootstrap_module()

    module._validate_default_app_insights_connection(
        _app_insights_connection_payload(),
        app_insights_resource_id=_APP_INSIGHTS_RESOURCE_ID,
    )

    with pytest.raises(module.BootstrapCommandError, match="different Application Insights"):
        module._validate_default_app_insights_connection(
            _app_insights_connection_payload(
                target=(
                    "/subscriptions/11111111-2222-3333-4444-555555555555"
                    "/resourceGroups/agents-rg/providers/Microsoft.Insights/components/other"
                )
            ),
            app_insights_resource_id=_APP_INSIGHTS_RESOURCE_ID,
        )
    with pytest.raises(module.BootstrapCommandError, match="AppKey"):
        module._validate_default_app_insights_connection(
            _app_insights_connection_payload(auth_type="AppKey"),
            app_insights_resource_id=_APP_INSIGHTS_RESOURCE_ID,
        )
    with pytest.raises(module.BootstrapCommandError, match="ProjectManagedIdentity"):
        module._validate_default_app_insights_connection(
            _app_insights_connection_payload(auth_type="ManagedIdentity"),
            app_insights_resource_id=_APP_INSIGHTS_RESOURCE_ID,
        )


def test_foundry_app_insights_lookup_uses_project_management_shape(tmp_path: Path) -> None:
    module = _bootstrap_module()
    plan = module.build_bootstrap_plan(_arguments(module, tmp_path))
    calls: list[tuple[str, ...]] = []

    class Runner:
        def run(self, command, *, cwd=None):
            del cwd
            calls.append(tuple(command))
            return module.CommandResult(
                returncode=0,
                stdout=_app_insights_connection_payload(),
                stderr="",
            )

    module._validate_foundry_default_app_insights_connection(
        plan,
        Runner(),
        app_insights_resource_id=_APP_INSIGHTS_RESOURCE_ID,
    )

    assert calls == [
        (
            "az",
            "rest",
            "--method",
            "GET",
            "--url",
            (
                "https://management.azure.com"
                f"{plan.arguments.project_resource_id}/connections"
                "?category=AppInsights&api-version=2025-09-01&includeAll=false"
            ),
            "--subscription",
            plan.arguments.subscription_id,
            "--output",
            "json",
        )
    ]


def test_function_app_app_insights_resource_lookup_resolves_one_link_or_setting(
    tmp_path: Path,
) -> None:
    module = _bootstrap_module()
    plan = module.build_bootstrap_plan(_arguments(module, tmp_path))

    class Runner:
        def __init__(self, function_app: dict[str, object], settings: list[object]) -> None:
            self.function_app = function_app
            self.settings = settings

        def run(self, command, *, cwd=None):
            del cwd
            if command[0:3] == ("az", "functionapp", "show"):
                stdout = json.dumps(self.function_app)
            else:
                stdout = json.dumps(self.settings)
            return module.CommandResult(returncode=0, stdout=stdout, stderr="")

    with pytest.raises(module.BootstrapCommandError, match="missing or ambiguous"):
        module._resolve_function_app_app_insights_resource_id(
            plan,
            Runner({"tags": {}}, []),
        )

    other_resource_id = _APP_INSIGHTS_RESOURCE_ID.rsplit("/", 1)[0] + "/other-app-insights"
    with pytest.raises(module.BootstrapCommandError, match="missing or ambiguous"):
        module._resolve_function_app_app_insights_resource_id(
            plan,
            Runner(
                {
                    "tags": {
                        "hidden-link:/app-insights-resource-id": _APP_INSIGHTS_RESOURCE_ID,
                        "hidden-related:insights-resource-id": other_resource_id,
                    }
                },
                [],
            ),
        )

    with pytest.raises(module.BootstrapCommandError, match="missing or ambiguous"):
        module._resolve_function_app_app_insights_resource_id(
            plan,
            Runner(
                {"tags": {}},
                [
                    {"name": "APPLICATIONINSIGHTS_RESOURCE_ID", "value": _APP_INSIGHTS_RESOURCE_ID},
                    {"name": "APPLICATIONINSIGHTS_RESOURCE_ID", "value": other_resource_id},
                ],
            ),
        )

    assert (
        module._resolve_function_app_app_insights_resource_id(
            plan,
            Runner(
                {"tags": {}},
                [{"name": "APPLICATIONINSIGHTS_RESOURCE_ID", "value": _APP_INSIGHTS_RESOURCE_ID}],
            ),
        )
        == _APP_INSIGHTS_RESOURCE_ID
    )

    override_plan = replace(
        plan,
        arguments=replace(plan.arguments, app_insights_resource_id=_APP_INSIGHTS_RESOURCE_ID),
    )

    class FailRunner:
        def run(self, command, *, cwd=None):
            del command, cwd
            raise AssertionError("An explicit App Insights resource ID must bypass discovery.")

    assert (
        module._resolve_function_app_app_insights_resource_id(override_plan, FailRunner())
        == _APP_INSIGHTS_RESOURCE_ID
    )


def test_observability_assignment_plan_is_deterministic_and_scoped(
    tmp_path: Path,
) -> None:
    module = _bootstrap_module()
    plan = module.build_bootstrap_plan(_arguments(module, tmp_path))
    deployment = module.HostedAgentDeployment(
        agent_name=plan.managed_agent_name,
        agent_version="7",
        instance_principal_id=_INSTANCE_PRINCIPAL_ID,
        blueprint_principal_id=_BLUEPRINT_PRINCIPAL_ID,
    )

    first = module._build_observability_role_assignments(
        plan,
        deployment=deployment,
        app_insights_resource_id=_APP_INSIGHTS_RESOURCE_ID,
    )
    second = module._build_observability_role_assignments(
        plan,
        deployment=deployment,
        app_insights_resource_id=_APP_INSIGHTS_RESOURCE_ID,
    )
    handoff = json.loads(
        module._render_observability_admin_handoff(
            first,
            subscription_id=plan.arguments.subscription_id,
        )
    )

    assert first == second
    assert len(first) == 4
    assert len({assignment.assignment_id for assignment in first}) == 4
    assert {assignment.role_definition_id for assignment in first} == {
        module._READER_ROLE_DEFINITION_ID,
        module._MONITORING_METRICS_PUBLISHER_ROLE_DEFINITION_ID,
    }
    assert {
        assignment.scope
        for assignment in first
        if assignment.role_definition_id == module._READER_ROLE_DEFINITION_ID
    } == {plan.arguments.project_resource_id.rsplit("/projects/", 1)[0]}
    assert {
        assignment.scope
        for assignment in first
        if assignment.role_definition_id == module._MONITORING_METRICS_PUBLISHER_ROLE_DEFINITION_ID
    } == {_APP_INSIGHTS_RESOURCE_ID}
    assert handoff["rerun"].startswith("Rerun the same bootstrap command")
    assert len(handoff["assignments"]) == 4
    assert all(
        entry["az_role_assignment_create_argv"][0:4]
        == ["az", "role", "assignment", "create"]
        for entry in handoff["assignments"]
    )


def test_auto_rbac_creates_only_missing_assignments_and_is_idempotent(tmp_path: Path) -> None:
    module = _bootstrap_module()
    arguments = _arguments(module, tmp_path)
    first_plan = module.build_bootstrap_plan(arguments)
    runner = _BootstrapRunner(module, first_plan)

    first_result = module.execute_bootstrap(first_plan, runner)

    first_creates = [
        command
        for command in runner.calls
        if command[0:4] == ("az", "role", "assignment", "create")
    ]
    assert len(first_creates) == 4
    assert first_result.managed_agent_version == "7"

    second_plan = module.build_bootstrap_plan(arguments)
    runner.set_plan(second_plan)
    second_result = module.execute_bootstrap(second_plan, runner)

    creates = [
        command
        for command in runner.calls
        if command[0:4] == ("az", "role", "assignment", "create")
    ]
    assert len(creates) == 4
    assert second_result.managed_agent_version == "7"


def test_plan_and_authorization_denial_emit_the_same_handoff_without_publishing(
    tmp_path: Path,
) -> None:
    module = _bootstrap_module()
    arguments = _arguments(module, tmp_path)
    plan_mode_plan = module.build_bootstrap_plan(replace(arguments, rbac_mode="plan"))
    plan_runner = _BootstrapRunner(module, plan_mode_plan)

    with pytest.raises(module.BootstrapRbacHandoffError) as plan_handoff:
        module.execute_bootstrap(plan_mode_plan, plan_runner)

    auto_plan = module.build_bootstrap_plan(arguments)
    denied_runner = _BootstrapRunner(
        module,
        auto_plan,
        deny_role_assignment_writes=True,
    )
    with pytest.raises(module.BootstrapRbacHandoffError) as denied_handoff:
        module.execute_bootstrap(auto_plan, denied_runner)

    assert str(plan_handoff.value) == str(denied_handoff.value)
    for calls in (plan_runner.calls, denied_runner.calls):
        assert not any(
            command[0:4] == ("az", "functionapp", "config", "appsettings")
            for command in calls
        )
        assert not any(command[0:3] == ("az", "functionapp", "restart") for command in calls)
        assert not any(command[0:4] == ("azd", "ai", "agent", "invoke") for command in calls)
    assert not any(
        command[0:4] == ("az", "role", "assignment", "create")
        for command in plan_runner.calls
    )
    assert sum(
        command[0:4] == ("az", "role", "assignment", "create")
        for command in denied_runner.calls
    ) == 1
    assert "secret" not in str(plan_handoff.value).casefold()


def test_auto_rbac_readback_failure_never_smokes_or_publishes(tmp_path: Path) -> None:
    module = _bootstrap_module()
    plan = module.build_bootstrap_plan(_arguments(module, tmp_path))
    runner = _BootstrapRunner(
        module,
        plan,
        drop_created_assignments=True,
    )

    with pytest.raises(module.BootstrapCommandError, match="read-back"):
        module.execute_bootstrap(plan, runner)

    assert sum(
        command[0:4] == ("az", "role", "assignment", "create")
        for command in runner.calls
    ) == 4
    assert not any(
        command[0:4] == ("az", "functionapp", "config", "appsettings")
        for command in runner.calls
    )
    assert not any(command[0:3] == ("az", "functionapp", "restart") for command in runner.calls)
    assert not any(command[0:4] == ("azd", "ai", "agent", "invoke") for command in runner.calls)
