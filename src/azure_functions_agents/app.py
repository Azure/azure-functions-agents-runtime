"""Azure Functions agent runtime app factory."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import azure.durable_functions as df
import azure.functions as func

from ._logger import logger
from ._observability import configure_observability
from ._source_marker import source_marker
from .config.http_auth import resolve_aca_submission_auth
from .config.paths import get_app_root, set_app_root
from .config.schema import GlobalConfig, ResolvedAgent, WorkflowConfig
from .controller.package import build_expected_manifest_binding
from .controller.readiness import (
    DEFAULT_AUTO_SUSPEND_SECONDS,
    DEFAULT_RECLAIM_IDLE_SECONDS,
    SessionRuntimeBinding,
    StateStoreBinding,
    lifecycle_policy_for_idle,
)
from .controller.reconciler import (
    ReconcilerConfig,
    SessionReconciler,
    reconciler_ncrontab,
    resolve_reconciler_cadence,
)
from .controller.sandbox_config import SandboxCreateProfile, build_sandbox_create_profile
from .egress import build_header_transform_rule, compile_mcp_headers
from .execution.backend import RunContext, RunStatus
from .execution.binding import AgentBinding
from .execution.foundry_responses_binding import resolve_foundry_responses_runtime_binding
from .execution.foundry_responses_runtime import FoundryResponsesRuntime
from .execution.run_control import RunControlError, SandboxRunControl
from .execution.session_runtime import SessionExecutionRuntime
from .foundry_responses.fha_model_catalog_gate import compile_fha_v0_project
from .foundry_responses.fha_runtime_projection import (
    FhaRuntimeProjectionError,
    parse_fha_runtime_projection,
)
from .harness.delegation import validate_delegation_graph
from .journal_paths import heartbeat_path
from .project_composition import compose_project
from .registration._handlers import build_output_validator
from .registration.endpoints import (
    register_builtin_endpoints,
    register_session_management_endpoints,
)
from .registration.triggers import register_agent
from .session_state import (
    DurableRunRecord,
    DurableSessionRecord,
    OwnerPartition,
    SessionOperationFence,
    build_store_from_service_client,
    compute_app_hash,
    get_table_service_client,
    resolve_function_app_identity,
)
from .transport.ports import SandboxSessionHandle, SandboxSessionProvider
from .transport.transport_models import (
    PersistedSandboxBinding,
    SandboxEgressHeader,
    SandboxFileNotFoundError,
    SandboxFileOperationError,
    SandboxGroupBinding,
)
from .workflows import build_workflow_integration


def _tool_name(tool: object) -> str:
    name = getattr(tool, "name", "") or ""
    return str(name)


def _serialize_capabilities_for_log(
    *,
    user_tools: list[Any] | None,
    mcp_tools: list[Any] | None,
    skill_paths: list[Path],
    skill_name_by_path: dict[str, str],
) -> dict[str, list[str]]:
    return {
        "user_tools": sorted(_tool_name(tool) for tool in (user_tools or [])),
        "mcp_servers": sorted(_tool_name(tool) for tool in (mcp_tools or [])),
        "skills": sorted(
            skill_name_by_path.get(str(path.resolve()), path.name) for path in skill_paths
        ),
    }


def _builtin_endpoints_enabled(builtin_endpoints: Any) -> bool:
    return bool(
        builtin_endpoints.debug_chat_ui or builtin_endpoints.chat_api or builtin_endpoints.mcp
    )


def _workflows_requested(workflows: WorkflowConfig | None) -> bool:
    return workflows is not None and workflows.enabled


def _sandbox_management_auth(resolved: ResolvedAgent) -> Any | None:
    """Choose the already-validated common policy for ACA management routes."""
    builtin_auth = (
        resolved.builtin_endpoints.http_auth if resolved.builtin_endpoints.chat_api else None
    )
    trigger_args = (
        resolved.trigger.args
        if resolved.trigger is not None and resolved.trigger.type == "http_trigger"
        else None
    )
    return resolve_aca_submission_auth(
        builtin_auth=builtin_auth,
        trigger_args=trigger_args,
    )


def _build_session_reconciler(
    runtime: SessionRuntimeBinding,
    state_binding: StateStoreBinding,
    provider: SandboxSessionProvider,
    *,
    cadence_seconds: int,
    max_pages: int | None = None,
    terminal_bindings: Mapping[str, AgentBinding] | None = None,
) -> SessionReconciler:
    """Compose provider-neutral reconciliation callbacks at the app boundary."""
    run_control = SandboxRunControl()

    async def with_handle[T](
        session: DurableSessionRecord,
        operation: Callable[[SandboxSessionHandle], Awaitable[T]],
    ) -> T | None:
        if session.sandbox_id is None:
            return None
        expected = build_expected_manifest_binding(
            session,
            sandbox_group_resource_id=runtime.sandbox_group_resource_id,
            state_store_fingerprint=state_binding.state_store_fingerprint,
        )
        persisted = PersistedSandboxBinding.create(
            session.sandbox_id,
            SandboxGroupBinding.create(runtime.sandbox_group_resource_id, session.region),
        )
        try:
            handle = await provider.attach(
                persisted,
                expected,
                readiness_timeout_seconds=30.0,
            )
        except Exception:
            return None
        try:
            return await operation(handle)
        finally:
            await handle.close()

    async def terminal_reader(
        session: DurableSessionRecord,
        run: DurableRunRecord,
    ) -> RunStatus | None:
        return await with_handle(
            session,
            lambda handle: run_control.get_status(
                handle,
                RunContext(session_id=run.session_id, run_id=run.run_id),
            ),
        )

    async def heartbeat_reader(
        session: DurableSessionRecord,
        run: DurableRunRecord,
    ) -> datetime | None:
        async def read_heartbeat(handle: SandboxSessionHandle) -> datetime | None:
            try:
                stat = await handle.stat_file(
                    heartbeat_path(run.run_id)
                )
            except SandboxFileNotFoundError:
                return None
            if stat.modified_at is None:
                return None
            return datetime.fromisoformat(stat.modified_at.replace("Z", "+00:00")).astimezone(UTC)

        return await with_handle(session, read_heartbeat)

    async def death_verifier(
        session: DurableSessionRecord,
        run: DurableRunRecord,
    ) -> bool | None:
        async def verify(handle: SandboxSessionHandle) -> bool | None:
            try:
                process_group_id = await run_control.read_process_group_id(
                    handle,
                    RunContext(session_id=run.session_id, run_id=run.run_id),
                )
                result = await handle.exec(
                    f"kill -0 -- -{process_group_id}",
                    timeout_seconds=5.0,
                )
            except (RunControlError, SandboxFileNotFoundError, SandboxFileOperationError):
                return None
            return result.exit_code != 0

        return await with_handle(session, verify)

    async def current_fenced_session(
        fence: SessionOperationFence,
    ) -> DurableSessionRecord | None:
        current = await state_binding.store.get_session(
            fence.owner_partition,
            fence.session_id,
        )
        operation = await state_binding.store.get_operation(
            fence.owner_partition,
            fence.session_id,
            fence.operation_id,
        )
        target = fence.target
        if (
            not fence.matches(current.record, operation.record)
            or target.sandbox_id is None
            or current.record.sandbox_id != target.sandbox_id
            or current.record.generation != target.generation
            or current.record.digest_kind != target.digest_kind
            or current.record.digest != target.digest
        ):
            return None
        return current.record

    async def apply_idle_lifecycle(fence: SessionOperationFence) -> bool:
        session = await current_fenced_session(fence)
        if session is None:
            return False

        async def apply(handle: SandboxSessionHandle) -> bool:
            if await current_fenced_session(fence) is None:
                return False
            await handle.set_lifecycle_policy(lifecycle_policy_for_idle(runtime))
            return True

        return await with_handle(session, apply) is True

    return SessionReconciler(
        store=state_binding.store,
        provider=provider,
        app_hash=compute_app_hash(runtime.app_identity),
        config=ReconcilerConfig(
            cadence_seconds=cadence_seconds,
            max_pages=max_pages or ReconcilerConfig().max_pages,
        ),
        terminal_reader=terminal_reader,
        heartbeat_reader=heartbeat_reader,
        death_verifier=death_verifier,
        idle_lifecycle_applier=apply_idle_lifecycle,
        reclaim_idle_seconds=runtime.reclaim_idle_seconds,
        terminal_bindings=terminal_bindings,
    )


def _build_session_runtime_binding(
    global_config: GlobalConfig,
    script_root: Path,
    *,
    terminal_bindings: Mapping[str, AgentBinding] | None = None,
    create_profile: SandboxCreateProfile | None = None,
) -> SessionRuntimeBinding | None:
    session_runtime = global_config.session_runtime
    if session_runtime is None or session_runtime.aca_sandbox is None:
        return None

    aca_sandbox = session_runtime.aca_sandbox
    retention = aca_sandbox.retention
    auto_suspend_seconds = (
        retention.auto_suspend_idle if retention is not None else DEFAULT_AUTO_SUSPEND_SECONDS
    )
    reclaim_idle_seconds = (
        retention.reclaim_idle if retention is not None else DEFAULT_RECLAIM_IDLE_SECONDS
    )

    async def provider_factory() -> SandboxSessionProvider:
        from .transport.aca_sdk import AcaSandboxAdapter

        return await AcaSandboxAdapter.open(aca_sandbox.sandbox_group_resource_id)

    async def state_store_factory() -> StateStoreBinding:
        service_client, fingerprint = await get_table_service_client()
        store = await build_store_from_service_client(service_client)
        await store.ensure_table()
        return StateStoreBinding.create(
            store=store,
            state_store_fingerprint=fingerprint,
        )

    runtime: SessionRuntimeBinding

    async def targeted_reconcile(partition: OwnerPartition, session_id: str) -> None:
        state_binding = await runtime.get_state_store()
        provider = await runtime.get_provider()
        reconciler = _build_session_reconciler(
            runtime,
            state_binding,
            provider,
            cadence_seconds=resolve_reconciler_cadence(),
            terminal_bindings=terminal_bindings,
        )
        await reconciler.reconcile_session(partition, session_id)

    async def bounded_reconcile() -> None:
        state_binding = await runtime.get_state_store()
        provider = await runtime.get_provider()
        reconciler = _build_session_reconciler(
            runtime,
            state_binding,
            provider,
            cadence_seconds=resolve_reconciler_cadence(),
            max_pages=1,
            terminal_bindings=terminal_bindings,
        )
        await reconciler.run_once()

    runtime = SessionRuntimeBinding.create(
        app_identity=resolve_function_app_identity(),
        sandbox_group_resource_id=aca_sandbox.sandbox_group_resource_id,
        script_root=script_root,
        provider_factory=provider_factory,
        state_store_factory=state_store_factory,
        auto_suspend_seconds=auto_suspend_seconds,
        reclaim_idle_seconds=reclaim_idle_seconds,
        targeted_reconciler=targeted_reconcile,
        post_create_reconciler=bounded_reconcile,
        capacity_reaper=bounded_reconcile,
        create_profile=create_profile,
    )
    return runtime


def _build_sandbox_create_profile(
    global_config: GlobalConfig,
    resolved_agents: list[ResolvedAgent],
    mcp_result: Any,
) -> SandboxCreateProfile | None:
    session_runtime = global_config.session_runtime
    if session_runtime is None or session_runtime.aca_sandbox is None:
        return None
    web_request_configs = [
        resolved.web_request_config
        for resolved in resolved_agents
        if resolved.web_request_config is not None
    ]
    if not web_request_configs:
        allowed_hosts: tuple[str, ...] | None = ()
    elif any(config.allowed_hosts is None for config in web_request_configs):
        allowed_hosts = None
    else:
        allowed_hosts = tuple(
            sorted(
                {
                    host
                    for config in web_request_configs
                    for host in config.allowed_hosts or []
                }
            )
        )
    reachable_mcp_names = {
        name
        for resolved in resolved_agents
        for name in resolved.enabled_mcp_names
    }
    reachable_mcp_definitions = tuple(
        definition
        for name, definition in mcp_result.definitions.items()
        if name in reachable_mcp_names
    )
    egress_rules = tuple(
        build_header_transform_rule(
            name=f"mcp-{definition.name}-headers",
            url=definition.url,
            headers=headers,
        )
        for definition in reachable_mcp_definitions
        if (
            headers := _compile_sandbox_mcp_headers(
                definition.name,
                definition.headers,
                definition.auth,
            )
        )
    )
    return build_sandbox_create_profile(
        web_request_allowed_hosts=allowed_hosts,
        mcp_urls=tuple(
            definition.url for definition in reachable_mcp_definitions
        ),
        model_endpoint=None,
        telemetry_endpoint=None,
        egress_rules=egress_rules,
    )


def _compile_sandbox_mcp_headers(
    name: str,
    headers: Mapping[str, object],
    auth: Mapping[str, object],
) -> tuple[SandboxEgressHeader, ...]:
    compiled = compile_mcp_headers(headers)
    scope = auth.get("scope")
    if not isinstance(scope, str) or not scope.strip():
        return compiled
    filtered = tuple(header for header in compiled if header.name.casefold() != "authorization")
    if len(filtered) != len(compiled):
        logger.warning(
            "MCP server '%s' declares native auth; omitting its static Authorization "
            "egress transform so the native managed-identity token wins.",
            name,
        )
    return filtered


def create_function_app(app_root: Path | None = None) -> func.FunctionApp:
    """Build and return a fully-configured Azure Functions app.

    Two-pass composition: resolve, validate, and freeze every agent into a
    read-only ``AgentCatalog`` (pass 1) before registering any trigger or
    endpoint (pass 2), so `subagents:` references always see the full,
    already-validated app. See FRD 0007 §4.2 for the full pipeline stages.
    """
    if app_root is not None:
        set_app_root(app_root)
    resolved_root = get_app_root()

    # Bootstrap observability before anything runs so MAF gen_ai spans + runtime spans/metrics
    # flow to Application Insights with zero app code. No-op unless a telemetry provider is active.
    configure_observability()

    # Resolve the binding before ordinary composition so the FHA compiler can reject raw
    # substitutions before the normal loader is allowed to resolve them.
    fha_binding = resolve_foundry_responses_runtime_binding(aca_sandbox_configured=False)
    fha_compilation = None
    if fha_binding is not None:
        runtime_projection = fha_binding.application_content_manifest.runtime_projection
        if runtime_projection is None:
            raise FhaRuntimeProjectionError("FHA runtime projection is unavailable.")
        expected_projection = parse_fha_runtime_projection(runtime_projection)
        if expected_projection.project_endpoint != fha_binding.project_endpoint:
            raise FhaRuntimeProjectionError(
                "FHA runtime projection project endpoint does not match the binding."
            )
        fha_compilation = compile_fha_v0_project(
            resolved_root,
            project_endpoint=expected_projection.project_endpoint,
            default_model=expected_projection.default_model,
            expected_projection=expected_projection,
        )
        composition = fha_compilation.composition
    else:
        composition = compose_project(resolved_root)

    global_config = composition.global_config
    agent_specs = composition.agent_specs
    tool_result = composition.tool_result
    mcp_result = composition.mcp_result
    skill_result = composition.skill_result
    user_tools = tool_result.user_tools
    mcp_tools = mcp_result.servers
    skills = skill_result.skills
    skill_names = list(skills)
    mcp_names = list(mcp_tools)
    skill_name_by_path = {str(path.resolve()): name for name, path in skills.items()}
    discovered_user_tool_names = sorted(_tool_name(tool) for tool in user_tools)

    logger.info(
        "discovery_summary: mcp_servers=%s skills=%s user_tools=%s",
        sorted(mcp_names),
        sorted(skill_names),
        discovered_user_tool_names,
    )

    resolved_agents = list(composition.resolved_agents)
    if (
        global_config.session_runtime is not None
        and global_config.session_runtime.aca_sandbox is not None
    ):
        validate_delegation_graph(resolved_agents)

    workflows_requested = any(
        resolved.is_main and _workflows_requested(resolved.workflows)
        for resolved in resolved_agents
    )

    # Collect indexing summary for structured logging
    agents_summary: list[dict[str, Any]] = []
    system_tools_used: set[str] = set()

    # Track global system tools configuration
    if (
        global_config.system_tools
        and global_config.system_tools.dynamic_sessions_code_interpreter
    ):
        system_tools_used.add("dynamic_sessions_code_interpreter")
    if not (global_config.system_tools and global_config.system_tools.web_request is False):
        system_tools_used.add("web_request")

    catalog = (
        fha_compilation.catalog if fha_compilation is not None else composition.catalog
    )
    terminal_bindings = {
        resolved.slug: AgentBinding(
            agent_name=resolved.slug,
            output_validator=build_output_validator(resolved),
        )
        for resolved in resolved_agents
    }
    create_profile = _build_sandbox_create_profile(
        global_config,
        resolved_agents,
        mcp_result,
    )
    session_runtime = _build_session_runtime_binding(
        global_config,
        resolved_root,
        terminal_bindings=terminal_bindings,
        create_profile=create_profile,
    )
    fha_runtime: FoundryResponsesRuntime | None = None
    if fha_binding is not None:
        if (
            global_config.session_runtime is not None
            and global_config.session_runtime.aca_sandbox is not None
        ):
            resolve_foundry_responses_runtime_binding(aca_sandbox_configured=True)
        assert fha_compilation is not None
        fha_binding.validate_application_content(resolved_root)
        app_identity = resolve_function_app_identity()
        fha_binding.validate_fingerprint(app_identity)
        fha_binding.validate_runtime_projection(fha_compilation.projection)
        fha_runtime = FoundryResponsesRuntime.create(
            binding=fha_binding,
            app_identity=app_identity,
        )
    execution_runtime: SessionExecutionRuntime | None = session_runtime or fha_runtime

    app: func.FunctionApp = (
        df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)
        if workflows_requested
        else func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
    )

    # --- Two-pass composition, pass 2 (FRD 0007 §4.2): mutate `app` --------------------
    for resolved in resolved_agents:
        capabilities = catalog[resolved.slug].capabilities
        management_auth = (
            _sandbox_management_auth(resolved) if execution_runtime is not None else None
        )

        workflows_enabled = False
        workflow_system_addendum: str | None = None
        trigger_workflow_system_addendum: str | None = None
        workflow_policy = None
        if resolved.is_main:
            workflow_integration = build_workflow_integration(
                app,
                resolved.metadata,
                workflow_tools=capabilities.filtered_workflow_tools,
                workflow_subagents=(
                    resolved.workflows.subagents if resolved.workflows is not None else ()
                ),
                catalog=catalog,
            )
            workflows_enabled = workflow_integration.enabled
            workflow_system_addendum = workflow_integration.chat_system_addendum
            trigger_workflow_system_addendum = (
                workflow_integration.trigger_system_addendum
            )
            workflow_policy = workflow_integration.plan_policy
        elif _workflows_requested(resolved.workflows):
            logger.warning(
                "workflows.enabled is only honored on main.agent.md; ignoring "
                "workflows for agent %s",
                resolved.name,
            )

        capability_names = _serialize_capabilities_for_log(
            user_tools=capabilities.filtered_user_tools,
            mcp_tools=capabilities.filtered_mcp_tools,
            skill_paths=capabilities.enabled_skill_paths,
            skill_name_by_path=skill_name_by_path,
        )
        logger.info(
            "agent_capabilities_registered: source_file=%s user_tools=%s mcp_servers=%s skills=%s",
            source_marker(resolved.source_file),
            capability_names["user_tools"],
            capability_names["mcp_servers"],
            capability_names["skills"],
        )
        # The identity slug (pass 1a) is already guaranteed globally unique, so it is
        # used directly as the registered function name / built-in endpoint slug —
        # no allocator or de-duplication pass is needed here anymore.
        if resolved.trigger is not None:
            if execution_runtime is None:
                register_agent(
                    app,
                    resolved,
                    capabilities,
                    function_name=resolved.slug,
                    catalog=catalog,
                    workflows_enabled=workflows_enabled,
                    workflow_system_addendum=trigger_workflow_system_addendum,
                    workflow_policy=workflow_policy,
                )
            else:
                register_agent(
                    app,
                    resolved,
                    capabilities,
                    function_name=resolved.slug,
                    catalog=catalog,
                    session_runtime=execution_runtime,
                    workflows_enabled=workflows_enabled,
                    workflow_system_addendum=trigger_workflow_system_addendum,
                    workflow_policy=workflow_policy,
                )
        if _builtin_endpoints_enabled(resolved.builtin_endpoints):
            if execution_runtime is None:
                register_builtin_endpoints(
                    app,
                    resolved,
                    capabilities,
                    slug=resolved.slug,
                    workflows_enabled=workflows_enabled,
                    workflow_system_addendum=workflow_system_addendum,
                    workflow_policy=workflow_policy,
                    catalog=catalog,
                )
            else:
                register_builtin_endpoints(
                    app,
                    resolved,
                    capabilities,
                    slug=resolved.slug,
                    workflows_enabled=workflows_enabled,
                    workflow_system_addendum=workflow_system_addendum,
                    workflow_policy=workflow_policy,
                    catalog=catalog,
                    session_runtime=execution_runtime,
                )
        if execution_runtime is not None and management_auth is not None:
            register_session_management_endpoints(
                app,
                slug=resolved.slug,
                auth=management_auth,
                session_runtime=execution_runtime,
                binding=terminal_bindings[resolved.slug],
            )

        # Collect agent summary info
        agent_info: dict[str, Any] = {
            "source_file": source_marker(resolved.source_file),
            "registered_capabilities": capability_names,
        }
        if resolved.trigger:
            agent_info["trigger_type"] = resolved.trigger.type
        else:
            agent_info["trigger_type"] = None
        if _builtin_endpoints_enabled(resolved.builtin_endpoints):
            endpoints = []
            if resolved.builtin_endpoints.debug_chat_ui:
                endpoints.append("debug_chat_ui")
            if resolved.builtin_endpoints.chat_api:
                endpoints.append("chat_api")
            if resolved.builtin_endpoints.mcp:
                endpoints.append("mcp")
            agent_info["builtin_endpoints"] = endpoints
        if workflows_enabled:
            agent_info["workflows"] = "enabled"

        # Track per-agent system tools (if not opted out)
        if resolved.sandbox_config:
            system_tools_used.add("dynamic_sessions_code_interpreter")
        if resolved.web_request_config:
            system_tools_used.add("web_request")

        agents_summary.append(agent_info)

    if session_runtime is not None:
        cadence = resolve_reconciler_cadence()

        async def reconcile_sandbox_sessions(_timer: Any) -> None:
            state_binding = await session_runtime.get_state_store()
            provider = await session_runtime.get_provider()
            reconciler = _build_session_reconciler(
                session_runtime,
                state_binding,
                provider,
                cadence_seconds=cadence,
                terminal_bindings=terminal_bindings,
            )
            await reconciler.run_once()

        reconciler_function = app.timer_trigger(
            schedule=reconciler_ncrontab(cadence),
            arg_name="timer",
        )(reconcile_sandbox_sessions)
        app.function_name(name="azure_functions_agents_reconciler")(reconciler_function)

    # Emit structured indexing summary log
    indexing_summary = {
        "event": "agent_runtime_indexed",
        "agent_count": len(agent_specs),
        "agents": agents_summary,
        "system_tools": list(system_tools_used),
        "discovered_capabilities": {
            "mcp_servers": len(mcp_names),
            "skills": len(skill_names),
            "user_tools": len(user_tools),
        },
        "discovered_capability_names": {
            "mcp_servers": sorted(mcp_names),
            "skills": sorted(skill_names),
            "user_tools": discovered_user_tool_names,
        },
        "failed_loads": {
            "mcp_servers": [f"{name}: {error}" for name, error in mcp_result.failed_loads],
            "skills": [f"{path}: {error}" for path, error in skill_result.failed_loads],
            "user_tools": [f"{file}: {error}" for file, error in tool_result.failed_loads],
        },
    }
    logger.info(
        "Agent runtime indexing completed: %s",
        json.dumps(indexing_summary, ensure_ascii=False, default=str),
    )

    return app
