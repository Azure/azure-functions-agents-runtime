"""Azure Functions agent runtime app factory."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import azure.durable_functions as df
import azure.functions as func

from ._logger import logger
from ._observability import configure_observability, emit_runtime_event
from ._source_marker import source_marker
from .config.http_auth import resolve_aca_submission_auth
from .config.loader import load_agent_specs, load_global_config
from .config.merge import compose
from .config.paths import get_app_root, set_app_root
from .config.schema import GlobalConfig, ResolvedAgent, WorkflowConfig
from .config.validation import (
    validate_resolved_agent,
    validate_session_runtime,
    validate_subagent_references,
    validate_workflow_subagent_references,
)
from .controller.package import build_expected_manifest_binding
from .controller.readiness import (
    DEFAULT_AUTO_SUSPEND_SECONDS,
    DEFAULT_RECLAIM_IDLE_SECONDS,
    SessionRuntimeBinding,
    SetupDeadline,
    StateStoreBinding,
    lifecycle_policy_for_idle,
)
from .controller.reconciler import (
    ReconcilerConfig,
    ReconcileReport,
    SessionReconciler,
    reconciler_ncrontab,
    resolve_reconciler_cadence,
)
from .controller.sandbox_config import SandboxCreateProfile, build_sandbox_create_profile
from .discovery.mcp import discover_mcp_servers
from .discovery.skills import discover_skills
from .discovery.tools import discover_project_tools
from .egress import build_header_transform_rule, compile_mcp_headers
from .execution.backend import RunContext, RunStatus
from .execution.binding import AgentBinding
from .execution.run_control import RunControlError, SandboxRunControl
from .harness.delegation import validate_delegation_graph
from .journal_paths import heartbeat_path
from .registration._handlers import build_output_validator
from .registration.capabilities import build_capabilities, validate_subagent_tool_names
from .registration.catalog import AgentCatalog, CatalogEntry, build_catalog
from .registration.endpoints import (
    register_builtin_endpoints,
    register_sandbox_management_endpoints,
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

DEFAULT_RECONCILER_TIMER_PASS_TIMEOUT_SECONDS = 240.0


class ReconcilerTimerPassDeadlineExceededError(TimeoutError):
    """The app-level deadline elapsed while a deployed timer pass was running."""


class _ReconcilerTimerPassDeadline:
    """One app-level deadline for a complete deployed timer reconciliation pass."""

    def __init__(self, *, deadline: float, clock: Callable[[], float]) -> None:
        self._deadline = deadline
        self._clock = clock

    @classmethod
    def start(
        cls,
        *,
        timeout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> _ReconcilerTimerPassDeadline:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timer reconciliation pass timeout must be positive and finite")
        return cls(deadline=clock() + timeout_seconds, clock=clock)

    def remaining_seconds(self) -> float:
        """Return the remaining pass time or fail before starting more I/O."""
        remaining = self._deadline - self._clock()
        if remaining <= 0:
            raise ReconcilerTimerPassDeadlineExceededError(
                "Timer reconciliation pass deadline exceeded."
            )
        return remaining

    async def wait_for[T](self, operation: Awaitable[T]) -> T:
        """Await the pass and let cancellation reach every awaited SDK operation."""
        try:
            remaining = self.remaining_seconds()
        except ReconcilerTimerPassDeadlineExceededError:
            if inspect.iscoroutine(operation):
                operation.close()
            raise

        timeout = asyncio.timeout(remaining)
        try:
            async with timeout:
                result = await operation
        except TimeoutError:
            if timeout.expired():
                raise ReconcilerTimerPassDeadlineExceededError(
                    "Timer reconciliation pass deadline exceeded."
                ) from None
            raise
        if timeout.expired():
            raise ReconcilerTimerPassDeadlineExceededError(
                "Timer reconciliation pass deadline exceeded."
            )
        return result


async def _run_deployed_reconciler_timer_pass(
    session_runtime: SessionRuntimeBinding,
    *,
    cadence_seconds: int,
    terminal_bindings: Mapping[str, AgentBinding],
    timeout_seconds: float = DEFAULT_RECONCILER_TIMER_PASS_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> ReconcileReport:
    """Run the Azure-aware timer pass within its independent application deadline."""
    deadline = _ReconcilerTimerPassDeadline.start(
        timeout_seconds=timeout_seconds,
        clock=clock,
    )

    async def reconcile() -> ReconcileReport:
        state_binding = await session_runtime.get_state_store()
        provider = await session_runtime.get_provider()
        reconciler = _build_session_reconciler(
            session_runtime,
            state_binding,
            provider,
            cadence_seconds=cadence_seconds,
            terminal_bindings=terminal_bindings,
        )
        return await reconciler.run_once()

    try:
        report = await deadline.wait_for(reconcile())
    except ReconcilerTimerPassDeadlineExceededError:
        logger.error(
            "Sandbox session reconciliation timer pass exceeded its %.0f-second application "
            "deadline; the invocation stopped and the next timer cadence will retry.",
            timeout_seconds,
        )
        raise
    logger.info(
        "%s",
        json.dumps(
            {
                "abandoned_runs": report.abandoned_runs,
                "adopted_terminal_runs": report.adopted_terminal_runs,
                "cadence_seconds": cadence_seconds,
                "deleted_sandboxes": report.deleted_sandboxes,
                "deleted_snapshots": report.deleted_snapshots,
                "event_name": "sandbox_reconciliation_completed",
                "evicted_results": report.evicted_results,
                "tombstoned_sessions": report.tombstoned_sessions,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )
    emit_runtime_event(
        "af.sandbox.reconciliation.completed",
        {
            "af.sandbox.cadence_seconds": cadence_seconds,
            "af.sandbox.deleted_sandboxes": report.deleted_sandboxes,
            "af.sandbox.deleted_snapshots": report.deleted_snapshots,
            "af.sandbox.evicted_results": report.evicted_results,
            "af.sandbox.tombstoned_sessions": report.tombstoned_sessions,
        },
    )
    return report


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
    from .transport.aca_sdk import validate_aca_sandbox_dependency

    validate_aca_sandbox_dependency()

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

    async def targeted_reconcile(
        partition: OwnerPartition,
        session_id: str,
        setup_deadline: SetupDeadline,
    ) -> None:
        state_binding = await runtime.get_state_store()
        provider = await runtime.get_provider()
        reconciler = _build_session_reconciler(
            runtime,
            state_binding,
            provider,
            cadence_seconds=resolve_reconciler_cadence(),
            terminal_bindings=terminal_bindings,
        )
        await reconciler.reconcile_session(partition, session_id, setup_deadline)

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


def _fail_on_duplicate_slugs(resolved_agents: list[ResolvedAgent]) -> set[str]:
    """Fail fast on colliding agent identity slugs and return the known-slug set.

    A slug (sanitized file stem) doubles as the function name, the
    ``/agents/<slug>/`` route, and the ``delegate_<slug>`` tool name, so a
    collision is a hard startup error, not the old silent auto-suffix behavior.
    Must run first (two-pass composition, pass 1) so
    ``known_slugs`` can be handed to ``validate_subagent_references``.
    """
    sources_by_slug: dict[str, list[str]] = {}
    for resolved in resolved_agents:
        sources_by_slug.setdefault(resolved.slug, []).append(source_marker(resolved.source_file))

    for slug, sources in sorted(sources_by_slug.items()):
        if len(sources) > 1:
            listed = ", ".join(sorted(sources))
            raise ValueError(
                f"Duplicate agent slug {slug!r} is used by {len(sources)} source "
                f"files: {listed}. Agent identity slugs must be globally unique "
                "across the app (a slug doubles as the registered function "
                "name, the `/agents/<slug>/` built-in endpoint route, and the "
                "`delegate_<slug>` tool name). Rename one of the colliding "
                "source files (e.g. its file stem) to resolve this. See "
                "docs/front-matter-spec.md#subagents."
            )

    return set(sources_by_slug)


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

    global_config = load_global_config(resolved_root)

    # Bootstrap observability before anything runs so MAF gen_ai spans + runtime spans/metrics
    # flow to Application Insights with zero app code. No-op unless a telemetry provider is active.
    configure_observability()

    agent_specs = load_agent_specs(resolved_root)
    tool_result = discover_project_tools(resolved_root)
    mcp_result = discover_mcp_servers(resolved_root)
    skill_result = discover_skills(resolved_root)

    user_tools = tool_result.user_tools
    workflow_tools = tool_result.workflow_tools
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

    resolved_agents = [
        compose(
            spec,
            global_config,
            discovered_mcp_names=mcp_names,
            discovered_skill_names=skill_names,
        )
        for spec in agent_specs
    ]

    # FRD 0008: enforce the `session_runtime` startup validation matrix before
    # any other cross-agent validation/registration runs. A no-op (returns
    # immediately) unless `session_runtime` is configured at all; configuring
    # `aca_sandbox` always fails here (capability gate), never at request time.
    validate_session_runtime(global_config, resolved_agents)

    # --- Two-pass composition, pass 1a: app-wide identity index (FRD 0007 §4.2). ---
    # Must run before any other cross-agent validation: `validate_subagent_references`
    # needs a collision-free slug set, and nothing below may assume slugs are unique
    # until this has actually verified it.
    known_slugs = _fail_on_duplicate_slugs(resolved_agents)

    referenced_slugs: set[str] = set()
    for resolved in resolved_agents:
        validate_subagent_references(resolved, known_slugs=known_slugs)
        validate_workflow_subagent_references(resolved, known_slugs=known_slugs)
        referenced_slugs.update(ref.agent for ref in resolved.subagents)
        if resolved.workflows is not None:
            referenced_slugs.update(ref.agent for ref in resolved.workflows.subagents)
    if (
        global_config.session_runtime is not None
        and global_config.session_runtime.aca_sandbox is not None
    ):
        validate_delegation_graph(resolved_agents)

    workflows_requested = any(
        resolved.is_main and _workflows_requested(resolved.workflows)
        for resolved in resolved_agents
    )
    app: func.FunctionApp = (
        df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)
        if workflows_requested
        else func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
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

    # --- Two-pass composition, pass 1b (FRD 0007 §4.2): validate + build capabilities ---
    # for every agent and freeze the result into a read-only AgentCatalog. Nothing here
    # touches `app` — a coordinator's `delegate_<slug>` tools must be able to resolve
    # *any* specialist by slug at request time, including ones registered later in
    # `resolved_agents` than the coordinator itself, so the full catalog has to exist
    # before pass 2 (FunctionApp registration) begins.
    catalog_entries: dict[str, CatalogEntry] = {}
    for resolved in resolved_agents:
        # Validation is owned by the app factory; compose() stays a pure translation step.
        validate_resolved_agent(
            resolved,
            discovered_mcp_names=mcp_names,
            discovered_skills=skill_names,
            is_referenced_as_subagent=resolved.slug in referenced_slugs,
        )
        capabilities = build_capabilities(
            resolved,
            discovered_user_tools=user_tools,
            discovered_workflow_tools=workflow_tools,
            discovered_mcp_tools=mcp_tools,
            discovered_skills=skills,
        )
        validate_subagent_tool_names(resolved, capabilities)
        catalog_entries[resolved.slug] = CatalogEntry(resolved, capabilities)

    catalog: AgentCatalog = build_catalog(catalog_entries)
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

    # --- Two-pass composition, pass 2 (FRD 0007 §4.2): mutate `app` --------------------
    for resolved in resolved_agents:
        capabilities = catalog[resolved.slug].capabilities
        management_auth = (
            _sandbox_management_auth(resolved) if session_runtime is not None else None
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
            if session_runtime is None:
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
                    session_runtime=session_runtime,
                    workflows_enabled=workflows_enabled,
                    workflow_system_addendum=trigger_workflow_system_addendum,
                    workflow_policy=workflow_policy,
                )
        if _builtin_endpoints_enabled(resolved.builtin_endpoints):
            if session_runtime is None:
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
                    session_runtime=session_runtime,
                )
        if session_runtime is not None and management_auth is not None:
            register_sandbox_management_endpoints(
                app,
                slug=resolved.slug,
                auth=management_auth,
                session_runtime=session_runtime,
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

        async def reconcile_sandbox_sessions(timer: func.TimerRequest) -> None:
            del timer
            await _run_deployed_reconciler_timer_pass(
                session_runtime,
                cadence_seconds=cadence,
                terminal_bindings=terminal_bindings,
            )

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
