"""Build a guarded bootstrap plan for one Foundry Hosted Agent Responses binding."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from azure_functions_agents.execution.foundry_application_content import (  # noqa: E402
    ApplicationContentManifest,
    ApplicationContentManifestError,
    build_application_content_manifest,
    compute_application_content_digest,
    serialize_application_content_manifest,
)
from azure_functions_agents.execution.foundry_responses_binding import (  # noqa: E402
    FHA_APPLICATION_CONTENT_DIGEST_ENV,
    FHA_APPLICATION_CONTENT_MANIFEST_ENV,
    FHA_BINDING_FINGERPRINT_ENV,
    FHA_MANAGED_AGENT_NAME_ENV,
    FHA_MANAGED_AGENT_VERSION_ENV,
    FHA_PROJECT_ENDPOINT_ENV,
    FHA_PROJECT_RESOURCE_ID_ENV,
    FHA_WRAPPER_DIGEST_ENV,
    compute_foundry_responses_binding_fingerprint,
)
from azure_functions_agents.foundry_responses.fha_hosted_source_staging import (  # noqa: E402
    FhaHostedDependencyPins,
    FhaHostedSourceArtifact,
    FhaHostedSourceStagingError,
    resolve_fha_runtime_pin,
    stage_fha_hosted_source,
)
from azure_functions_agents.foundry_responses.fha_model_catalog_gate import (  # noqa: E402
    compile_fha_v0_project,
)
from azure_functions_agents.foundry_responses.fha_runtime_projection import (  # noqa: E402
    FhaRuntimeProjection,
    compute_fha_wrapper_digest,
    serialize_fha_runtime_projection,
)
from azure_functions_agents.session_state import (  # noqa: E402
    AppIdentity,
    compute_app_hash,
    encode_label_safe_digest,
)

_DEFAULT_AGENTSERVER_CORE_PIN = "azure-ai-agentserver-core==2.1.0b1"
_DEFAULT_AGENTSERVER_RESPONSES_PIN = "azure-ai-agentserver-responses==2.1.0b1"
_DEFAULT_STAGE_ROOT = Path(tempfile.gettempdir()) / "azure-functions-agents-fha"
_BOOTSTRAP_EXECUTABLES = frozenset({"az", "azd"})
_WINDOWS_BATCH_SUFFIXES = frozenset({".bat", ".cmd"})
_AZD_DIAGNOSTIC_MAX_LENGTH = 800
_PROVENANCE_METADATA_KEY = "azure_functions_agents_provenance"
_ANSI_ESCAPE_SEQUENCE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SENSITIVE_AZD_DIAGNOSTIC = re.compile(
    r"""(?ix)
    (?:
        --(?:api[-_]?key|access[-_]?token|client[-_]?secret|connection[-_]?string|password|secret|token)
        \s+\S+
        |
        ["']?(?:api[-_]?key|access[-_]?token|client[-_]?secret|connection[-_]?string|password|secret|token|authorization)["']?
        \s*[:=]\s*(?:"[^"]*"|'[^']*'|\S+)
        |
        \bbearer\s+\S+
        |
        https?://[^\s?]+\?[^\s]+
    )
    """
)
_AZD_ERROR_MARKERS = (
    "cannot",
    "error",
    "failed",
    "invalid",
    "missing",
    "not found",
    "required",
    "unable",
    "unsupported",
)
_FOUNDRY_PROJECT_CONNECTIONS_API_VERSION = "2025-09-01"
_HOSTED_AGENT_WAIT_ATTEMPTS = 30
_HOSTED_AGENT_WAIT_SECONDS = 2
_RBAC_MODE_AUTO = "auto"
_RBAC_MODE_PLAN = "plan"
_READER_ROLE_DEFINITION_ID = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
_MONITORING_METRICS_PUBLISHER_ROLE_DEFINITION_ID = "3913510d-42f4-4e42-8a64-420c390055eb"
_HIDDEN_APP_INSIGHTS_RESOURCE_ID_NAMES = frozenset(
    {
        "hidden-link:/app-insights-resource-id",
        "hidden-related:insights-resource-id",
    }
)
_APP_INSIGHTS_RESOURCE_ID_SETTING_NAMES = frozenset({"applicationinsights_resource_id"})


class BootstrapCommandError(RuntimeError):
    """A read-only bootstrap preflight command failed."""


class BootstrapRbacHandoffError(BootstrapCommandError):
    """An administrator must apply the required hosted-observability roles."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    """One command runner result."""

    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """Narrow command boundary used by guarded bootstrap execution."""

    def run(self, command: Sequence[str], *, cwd: Path | None = None) -> CommandResult:
        """Run one command without a shell."""


class SubprocessCommandRunner:
    """Production command runner for explicit user-invoked bootstrap execution."""

    def run(self, command: Sequence[str], *, cwd: Path | None = None) -> CommandResult:
        environment = os.environ.copy()
        if command and command[0] == "azd":
            environment["AZURE_DEV_USER_AGENT"] = "microsoft_foundry_skill"
        resolved_command = _resolve_bootstrap_command(command)
        try:
            completed = subprocess.run(
                list(resolved_command),
                cwd=cwd,
                env=environment,
                check=False,
                capture_output=True,
                shell=False,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            raise BootstrapCommandError("Bootstrap command could not be started.") from None
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def _resolve_bootstrap_command(command: Sequence[str]) -> tuple[str, ...]:
    """Resolve one allowlisted bootstrap executable without using a shell."""
    if not command or command[0] not in _BOOTSTRAP_EXECUTABLES:
        raise BootstrapCommandError("Bootstrap command is not permitted.")
    executable_name = command[0]
    resolved = shutil.which(executable_name)
    if resolved is None:
        raise BootstrapCommandError(
            f"Required bootstrap executable '{executable_name}' is unavailable."
        )
    executable_path = Path(resolved).resolve()
    if not executable_path.is_file():
        raise BootstrapCommandError(
            f"Required bootstrap executable '{executable_name}' is unavailable."
        )
    if _is_windows_batch_wrapper(executable_path):
        return _resolve_windows_batch_command(executable_name, executable_path, command[1:])
    return (str(executable_path), *command[1:])


def _is_windows_batch_wrapper(executable_path: Path) -> bool:
    return os.name == "nt" and executable_path.suffix.casefold() in _WINDOWS_BATCH_SUFFIXES


def _resolve_windows_batch_command(
    executable_name: str,
    wrapper_path: Path,
    arguments: Sequence[str],
) -> tuple[str, ...]:
    """Use the Azure CLI's native Python host instead of its batch shim."""
    if executable_name != "az":
        raise BootstrapCommandError(
            f"Required bootstrap executable '{executable_name}' is unavailable."
        )
    azure_cli_python = wrapper_path.parent.parent / "python.exe"
    if not azure_cli_python.is_file():
        raise BootstrapCommandError("Required bootstrap executable 'az' is unavailable.")
    return (
        str(azure_cli_python.resolve()),
        "-IBm",
        "azure.cli",
        *arguments,
    )


@dataclass(frozen=True, slots=True)
class BootstrapArguments:
    """Validated inputs needed to stage and publish a single app-scoped binding."""

    application_root: Path
    stage_root: Path
    subscription_id: str
    function_app_name: str
    function_app_slot: str | None
    resource_group: str
    setup_principal_id: str | None
    project_endpoint: str
    project_resource_id: str
    model_deployment_name: str
    runtime_pin: str | None
    agentserver_core_pin: str | None
    agentserver_responses_pin: str | None
    app_insights_resource_id: str | None = None
    rbac_mode: str = _RBAC_MODE_AUTO
    capture_trace_content: bool = False


@dataclass(frozen=True, slots=True)
class BootstrapPlan:
    """Deterministic local staging output and Azure bootstrap inputs."""

    arguments: BootstrapArguments
    app_identity: AppIdentity
    managed_agent_name: str
    application_root: Path
    workspace_root: Path
    manifest: ApplicationContentManifest
    application_content_digest: str
    wrapper_digest: str
    projection: FhaRuntimeProjection
    smoke_agent_slug: str
    artifact: FhaHostedSourceArtifact
    preflight_commands: tuple[tuple[str, ...], ...]
    hosted_agent_spec: dict[str, str]


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """One successfully deployed and published hosted-agent binding."""

    managed_agent_name: str
    managed_agent_version: str
    app_settings: dict[str, str]


@dataclass(frozen=True, slots=True)
class HostedAgentDeployment:
    """The exact active hosted-agent version and its deployment identities."""

    agent_name: str
    agent_version: str
    instance_principal_id: str
    blueprint_principal_id: str


@dataclass(frozen=True, slots=True)
class ObservabilityRoleAssignment:
    """One deployment-owned hosted-observability role assignment."""

    principal_kind: str
    principal_id: str
    scope: str
    role_definition_id: str
    role_name: str
    assignment_id: str

    def create_command(self, *, subscription_id: str) -> tuple[str, ...]:
        """Build the exact Azure CLI command required to create this assignment."""
        return (
            "az",
            "role",
            "assignment",
            "create",
            "--assignee-object-id",
            self.principal_id,
            "--assignee-principal-type",
            "ServicePrincipal",
            "--role",
            self.role_definition_id,
            "--scope",
            self.scope,
            "--name",
            self.assignment_id,
            "--subscription",
            subscription_id,
        )


def build_bootstrap_plan(arguments: BootstrapArguments) -> BootstrapPlan:
    """Validate, stage, and generate a non-mutating bootstrap plan."""
    if arguments.rbac_mode not in {_RBAC_MODE_AUTO, _RBAC_MODE_PLAN}:
        raise BootstrapCommandError("Bootstrap RBAC mode must be 'auto' or 'plan'.")
    if arguments.app_insights_resource_id is not None:
        _require_app_insights_resource_id(arguments.app_insights_resource_id)
    application_root_candidate = Path(arguments.application_root)
    if application_root_candidate.is_symlink():
        raise BootstrapCommandError("Bootstrap application root must not be a link.")
    application_root = application_root_candidate.resolve()
    try:
        compiled_project = compile_fha_v0_project(
            application_root,
            project_endpoint=arguments.project_endpoint,
            default_model=arguments.model_deployment_name,
        )
    except ValueError as exc:
        raise BootstrapCommandError("Bootstrap FHA V0 compilation failed.") from exc
    try:
        source_manifest = build_application_content_manifest(application_root)
        manifest = ApplicationContentManifest.create(
            entries=source_manifest.entries,
            runtime_projection=serialize_fha_runtime_projection(
                compiled_project.projection
            ),
        )
        application_content_digest = compute_application_content_digest(application_root, manifest)
    except ApplicationContentManifestError as exc:
        raise BootstrapCommandError("Bootstrap application content is invalid.") from exc
    catalog = compiled_project.catalog
    if not catalog:
        raise BootstrapCommandError("Bootstrap application has no hosted agent.")
    smoke_agent_slug = sorted(catalog)[0]
    try:
        pins = FhaHostedDependencyPins.create(
            runtime=resolve_fha_runtime_pin(application_root, arguments.runtime_pin),
            agentserver_core=(
                arguments.agentserver_core_pin or _DEFAULT_AGENTSERVER_CORE_PIN
            ),
            agentserver_responses=(
                arguments.agentserver_responses_pin
                or _DEFAULT_AGENTSERVER_RESPONSES_PIN
            ),
        )
    except FhaHostedSourceStagingError as exc:
        raise BootstrapCommandError("Bootstrap hosted dependency pins are invalid.") from exc
    staging_parent_candidate = Path(arguments.stage_root)
    if staging_parent_candidate.is_symlink():
        raise BootstrapCommandError("Bootstrap staging root must not be a link.")
    staging_parent = staging_parent_candidate.resolve()
    snapshot_root = _create_staging_directory(staging_parent, prefix="snapshot-")
    _seal_application_snapshot(
        application_root,
        snapshot_root,
        manifest,
        expected_digest=application_content_digest,
    )
    artifact_root = _create_staging_directory(
        staging_parent,
        prefix=f"{application_content_digest.removeprefix('sha256:')[:12]}-",
    )
    workspace_root = _create_staging_directory(staging_parent, prefix="azd-")
    try:
        artifact = stage_fha_hosted_source(
            application_root=snapshot_root,
            stage_root=artifact_root,
            selected_relative_paths=[entry.path for entry in manifest.entries],
            dependency_pins=pins,
            projection=compiled_project.projection,
        )
    except FhaHostedSourceStagingError as exc:
        raise BootstrapCommandError("Bootstrap hosted source staging failed.") from exc
    _stage_runtime_source(artifact)
    wrapper_digest = compute_fha_wrapper_digest(
        compiled_project.projection,
        artifact.rendered_entrypoint,
    )
    app_identity = AppIdentity.create(
        subscription_id=arguments.subscription_id,
        site_name=arguments.function_app_name,
        slot_name=arguments.function_app_slot,
    )
    managed_agent_name = f"afa-v2-{compute_app_hash(app_identity)}"
    provenance_tag = f"afa-provenance:{managed_agent_name}"
    slot_arguments = (
        () if arguments.function_app_slot is None else ("--slot", arguments.function_app_slot)
    )
    preflight_commands = (
        (
            "az",
            "account",
            "show",
            "--subscription",
            arguments.subscription_id,
            "--output",
            "json",
        ),
        (
            "az",
            "functionapp",
            "identity",
            "show",
            "--resource-group",
            arguments.resource_group,
            "--name",
            arguments.function_app_name,
            "--subscription",
            arguments.subscription_id,
            "--output",
            "json",
            *slot_arguments,
        ),
        ("azd", "ai", "agent", "--help"),
    )
    return BootstrapPlan(
        arguments=arguments,
        app_identity=app_identity,
        managed_agent_name=managed_agent_name,
        application_root=application_root,
        workspace_root=workspace_root,
        manifest=manifest,
        application_content_digest=application_content_digest,
        wrapper_digest=wrapper_digest,
        projection=compiled_project.projection,
        smoke_agent_slug=smoke_agent_slug,
        artifact=artifact,
        preflight_commands=preflight_commands,
        hosted_agent_spec={
            "project_resource_id": arguments.project_resource_id,
            "managed_agent_name": managed_agent_name,
            "provenance_tag": provenance_tag,
            "staged_source_root": str(artifact.stage_root),
            "required_lifecycle": "create-or-update-one-agent, wait-active, smoke-test",
        },
    )


def execute_bootstrap(plan: BootstrapPlan, runner: CommandRunner) -> BootstrapResult:
    """Deploy one app-scoped FHA, smoke-test it, and publish its verified binding."""
    account_payload: str | None = None
    function_principal_id: str | None = None
    for index, command in enumerate(plan.preflight_commands):
        result = runner.run(command, cwd=plan.application_root)
        if result.returncode != 0:
            raise BootstrapCommandError(
                f"Bootstrap preflight failed for {command[0]} {command[1]}."
            )
        if index == 0:
            account_payload = result.stdout
        elif index == 1:
            function_principal_id = _resolve_function_principal_id(result.stdout)

    if account_payload is None or function_principal_id is None:
        raise BootstrapCommandError("Bootstrap preflight did not resolve required identities.")
    setup_assignee = _resolve_setup_assignee(
        plan.arguments.setup_principal_id,
        account_payload,
    )
    setup_roles = _run_required(
        runner,
        (
            "az",
            "role",
            "assignment",
            "list",
            "--assignee",
            setup_assignee,
            "--scope",
            plan.arguments.project_resource_id,
            "--include-inherited",
            "--output",
            "json",
            "--subscription",
            plan.arguments.subscription_id,
        ),
        cwd=plan.application_root,
        operation="setup role preflight",
    )
    _validate_role_assignments(
        setup_roles.stdout,
        expected={"Foundry Project Manager", "Azure AI Project Manager"},
    )

    runtime_roles = _run_required(
        runner,
        (
            "az",
            "role",
            "assignment",
            "list",
            "--assignee",
            function_principal_id,
            "--scope",
            plan.arguments.project_resource_id,
            "--include-inherited",
            "--output",
            "json",
            "--subscription",
            plan.arguments.subscription_id,
        ),
        cwd=plan.application_root,
        operation="runtime role preflight",
    )
    _validate_role_assignments(
        runtime_roles.stdout,
        expected={"Foundry Agent Consumer", "Azure AI User"},
    )
    _validate_existing_agent_provenance(
        runner,
        project_endpoint=plan.projection.project_endpoint,
        agent_name=plan.managed_agent_name,
        provenance_tag=plan.hosted_agent_spec["provenance_tag"],
        subscription_id=plan.arguments.subscription_id,
        cwd=plan.application_root,
    )

    plan.workspace_root.mkdir(parents=True, exist_ok=True)
    _stage_azd_workspace(plan.artifact.stage_root, plan.workspace_root)
    init_command = (
        "azd",
        "ai",
        "agent",
        "init",
        "--no-prompt",
        "--agent-name",
        plan.managed_agent_name,
        "--project-id",
        plan.arguments.project_resource_id,
        "--model-deployment",
        plan.projection.default_model,
        "--deploy-mode",
        "code",
        "--runtime",
        "python_3_13",
        "--entry-point",
        plan.artifact.entrypoint_path.name,
        "--dep-resolution",
        "remote_build",
        "--protocol",
        "responses",
    )
    _run_required(runner, init_command, cwd=plan.workspace_root, operation="agent init")
    _stamp_azd_provenance(
        plan.workspace_root,
        plan.hosted_agent_spec["provenance_tag"],
        projection=plan.projection,
        capture_trace_content=plan.arguments.capture_trace_content,
    )
    _run_required(
        runner,
        ("azd", "deploy", "--all", "--no-prompt"),
        cwd=plan.workspace_root,
        operation="agent deploy",
    )
    deployment = _wait_for_exact_active_agent(
        runner,
        expected_name=plan.managed_agent_name,
        cwd=plan.workspace_root,
    )
    app_insights_resource_id = _resolve_function_app_app_insights_resource_id(plan, runner)
    _validate_foundry_default_app_insights_connection(
        plan,
        runner,
        app_insights_resource_id=app_insights_resource_id,
    )
    observability_assignments = _build_observability_role_assignments(
        plan,
        deployment=deployment,
        app_insights_resource_id=app_insights_resource_id,
    )
    _ensure_observability_role_assignments(
        plan,
        runner,
        assignments=observability_assignments,
    )
    smoke_nonce = uuid4().hex
    smoke_history_scope = "o1-" + encode_label_safe_digest(
        hashlib.sha256(smoke_nonce.encode("ascii")).digest()
    )
    smoke_envelope = json.dumps(
        {
            "agent_slug": plan.smoke_agent_slug,
            "history_scope": smoke_history_scope,
            "runtime_session_id": f"bootstrap-smoke-session-{smoke_nonce}",
            "runtime_run_id": f"bootstrap-smoke-run-{smoke_nonce}",
            "prompt": "Reply with exactly READY.",
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    smoke = _run_required(
        runner,
        (
            "azd",
            "ai",
            "agent",
            "invoke",
            "--new-session",
            "--version",
            deployment.agent_version,
            "--timeout",
            "180",
            smoke_envelope,
        ),
        cwd=plan.workspace_root,
        operation="agent smoke test",
    )
    if not _smoke_output_is_ready(smoke.stdout, agent_name=deployment.agent_name):
        raise BootstrapCommandError("Hosted agent smoke test returned unexpected output.")

    app_settings = _binding_settings(
        plan,
        agent_name=deployment.agent_name,
        agent_version=deployment.agent_version,
    )
    slot_arguments = (
        ()
        if plan.arguments.function_app_slot is None
        else ("--slot", plan.arguments.function_app_slot)
    )
    _run_required(
        runner,
        (
            "az",
            "functionapp",
            "config",
            "appsettings",
            "set",
            "--resource-group",
            plan.arguments.resource_group,
            "--name",
            plan.arguments.function_app_name,
            *slot_arguments,
            "--settings",
            *tuple(f"{key}={value}" for key, value in app_settings.items()),
            "--subscription",
            plan.arguments.subscription_id,
        ),
        cwd=plan.application_root,
        operation="binding publication",
    )
    _run_required(
        runner,
        (
            "az",
            "functionapp",
            "restart",
            "--resource-group",
            plan.arguments.resource_group,
            "--name",
            plan.arguments.function_app_name,
            *slot_arguments,
            "--subscription",
            plan.arguments.subscription_id,
        ),
        cwd=plan.application_root,
        operation="Function App restart",
    )
    return BootstrapResult(
        managed_agent_name=deployment.agent_name,
        managed_agent_version=deployment.agent_version,
        app_settings=app_settings,
    )


def _binding_settings(
    plan: BootstrapPlan,
    *,
    agent_name: str,
    agent_version: str,
) -> dict[str, str]:
    manifest_json = serialize_application_content_manifest(plan.manifest)
    settings = {
        FHA_PROJECT_ENDPOINT_ENV: plan.projection.project_endpoint,
        FHA_PROJECT_RESOURCE_ID_ENV: plan.arguments.project_resource_id,
        FHA_MANAGED_AGENT_NAME_ENV: agent_name,
        FHA_MANAGED_AGENT_VERSION_ENV: agent_version,
        FHA_APPLICATION_CONTENT_MANIFEST_ENV: manifest_json,
        FHA_APPLICATION_CONTENT_DIGEST_ENV: plan.application_content_digest,
        FHA_WRAPPER_DIGEST_ENV: plan.wrapper_digest,
    }
    settings[FHA_BINDING_FINGERPRINT_ENV] = compute_foundry_responses_binding_fingerprint(
        app_identity=plan.app_identity,
        project_endpoint=settings[FHA_PROJECT_ENDPOINT_ENV],
        project_resource_id=settings[FHA_PROJECT_RESOURCE_ID_ENV],
        managed_agent_name=settings[FHA_MANAGED_AGENT_NAME_ENV],
        managed_agent_version=settings[FHA_MANAGED_AGENT_VERSION_ENV],
        application_content_manifest=settings[FHA_APPLICATION_CONTENT_MANIFEST_ENV],
        application_content_digest=settings[FHA_APPLICATION_CONTENT_DIGEST_ENV],
        wrapper_digest=settings[FHA_WRAPPER_DIGEST_ENV],
    )
    return settings


def _wait_for_exact_active_agent(
    runner: CommandRunner,
    *,
    expected_name: str,
    cwd: Path,
) -> HostedAgentDeployment:
    """Wait for one immutable hosted-agent version and both deployment identities."""
    expected_version: str | None = None
    for attempt in range(_HOSTED_AGENT_WAIT_ATTEMPTS):
        show = _run_required(
            runner,
            ("azd", "ai", "agent", "show", "--output", "json"),
            cwd=cwd,
            operation="agent show",
        )
        document, observed_version, status = _parse_hosted_agent_status(
            show.stdout,
            expected_name=expected_name,
        )
        if expected_version is None:
            expected_version = observed_version
        elif observed_version != expected_version:
            raise BootstrapCommandError("Hosted agent version changed while activation was awaited.")
        principal_ids = _resolve_hosted_agent_principal_ids(document)
        if status.casefold() == "active" and principal_ids is not None:
            return HostedAgentDeployment(
                agent_name=expected_name,
                agent_version=expected_version,
                instance_principal_id=principal_ids[0],
                blueprint_principal_id=principal_ids[1],
            )
        if attempt + 1 < _HOSTED_AGENT_WAIT_ATTEMPTS:
            time.sleep(_HOSTED_AGENT_WAIT_SECONDS)
    raise BootstrapCommandError(
        "Hosted agent did not reach the expected active version with deployment identities."
    )


def _parse_hosted_agent_status(
    payload: str,
    *,
    expected_name: str,
) -> tuple[dict[str, object], str, str]:
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise BootstrapCommandError("Hosted agent status returned invalid JSON.") from exc
    if not isinstance(document, dict):
        raise BootstrapCommandError("Hosted agent status returned invalid JSON.")
    name = document.get("name") or document.get("agentName")
    version = document.get("version") or document.get("agentVersion")
    status = document.get("status")
    if (
        name != expected_name
        or not isinstance(version, (str, int))
        or not str(version).strip()
        or not isinstance(status, str)
        or not status.strip()
    ):
        raise BootstrapCommandError("Hosted agent did not reach the expected active version.")
    return document, str(version).strip(), status.strip()


def _resolve_hosted_agent_principal_ids(
    document: Mapping[str, object],
) -> tuple[str, str] | None:
    instance_identity = document.get("instance_identity")
    blueprint = document.get("blueprint")
    if not isinstance(instance_identity, Mapping) or not isinstance(blueprint, Mapping):
        return None
    instance_principal_id = _valid_principal_id(instance_identity.get("principal_id"))
    blueprint_principal_id = _valid_principal_id(blueprint.get("principal_id"))
    if instance_principal_id is None or blueprint_principal_id is None:
        return None
    return instance_principal_id, blueprint_principal_id


def _resolve_function_app_app_insights_resource_id(
    plan: BootstrapPlan,
    runner: CommandRunner,
) -> str:
    override = plan.arguments.app_insights_resource_id
    if override is not None:
        return _require_app_insights_resource_id(override)
    slot_arguments = (
        () if plan.arguments.function_app_slot is None else ("--slot", plan.arguments.function_app_slot)
    )
    function_app = _run_required(
        runner,
        (
            "az",
            "functionapp",
            "show",
            "--resource-group",
            plan.arguments.resource_group,
            "--name",
            plan.arguments.function_app_name,
            *slot_arguments,
            "--subscription",
            plan.arguments.subscription_id,
            "--output",
            "json",
        ),
        cwd=plan.application_root,
        operation="Function App App Insights lookup",
    )
    try:
        function_app_document = json.loads(function_app.stdout)
    except json.JSONDecodeError as exc:
        raise BootstrapCommandError("Function App App Insights lookup returned invalid JSON.") from exc
    if not isinstance(function_app_document, dict):
        raise BootstrapCommandError("Function App App Insights lookup returned invalid JSON.")
    tag_resource_ids = _app_insights_resource_ids(
        function_app_document.get("tags"),
        names=_HIDDEN_APP_INSIGHTS_RESOURCE_ID_NAMES,
    )
    if tag_resource_ids:
        return _single_app_insights_resource_id(tag_resource_ids)
    app_settings = _run_required(
        runner,
        (
            "az",
            "functionapp",
            "config",
            "appsettings",
            "list",
            "--resource-group",
            plan.arguments.resource_group,
            "--name",
            plan.arguments.function_app_name,
            *slot_arguments,
            "--subscription",
            plan.arguments.subscription_id,
            "--query",
            "[?name=='APPLICATIONINSIGHTS_RESOURCE_ID']",
            "--output",
            "json",
        ),
        cwd=plan.application_root,
        operation="Function App App Insights settings lookup",
    )
    try:
        settings_document = json.loads(app_settings.stdout)
    except json.JSONDecodeError as exc:
        raise BootstrapCommandError(
            "Function App App Insights settings lookup returned invalid JSON."
        ) from exc
    if not isinstance(settings_document, list):
        raise BootstrapCommandError("Function App App Insights settings lookup returned invalid JSON.")
    setting_resource_ids: dict[str, str] = {}
    for setting in settings_document:
        if not isinstance(setting, dict):
            raise BootstrapCommandError(
                "Function App App Insights settings lookup returned invalid JSON."
            )
        name = setting.get("name")
        if (
            not isinstance(name, str)
            or name.casefold() not in _APP_INSIGHTS_RESOURCE_ID_SETTING_NAMES
        ):
            continue
        resource_id = _require_app_insights_resource_id(setting.get("value"))
        setting_resource_ids.setdefault(_resource_id_key(resource_id), resource_id)
    return _single_app_insights_resource_id(tuple(setting_resource_ids.values()))


def _app_insights_resource_ids(
    values: object,
    *,
    names: frozenset[str],
) -> tuple[str, ...]:
    if values is None:
        return ()
    if not isinstance(values, Mapping):
        raise BootstrapCommandError("Function App App Insights resource link was invalid.")
    resource_ids: dict[str, str] = {}
    for name, value in values.items():
        if not isinstance(name, str) or name.casefold() not in names:
            continue
        resource_id = _require_app_insights_resource_id(value)
        resource_ids.setdefault(_resource_id_key(resource_id), resource_id)
    return tuple(resource_ids.values())


def _single_app_insights_resource_id(resource_ids: Sequence[str]) -> str:
    if len(resource_ids) != 1:
        raise BootstrapCommandError(
            "Function App App Insights resource ID was missing or ambiguous; "
            "pass --app-insights-resource-id explicitly."
        )
    return resource_ids[0]


def _require_app_insights_resource_id(value: object) -> str:
    if not isinstance(value, str):
        raise BootstrapCommandError("Application Insights resource ID was invalid.")
    candidate = value.strip().rstrip("/")
    parts = [part for part in candidate.split("/") if part]
    provider_index = next(
        (
            index
            for index, part in enumerate(parts)
            if part.casefold() == "providers"
            and len(parts) == index + 4
            and parts[index + 1].casefold() == "microsoft.insights"
            and parts[index + 2].casefold() == "components"
            and bool(parts[index + 3].strip())
        ),
        None,
    )
    if provider_index is None:
        raise BootstrapCommandError("Application Insights resource ID was invalid.")
    return "/" + "/".join(parts)


def _resource_id_key(resource_id: str) -> str:
    return resource_id.rstrip("/").casefold()


def _validate_foundry_default_app_insights_connection(
    plan: BootstrapPlan,
    runner: CommandRunner,
    *,
    app_insights_resource_id: str,
) -> None:
    connection_result = _run_required(
        runner,
        (
            "az",
            "rest",
            "--method",
            "GET",
            "--url",
            (
                "https://management.azure.com"
                f"{plan.arguments.project_resource_id.rstrip('/')}"
                "/connections?category=AppInsights"
                f"&api-version={_FOUNDRY_PROJECT_CONNECTIONS_API_VERSION}"
                "&includeAll=false"
            ),
            "--subscription",
            plan.arguments.subscription_id,
            "--output",
            "json",
        ),
        cwd=plan.application_root,
        operation="Foundry App Insights connection lookup",
    )
    _validate_default_app_insights_connection(
        connection_result.stdout,
        app_insights_resource_id=app_insights_resource_id,
    )


def _validate_default_app_insights_connection(
    payload: str,
    *,
    app_insights_resource_id: str,
) -> None:
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise BootstrapCommandError("Foundry App Insights connection lookup returned invalid JSON.") from exc
    connections = document.get("value") if isinstance(document, dict) else None
    if not isinstance(connections, list):
        raise BootstrapCommandError("Foundry App Insights connection lookup returned invalid JSON.")
    app_insights_connections = [
        connection
        for connection in connections
        if isinstance(connection, dict) and _is_app_insights_connection(connection)
    ]
    defaults = [
        connection for connection in app_insights_connections if _is_default_connection(connection)
    ]
    if len(defaults) > 1:
        raise BootstrapCommandError("Foundry project has ambiguous default App Insights connections.")
    if defaults:
        connection = defaults[0]
    elif len(app_insights_connections) == 1:
        connection = app_insights_connections[0]
    elif not app_insights_connections:
        raise BootstrapCommandError("Foundry project has no default App Insights connection.")
    else:
        raise BootstrapCommandError("Foundry project has ambiguous App Insights connections.")
    properties = _connection_properties(connection)
    target = properties.get("target")
    if target is None:
        target = connection.get("target")
    try:
        target_resource_id = _require_app_insights_resource_id(target)
    except BootstrapCommandError as exc:
        raise BootstrapCommandError("Foundry default App Insights connection target was invalid.") from exc
    if _resource_id_key(target_resource_id) != _resource_id_key(app_insights_resource_id):
        raise BootstrapCommandError(
            "Foundry default App Insights connection targets a different Application Insights resource."
        )
    auth_type = properties.get("authType")
    if auth_type is None:
        auth_type = connection.get("authType")
    if not isinstance(auth_type, str):
        raise BootstrapCommandError("Foundry default App Insights connection authentication was invalid.")
    if auth_type.casefold() == "appkey":
        raise BootstrapCommandError(
            "Foundry default App Insights connection uses unsupported AppKey authentication."
        )
    if auth_type.casefold() != "projectmanagedidentity":
        raise BootstrapCommandError(
            "Foundry default App Insights connection must use ProjectManagedIdentity authentication."
        )


def _connection_properties(connection: Mapping[str, object]) -> Mapping[str, object]:
    properties = connection.get("properties")
    return properties if isinstance(properties, Mapping) else connection


def _is_app_insights_connection(connection: Mapping[str, object]) -> bool:
    properties = _connection_properties(connection)
    values = (
        connection.get("type"),
        connection.get("category"),
        connection.get("kind"),
        properties.get("type"),
        properties.get("category"),
        properties.get("kind"),
    )
    return any(
        isinstance(value, str) and value.casefold() in {"appinsights", "applicationinsights"}
        for value in values
    )


def _is_default_connection(connection: Mapping[str, object]) -> bool:
    properties = _connection_properties(connection)
    values = (
        connection.get("isDefault"),
        connection.get("is_default"),
        properties.get("isDefault"),
        properties.get("is_default"),
    )
    return any(value is True or value == "true" for value in values)


def _build_observability_role_assignments(
    plan: BootstrapPlan,
    *,
    deployment: HostedAgentDeployment,
    app_insights_resource_id: str,
) -> tuple[ObservabilityRoleAssignment, ...]:
    account_scope = _foundry_account_scope(plan.arguments.project_resource_id)
    roles = (
        (account_scope, _READER_ROLE_DEFINITION_ID, "Reader"),
        (
            app_insights_resource_id,
            _MONITORING_METRICS_PUBLISHER_ROLE_DEFINITION_ID,
            "Monitoring Metrics Publisher",
        ),
    )
    principals = (
        ("instance", deployment.instance_principal_id),
        ("blueprint", deployment.blueprint_principal_id),
    )
    return tuple(
        ObservabilityRoleAssignment(
            principal_kind=principal_kind,
            principal_id=principal_id,
            scope=scope,
            role_definition_id=role_definition_id,
            role_name=role_name,
            assignment_id=_role_assignment_id(
                principal_id=principal_id,
                scope=scope,
                role_definition_id=role_definition_id,
            ),
        )
        for principal_kind, principal_id in principals
        for scope, role_definition_id, role_name in roles
    )


def _foundry_account_scope(project_resource_id: str) -> str:
    parts = [part for part in project_resource_id.strip().rstrip("/").split("/") if part]
    provider_index = next(
        (
            index
            for index, part in enumerate(parts)
            if part.casefold() == "providers"
            and len(parts) == index + 6
            and parts[index + 1].casefold() == "microsoft.cognitiveservices"
            and parts[index + 2].casefold() == "accounts"
            and bool(parts[index + 3].strip())
            and parts[index + 4].casefold() == "projects"
            and bool(parts[index + 5].strip())
        ),
        None,
    )
    if provider_index is None:
        raise BootstrapCommandError("Foundry project resource ID was invalid.")
    return "/" + "/".join(parts[: provider_index + 4])


def _role_assignment_id(
    *,
    principal_id: str,
    scope: str,
    role_definition_id: str,
) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            (
                "azure-functions-agents-fha-observability-v1/"
                f"{principal_id.casefold()}/{_resource_id_key(scope)}/{role_definition_id.casefold()}"
            ),
        )
    )


def _ensure_observability_role_assignments(
    plan: BootstrapPlan,
    runner: CommandRunner,
    *,
    assignments: Sequence[ObservabilityRoleAssignment],
) -> None:
    handoff = _render_observability_admin_handoff(
        assignments,
        subscription_id=plan.arguments.subscription_id,
    )
    if plan.arguments.rbac_mode == _RBAC_MODE_PLAN:
        raise BootstrapRbacHandoffError(handoff)
    missing_assignments = tuple(
        assignment
        for assignment in assignments
        if not _role_assignment_exists(plan, runner, assignment)
    )
    for assignment in missing_assignments:
        result = runner.run(
            assignment.create_command(subscription_id=plan.arguments.subscription_id),
            cwd=plan.application_root,
        )
        if result.returncode != 0:
            if _role_assignment_write_was_denied(result):
                raise BootstrapRbacHandoffError(handoff)
            if _role_assignment_already_exists(result) and _role_assignment_exists(
                plan,
                runner,
                assignment,
            ):
                continue
            raise BootstrapCommandError("Bootstrap hosted-observability role assignment failed.")
    for assignment in assignments:
        if not _role_assignment_exists(plan, runner, assignment):
            raise BootstrapCommandError(
                "Bootstrap hosted-observability role assignment read-back failed."
            )


def _role_assignment_exists(
    plan: BootstrapPlan,
    runner: CommandRunner,
    assignment: ObservabilityRoleAssignment,
) -> bool:
    result = _run_required(
        runner,
        (
            "az",
            "role",
            "assignment",
            "list",
            "--assignee-object-id",
            assignment.principal_id,
            "--scope",
            assignment.scope,
            "--fill-principal-name",
            "false",
            "--subscription",
            plan.arguments.subscription_id,
            "--output",
            "json",
        ),
        cwd=plan.application_root,
        operation="hosted-observability role lookup",
    )
    try:
        role_assignments = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BootstrapCommandError(
            "Bootstrap hosted-observability role lookup returned invalid JSON."
        ) from exc
    if not isinstance(role_assignments, list):
        raise BootstrapCommandError("Bootstrap hosted-observability role lookup returned invalid JSON.")
    return any(
        isinstance(candidate, dict) and _is_exact_role_assignment(candidate, assignment)
        for candidate in role_assignments
    )


def _is_exact_role_assignment(
    candidate: Mapping[str, object],
    assignment: ObservabilityRoleAssignment,
) -> bool:
    principal_id = candidate.get("principalId")
    scope = candidate.get("scope")
    role_definition_id = candidate.get("roleDefinitionId")
    return (
        isinstance(principal_id, str)
        and principal_id.casefold() == assignment.principal_id.casefold()
        and isinstance(scope, str)
        and _resource_id_key(scope) == _resource_id_key(assignment.scope)
        and isinstance(role_definition_id, str)
        and role_definition_id.rstrip("/").rsplit("/", 1)[-1].casefold()
        == assignment.role_definition_id.casefold()
    )


def _role_assignment_write_was_denied(result: CommandResult) -> bool:
    diagnostic = f"{result.stdout}\n{result.stderr}".casefold()
    return (
        "roleassignments/write" in diagnostic
        and any(
            marker in diagnostic
            for marker in ("authorization", "forbidden", "denied", "not authorized")
        )
    )


def _role_assignment_already_exists(result: CommandResult) -> bool:
    diagnostic = f"{result.stdout}\n{result.stderr}".casefold()
    return "roleassignmentexists" in diagnostic or "already exists" in diagnostic


def _render_observability_admin_handoff(
    assignments: Sequence[ObservabilityRoleAssignment],
    *,
    subscription_id: str,
) -> str:
    return json.dumps(
        {
            "assignments": [
                {
                    "assignment_id": assignment.assignment_id,
                    "az_role_assignment_create_argv": list(
                        assignment.create_command(subscription_id=subscription_id)
                    ),
                    "principal_id": assignment.principal_id,
                    "principal_kind": assignment.principal_kind,
                    "role_definition_id": assignment.role_definition_id,
                    "role_name": assignment.role_name,
                    "scope": assignment.scope,
                }
                for assignment in assignments
            ],
            "rerun": (
                "Rerun the same bootstrap command after an administrator creates "
                "these assignments."
            ),
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _validate_role_assignments(payload: str, *, expected: set[str]) -> None:
    try:
        assignments = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise BootstrapCommandError("Bootstrap role preflight returned invalid JSON.") from exc
    names = {
        item.get("roleDefinitionName")
        for item in assignments
        if isinstance(item, dict) and isinstance(item.get("roleDefinitionName"), str)
    } if isinstance(assignments, list) else set()
    if not names.intersection(expected):
        raise BootstrapCommandError("Required pre-granted role assignment was not found.")


def _resolve_function_principal_id(payload: str) -> str:
    try:
        identity = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise BootstrapCommandError(
            "Deployed Function App has no valid managed identity principal."
        ) from exc
    if not isinstance(identity, dict):
        raise BootstrapCommandError("Deployed Function App has no valid managed identity principal.")
    system_assigned_principal = _valid_principal_id(identity.get("principalId"))
    if system_assigned_principal is not None:
        return system_assigned_principal
    user_assigned = identity.get("userAssignedIdentities")
    if not isinstance(user_assigned, dict):
        raise BootstrapCommandError("Deployed Function App has no valid managed identity principal.")
    principals = {
        principal
        for value in user_assigned.values()
        if isinstance(value, dict)
        and (principal := _valid_principal_id(value.get("principalId"))) is not None
    }
    if len(principals) == 1:
        return principals.pop()
    raise BootstrapCommandError("Deployed Function App has no valid managed identity principal.")


def _valid_principal_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    try:
        UUID(candidate)
    except ValueError:
        return None
    return candidate


def _resolve_setup_assignee(explicit: str | None, account_payload: str) -> str:
    if explicit is not None:
        value = explicit.strip()
        if value:
            return value
        raise BootstrapCommandError("Setup principal override must not be empty.")
    try:
        account = json.loads(account_payload)
    except json.JSONDecodeError as exc:
        raise BootstrapCommandError(
            "Azure CLI account identity could not be resolved."
        ) from exc
    user = account.get("user") if isinstance(account, dict) else None
    name = user.get("name") if isinstance(user, dict) else None
    if not isinstance(name, str) or not name.strip():
        raise BootstrapCommandError(
            "Azure CLI account identity could not be resolved; "
            "pass --setup-principal-id explicitly."
        )
    return name.strip()


def _run_required(
    runner: CommandRunner,
    command: tuple[str, ...],
    *,
    cwd: Path,
    operation: str,
) -> CommandResult:
    result = runner.run(command, cwd=cwd)
    if result.returncode != 0:
        diagnostic = _azd_failure_diagnostic(command, result)
        if diagnostic is not None:
            raise BootstrapCommandError(f"Bootstrap {operation} failed: {diagnostic}")
        raise BootstrapCommandError(f"Bootstrap {operation} failed.")
    return result


def _azd_failure_diagnostic(command: Sequence[str], result: CommandResult) -> str | None:
    if not command or command[0] != "azd":
        return None
    output = _ANSI_ESCAPE_SEQUENCE.sub("", f"{result.stderr}\n{result.stdout}")
    lines = [
        line.strip()
        for line in output.splitlines()
        if any(marker in line.casefold() for marker in _AZD_ERROR_MARKERS)
    ]
    if not lines:
        return None
    diagnostic = _SENSITIVE_AZD_DIAGNOSTIC.sub("<redacted>", " ".join(lines))
    diagnostic = " ".join(diagnostic.split())
    if len(diagnostic) > _AZD_DIAGNOSTIC_MAX_LENGTH:
        return f"{diagnostic[:_AZD_DIAGNOSTIC_MAX_LENGTH].rstrip()}..."
    return diagnostic


def _parse_active_agent(payload: str, expected_name: str) -> tuple[str, str]:
    _, version, status = _parse_hosted_agent_status(payload, expected_name=expected_name)
    if status.casefold() != "active":
        raise BootstrapCommandError("Hosted agent did not reach the expected active version.")
    return expected_name, version


def _smoke_output_is_ready(output: str, *, agent_name: str) -> bool:
    expected_lines = {"READY", f"[{agent_name}] READY"}
    return any(line.strip() in expected_lines for line in output.splitlines())


def _seal_application_snapshot(
    source_root: Path,
    snapshot_root: Path,
    manifest: ApplicationContentManifest,
    *,
    expected_digest: str,
) -> None:
    for entry in manifest.entries:
        source = source_root.joinpath(*entry.path.split("/"))
        destination = snapshot_root.joinpath(*entry.path.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            destination.write_bytes(source.read_bytes())
        except OSError as exc:
            raise BootstrapCommandError("Application snapshot could not be sealed.") from exc
    observed = compute_application_content_digest(snapshot_root, manifest)
    if observed != expected_digest:
        raise BootstrapCommandError("Application changed while its snapshot was sealed.")


def _create_staging_directory(parent: Path, *, prefix: str) -> Path:
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BootstrapCommandError("Bootstrap staging directory could not be created.") from exc
    if parent.is_symlink() or not parent.is_dir():
        raise BootstrapCommandError("Bootstrap staging directory is invalid.")
    for _ in range(16):
        candidate = parent / f"{prefix}{uuid4().hex}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        except OSError as exc:
            raise BootstrapCommandError("Bootstrap staging directory could not be created.") from exc
        return candidate
    raise BootstrapCommandError("Bootstrap staging directory name could not be allocated.")


def _stage_runtime_source(artifact: FhaHostedSourceArtifact) -> None:
    source_root = _PROJECT_ROOT / "src" / "azure_functions_agents"
    destination_root = artifact.stage_root / "azure_functions_agents"
    if destination_root.exists():
        raise BootstrapCommandError("Application content conflicts with staged runtime source.")
    if any(path.is_symlink() for path in source_root.rglob("*")):
        raise BootstrapCommandError("Runtime source contains unsupported links.")
    try:
        shutil.copytree(
            source_root,
            destination_root,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
    except OSError as exc:
        raise BootstrapCommandError("Runtime source could not be staged.") from exc


def _stage_azd_workspace(source_root: Path, workspace_root: Path) -> None:
    if source_root.is_symlink() or any(path.is_symlink() for path in source_root.rglob("*")):
        raise BootstrapCommandError("Hosted source contains unsupported links.")
    if workspace_root.is_symlink() or not workspace_root.is_dir():
        raise BootstrapCommandError("Bootstrap azd workspace is invalid.")
    if any(workspace_root.iterdir()):
        raise BootstrapCommandError("Bootstrap azd workspace is not empty.")
    try:
        shutil.copytree(source_root, workspace_root, dirs_exist_ok=True)
    except OSError as exc:
        raise BootstrapCommandError("Bootstrap azd workspace could not be staged.") from exc


def _validate_existing_agent_provenance(
    runner: CommandRunner,
    *,
    project_endpoint: str,
    agent_name: str,
    provenance_tag: str,
    subscription_id: str,
    cwd: Path,
) -> None:
    result = runner.run(
        (
            "az",
            "rest",
            "--method",
            "GET",
            "--url",
            f"{project_endpoint}/agents/{agent_name}?api-version=v1",
            "--resource",
            "https://ai.azure.com",
            "--output",
            "json",
            "--subscription",
            subscription_id,
        ),
        cwd=cwd,
    )
    if result.returncode != 0:
        combined = f"{result.stdout}\n{result.stderr}".casefold()
        if "404" in combined or "notfound" in combined or "not found" in combined:
            return
        raise BootstrapCommandError("Existing hosted-agent provenance could not be verified.")
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BootstrapCommandError("Existing hosted-agent provenance was invalid.") from exc
    latest = document.get("versions", {}).get("latest", {}) if isinstance(document, dict) else {}
    metadata = latest.get("metadata") if isinstance(latest, dict) else None
    if not isinstance(metadata, dict) or metadata.get(_PROVENANCE_METADATA_KEY) != provenance_tag:
        raise BootstrapCommandError(
            "Existing hosted agent is not owned by this Function App environment."
        )


def _stamp_azd_provenance(
    workspace_root: Path,
    provenance_tag: str,
    *,
    projection: FhaRuntimeProjection,
    capture_trace_content: bool = False,
) -> None:
    manifest_path = workspace_root / "azure.yaml"
    try:
        document = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BootstrapCommandError("Generated azd project manifest was invalid.") from exc
    services = document.get("services") if isinstance(document, dict) else None
    candidates = [
        service
        for service in services.values()
        if isinstance(service, dict) and service.get("host") == "azure.ai.agent"
    ] if isinstance(services, dict) else []
    if len(candidates) != 1:
        raise BootstrapCommandError("Generated azd project must contain one hosted agent.")
    metadata = candidates[0].setdefault("metadata", {})
    if not isinstance(metadata, dict):
        raise BootstrapCommandError("Generated hosted-agent metadata was invalid.")
    existing_provenance = metadata.get(_PROVENANCE_METADATA_KEY)
    if existing_provenance not in {None, provenance_tag}:
        raise BootstrapCommandError("Generated hosted-agent provenance was invalid.")
    metadata[_PROVENANCE_METADATA_KEY] = provenance_tag
    environment_variables = candidates[0].setdefault("environmentVariables", [])
    if not isinstance(environment_variables, list) or any(
        not isinstance(item, dict) for item in environment_variables
    ):
        raise BootstrapCommandError(
            "Generated hosted-agent environment variables were invalid."
        )
    desired_variables = (
        {
            "name": "FOUNDRY_PROJECT_ENDPOINT",
            "value": projection.project_endpoint,
        },
        {
            "name": "FOUNDRY_MODEL",
            "value": projection.default_model,
        },
        {
            "name": "AZURE_AI_MODEL_DEPLOYMENT_NAME",
            "value": projection.default_model,
        },
        {
            "name": "APPLICATIONINSIGHTS_AUTH_MODE",
            "value": "entra",
        },
        {
            "name": "AZURE_EXPERIMENTAL_ENABLE_GENAI_TRACING",
            "value": "true",
        },
        {
            "name": "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
            "value": "true" if capture_trace_content else "false",
        },
        {
            "name": "OTEL_TRACES_SAMPLER",
            "value": "always_on",
        },
    )
    environment_variables[:] = desired_variables
    manifest_path.write_text(
        yaml.safe_dump(document, sort_keys=False),
        encoding="utf-8",
    )


def _arguments_from_namespace(namespace: argparse.Namespace) -> BootstrapArguments:
    return BootstrapArguments(
        application_root=Path(namespace.application_root),
        stage_root=Path(namespace.stage_root),
        subscription_id=namespace.subscription_id,
        function_app_name=namespace.function_app_name,
        function_app_slot=namespace.function_app_slot,
        resource_group=namespace.resource_group,
        setup_principal_id=namespace.setup_principal_id,
        project_endpoint=namespace.project_endpoint,
        project_resource_id=namespace.project_resource_id,
        model_deployment_name=namespace.model_deployment_name,
        runtime_pin=namespace.runtime_pin,
        agentserver_core_pin=namespace.agentserver_core_pin,
        agentserver_responses_pin=namespace.agentserver_responses_pin,
        app_insights_resource_id=namespace.app_insights_resource_id,
        rbac_mode=namespace.rbac_mode,
        capture_trace_content=namespace.capture_trace_content,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application-root", required=True)
    parser.add_argument(
        "--stage-root",
        default=str(_DEFAULT_STAGE_ROOT),
        help=f"Temporary staging parent (default: {_DEFAULT_STAGE_ROOT}).",
    )
    parser.add_argument("--subscription-id", required=True)
    parser.add_argument("--function-app-name", required=True)
    parser.add_argument("--function-app-slot")
    parser.add_argument("--resource-group", required=True)
    parser.add_argument(
        "--setup-principal-id",
        help="Optional Azure principal override; defaults to the current `az` login.",
    )
    parser.add_argument("--project-endpoint", required=True)
    parser.add_argument("--project-resource-id", required=True)
    parser.add_argument(
        "--app-insights-resource-id",
        help=(
            "Optional Application Insights component resource ID override when the "
            "Function App hidden link is missing or ambiguous."
        ),
    )
    parser.add_argument("--model-deployment-name", required=True)
    parser.add_argument(
        "--rbac-mode",
        choices=(_RBAC_MODE_AUTO, _RBAC_MODE_PLAN),
        default=_RBAC_MODE_AUTO,
        help="Apply missing hosted-observability assignments or emit an admin plan.",
    )
    parser.add_argument(
        "--capture-trace-content",
        action="store_true",
        help=(
            "Include prompt, completion, and tool content in hosted GenAI telemetry. "
            "Disabled by default."
        ),
    )
    parser.add_argument(
        "--runtime-pin",
        help="Optional exact runtime override; inferred from requirements.txt by default.",
    )
    parser.add_argument(
        "--agentserver-core-pin",
        help=f"Optional hosted server override (default: {_DEFAULT_AGENTSERVER_CORE_PIN}).",
    )
    parser.add_argument(
        "--agentserver-responses-pin",
        help=(
            "Optional hosted Responses override "
            f"(default: {_DEFAULT_AGENTSERVER_RESPONSES_PIN})."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run preflight, deploy/update the FHA, smoke-test, and publish settings.",
    )
    return parser


def main(argv: Sequence[str] | None = None, *, runner: CommandRunner | None = None) -> int:
    """Render a guarded plan or execute one deployment-only hosted bootstrap."""
    namespace = _parser().parse_args(argv)
    try:
        plan = build_bootstrap_plan(_arguments_from_namespace(namespace))
        print(
            json.dumps(
                {
                    "application_content_digest": plan.application_content_digest,
                    "wrapper_digest": plan.wrapper_digest,
                    "manifest_entries": len(plan.manifest.entries),
                    "preflight_commands": [list(command) for command in plan.preflight_commands],
                    "hosted_agent_spec": plan.hosted_agent_spec,
                },
                sort_keys=True,
            )
        )
        if namespace.execute:
            execute_bootstrap(plan, runner or SubprocessCommandRunner())
    except BootstrapRbacHandoffError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except BootstrapCommandError as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
