"""Trigger registration for resolved agents."""

from __future__ import annotations

from typing import Any

import azure.functions as func
from pydantic import ValidationError

from .._logger import logger
from .._source_marker import source_marker
from ..config import EndpointAuthConfig, ResolvedAgent
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

__all__ = [
    "allocate_unique_function_name",
    "register_agent",
]

_function_name_from_source = _naming._function_name_from_source

# Legacy flat ``auth_level`` values accepted on ``http_trigger``. These map 1:1 to
# the ``function``/``admin``/``anonymous`` auth modes; the flat field never
# supported ``entra`` (identity enforcement is only expressible via ``http_auth``).
_LEGACY_AUTH_LEVELS = frozenset({"anonymous", "function", "admin"})


def _register_builtin_agent(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    function_name: str,
    trigger_params: dict[str, Any],
    trigger_type: str,
    catalog: AgentCatalog | None = None,
    *,
    workflows_enabled: bool = False,
    workflow_system_addendum: str | None = None,
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
        workflows_enabled=workflows_enabled,
        workflow_system_addendum=workflow_system_addendum,
    )
    trigger_params["arg_name"] = "trigger_data"

    if workflows_enabled:
        handler = app.durable_client_input(client_name="client")(handler)
    decorated = decorator_fn(**trigger_params)(handler)
    decorated = app.function_name(name=function_name)(decorated)


def _resolve_http_trigger_auth(
    resolved: ResolvedAgent, trigger_params: dict[str, Any]
) -> EndpointAuthConfig:
    """Resolve an ``http_trigger``'s auth policy into the shared ``EndpointAuthConfig``.

    Accepts the nested ``http_auth`` object (preferred — the same model built-in
    endpoints use, supporting ``function``/``admin``/``anonymous``/``entra`` and
    the string shorthand) and the legacy flat ``auth_level`` string (deprecated).
    When both are present ``http_auth`` wins and ``auth_level`` is ignored with a
    warning. When neither is present the default (``function``) is used.
    """
    raw_auth = trigger_params.get("http_auth")
    raw_level = trigger_params.get("auth_level")

    if raw_auth is not None:
        if raw_level is not None:
            logger.warning(
                "Agent '%s' (%s): http_trigger sets both 'http_auth' and 'auth_level'; "
                "'auth_level' is deprecated and ignored in favor of 'http_auth'. "
                "See docs/front-matter-spec.md#http-trigger.",
                resolved.name,
                source_marker(resolved.source_file),
            )
        try:
            return EndpointAuthConfig.model_validate(raw_auth)
        except ValidationError as exc:
            detail = exc.errors()[0].get("msg", "invalid value") if exc.errors() else "invalid value"
            raise ValueError(
                f"Agent '{resolved.name}' ({resolved.source_file}): "
                f"invalid http_trigger 'http_auth': {detail}. "
                "See docs/front-matter-spec.md#http-trigger."
            ) from exc

    if raw_level is not None:
        logger.warning(
            "Agent '%s' (%s): http_trigger 'auth_level' is deprecated; use the nested "
            "'http_auth' object instead (http_auth: %s). See docs/front-matter-spec.md#http-trigger.",
            resolved.name,
            source_marker(resolved.source_file),
            str(raw_level).lower(),
        )
        level_str = str(raw_level).lower()
        if level_str not in _LEGACY_AUTH_LEVELS:
            valid = ", ".join(sorted(_LEGACY_AUTH_LEVELS))
            raise ValueError(
                f"Agent '{resolved.name}' ({resolved.source_file}): "
                f"invalid auth_level '{level_str}'. Must be one of: {valid}. "
                "See docs/front-matter-spec.md#auth_level."
            )
        return EndpointAuthConfig.model_validate({"mode": level_str})

    return EndpointAuthConfig()


def _register_http_agent(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    function_name: str,
    trigger_params: dict[str, Any],
    catalog: AgentCatalog | None = None,
    *,
    workflows_enabled: bool = False,
    workflow_system_addendum: str | None = None,
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
    handler = make_http_agent_handler(
        resolved,
        capabilities,
        catalog,
        auth=auth,
        workflows_enabled=workflows_enabled,
        workflow_system_addendum=workflow_system_addendum,
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
    workflows_enabled: bool = False,
    workflow_system_addendum: str | None = None,
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
            workflows_enabled=workflows_enabled,
            workflow_system_addendum=workflow_system_addendum,
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

    _register_builtin_agent(
        app,
        resolved,
        capabilities,
        function_name,
        trigger_params,
        trigger_type,
        catalog,
        workflows_enabled=workflows_enabled,
        workflow_system_addendum=workflow_system_addendum,
    )
    logger.info(
        "Registered trigger: source_file=%s function=%s trigger_type=%s",
        source_marker(resolved.source_file),
        function_name,
        trigger_type,
    )
    if registered_names is not None:
        registered_names.add(function_name)
