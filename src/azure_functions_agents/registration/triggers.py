"""Trigger registration for resolved agents."""

from __future__ import annotations

from typing import Any

import azure.functions as func

from .._logger import logger
from .._source_marker import source_marker
from ..config import ResolvedAgent
from . import _naming
from ._handlers import (
    AUTH_LEVEL_MAP,
    make_agent_handler,
    make_http_agent_handler,
    normalize_timer_schedule,
)
from ._naming import allocate_unique_function_name
from .capabilities import AgentCapabilities

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

    handler = make_agent_handler(resolved, trigger_type, capabilities)
    trigger_params["arg_name"] = "trigger_data"

    decorated = decorator_fn(**trigger_params)(handler)
    decorated = app.function_name(name=function_name)(decorated)


def _register_http_agent(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    function_name: str,
    trigger_params: dict[str, Any],
) -> None:
    route = trigger_params.get("route")
    if not route:
        raise ValueError(
            f"Agent '{resolved.name}' ({resolved.source_file}): "
            "http_trigger requires 'route' in trigger.args. "
            "See docs/front-matter-spec.md#http-trigger."
        )

    methods = trigger_params.get("methods", ["POST"])
    auth_str = str(trigger_params.get("auth_level", "function")).lower()
    if auth_str not in AUTH_LEVEL_MAP:
        valid = ", ".join(sorted(AUTH_LEVEL_MAP))
        raise ValueError(
            f"Agent '{resolved.name}' ({resolved.source_file}): "
            f"invalid auth_level '{auth_str}'. Must be one of: {valid}. "
            "See docs/front-matter-spec.md#auth_level."
        )
    auth_level = AUTH_LEVEL_MAP[auth_str]
    handler = make_http_agent_handler(resolved, capabilities)

    decorated = app.route(
        route=route,
        methods=methods,
        auth_level=auth_level,
    )(handler)
    decorated = app.function_name(name=function_name)(decorated)


def register_agent(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    registered_names: set[str] | None = None,
    function_name: str | None = None,
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
        _register_http_agent(app, resolved, capabilities, function_name, trigger_params)
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
    )
    logger.info(
        "Registered trigger: source_file=%s function=%s trigger_type=%s",
        source_marker(resolved.source_file),
        function_name,
        trigger_type,
    )
    if registered_names is not None:
        registered_names.add(function_name)
