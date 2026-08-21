"""Trigger registration for resolved agents."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import azure.functions as func

from .._logger import logger
from .._source_marker import source_marker
from ..config import EndpointAuthConfig, ResolvedAgent
from ..config.http_auth import resolve_http_trigger_auth
from ..execution.session_runtime import SessionExecutionRuntime, is_foundry_responses_runtime
from . import _naming
from ._auth import resolve_endpoint_auth_level
from ._handlers import (
    make_agent_handler,
    make_http_agent_handler,
    normalize_timer_schedule,
)
from ._naming import allocate_unique_function_name
from .capabilities import AgentCapabilities
from .catalog import AgentCatalog

if TYPE_CHECKING:
    from ..workflows.workflow_schema import WorkflowPlanPolicy

__all__ = [
    "allocate_unique_function_name",
    "register_agent",
]

_function_name_from_source = _naming._function_name_from_source

def _register_builtin_agent(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    function_name: str,
    trigger_params: dict[str, Any],
    trigger_type: str,
    catalog: AgentCatalog | None = None,
    *,
    session_runtime: SessionExecutionRuntime | None = None,
    workflows_enabled: bool = False,
    workflow_system_addendum: str | None = None,
    workflow_policy: WorkflowPlanPolicy | None = None,
) -> None:
    trigger_params = dict(trigger_params)
    decorator_fn = getattr(app, trigger_type, None)
    if decorator_fn is None and trigger_type == "connector_trigger":
        decorator_fn = getattr(app, "generic_trigger", None)
        trigger_params.setdefault("type", "connectorTrigger")

    if decorator_fn is None:
        logger.warning(
            "Skipping '%s': unknown trigger type '%s'",
            function_name,
            trigger_type,
        )
        return

    if trigger_type == "timer_trigger" and "schedule" in trigger_params:
        trigger_params["schedule"] = normalize_timer_schedule(str(trigger_params["schedule"]))

    handler = make_agent_handler(
        resolved,
        trigger_type,
        capabilities,
        catalog,
        session_runtime=session_runtime,
        workflows_enabled=workflows_enabled,
        workflow_system_addendum=workflow_system_addendum,
        workflow_policy=workflow_policy,
    )
    trigger_params["arg_name"] = "trigger_data"

    if workflows_enabled:
        handler = app.durable_client_input(client_name="client")(handler)
    decorated = decorator_fn(**trigger_params)(handler)
    decorated = app.function_name(name=function_name)(decorated)


def _resolve_http_trigger_auth(
    resolved: ResolvedAgent, trigger_params: dict[str, Any]
) -> EndpointAuthConfig:
    """Resolve custom HTTP auth and warn on deprecated flat configuration."""
    raw_auth = trigger_params.get("http_auth")
    raw_level = trigger_params.get("auth_level")

    if raw_auth is not None and raw_level is not None:
        logger.warning(
            "Agent '%s' (%s): http_trigger sets both 'http_auth' and 'auth_level'; "
            "'auth_level' is deprecated and ignored in favor of 'http_auth'. "
            "See docs/front-matter-spec.md#http-trigger.",
            resolved.name,
            source_marker(resolved.source_file),
        )
    elif raw_level is not None:
        logger.warning(
            "Agent '%s' (%s): http_trigger 'auth_level' is deprecated; use the nested "
            "'http_auth' object instead (http_auth: %s). See docs/front-matter-spec.md#http-trigger.",
            resolved.name,
            source_marker(resolved.source_file),
            str(raw_level).lower(),
        )
    try:
        return resolve_http_trigger_auth(trigger_params)
    except ValueError as exc:
        raise ValueError(
            f"Agent '{resolved.name}' ({resolved.source_file}): {exc}. "
            "See docs/front-matter-spec.md#http-trigger."
        ) from exc


def _register_http_agent(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    function_name: str,
    trigger_params: dict[str, Any],
    catalog: AgentCatalog | None = None,
    *,
    session_runtime: SessionExecutionRuntime | None = None,
    workflows_enabled: bool = False,
    workflow_system_addendum: str | None = None,
    workflow_policy: WorkflowPlanPolicy | None = None,
) -> None:
    route = trigger_params.get("route")
    if not route:
        raise ValueError(
            f"Agent '{resolved.name}' ({resolved.source_file}): "
            "http_trigger requires 'route' in trigger.args. "
            "See docs/front-matter-spec.md#http-trigger."
        )

    methods = trigger_params.get("methods", ["POST"])
    auth = _resolve_http_trigger_auth(resolved, trigger_params)
    if session_runtime is None:
        handler = make_http_agent_handler(
            resolved,
            capabilities,
            catalog,
            auth=auth,
            workflows_enabled=workflows_enabled,
            workflow_system_addendum=workflow_system_addendum,
            workflow_policy=workflow_policy,
        )
    else:
        handler = make_http_agent_handler(
            resolved,
            capabilities,
            catalog,
            auth=auth,
            session_runtime=session_runtime,
            workflows_enabled=workflows_enabled,
            workflow_system_addendum=workflow_system_addendum,
            workflow_policy=workflow_policy,
        )

    if workflows_enabled:
        handler = app.durable_client_input(client_name="client")(handler)
    decorated = app.route(
        route=route,
        methods=methods,
        auth_level=resolve_endpoint_auth_level(auth),
    )(handler)
    decorated = app.function_name(name=function_name)(decorated)


def register_agent(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    registered_names: set[str] | None = None,
    function_name: str | None = None,
    catalog: AgentCatalog | None = None,
    *,
    session_runtime: SessionExecutionRuntime | None = None,
    workflows_enabled: bool = False,
    workflow_system_addendum: str | None = None,
    workflow_policy: WorkflowPlanPolicy | None = None,
) -> None:
    """Register an agent trigger on the FunctionApp."""
    if resolved.trigger is None:
        logger.warning(
            "Skipping registration: resolved agent has no trigger (source_file=%s)",
            source_marker(resolved.source_file),
        )
        return

    trigger_type = resolved.trigger.type.strip()
    trigger_params = dict(resolved.trigger.args or {})
    if function_name is None and registered_names is None:
        function_name = _function_name_from_source(resolved.source_file, resolved.name)
    elif function_name is None:
        assert registered_names is not None
        function_name = allocate_unique_function_name(
            resolved.source_file,
            resolved.name,
            registered_names.copy(),
        )

    if trigger_type == "http_trigger":
        _register_http_agent(
            app,
            resolved,
            capabilities,
            function_name,
            trigger_params,
            catalog,
            session_runtime=session_runtime,
            workflows_enabled=workflows_enabled,
            workflow_system_addendum=workflow_system_addendum,
            workflow_policy=workflow_policy,
        )
        logger.info(
            "Registered trigger: source_file=%s function=%s trigger_type=http_trigger route=%s methods=%s",
            source_marker(resolved.source_file),
            function_name,
            trigger_params.get("route"),
            trigger_params.get("methods", ["POST"]),
        )
        if registered_names is not None:
            registered_names.add(function_name)
        return

    if (
        session_runtime is not None
        and is_foundry_responses_runtime(session_runtime)
        and trigger_type != "service_bus_queue_trigger"
    ):
        raise ValueError(
            "Foundry Hosted Agent Responses supports only service_bus_queue_trigger "
            "for non-HTTP agents."
        )

    _register_builtin_agent(
        app,
        resolved,
        capabilities,
        function_name,
        trigger_params,
        trigger_type,
        catalog,
        session_runtime=session_runtime,
        workflows_enabled=workflows_enabled,
        workflow_system_addendum=workflow_system_addendum,
        workflow_policy=workflow_policy,
    )
    logger.info(
        "Registered trigger: source_file=%s function=%s trigger_type=%s",
        source_marker(resolved.source_file),
        function_name,
        trigger_type,
    )
    if registered_names is not None:
        registered_names.add(function_name)
