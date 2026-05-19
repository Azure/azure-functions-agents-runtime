"""Trigger registration for resolved non-main agents."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import azure.functions as func

from .._logger import logger
from ..config import ResolvedAgent
from ..system_tools.connectors.cache import configure_connector_tools
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

_CONNECTORS_INSTANCES: dict[int, object] = {}
_function_name_from_source = _naming._function_name_from_source


def _dump_connector_specs(resolved: ResolvedAgent) -> list[dict[str, Any]]:
    return [spec.model_dump() for spec in resolved.connector_specs]


def _configure_connector_tools_if_needed(
    resolved: ResolvedAgent, capabilities: AgentCapabilities
) -> None:
    if not capabilities.use_connector_tools or not resolved.connector_specs:
        return
    configure_connector_tools(_dump_connector_specs(resolved))


def _register_builtin_agent(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    function_name: str,
    trigger_params: dict[str, Any],
    trigger_type: str,
) -> None:
    decorator_fn = getattr(app, trigger_type, None)
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
    trigger_params = dict(trigger_params)
    trigger_params["arg_name"] = "trigger_data"

    decorated = decorator_fn(**trigger_params)(handler)
    decorated = app.function_name(name=function_name)(decorated)
    logger.info(
        "Registered '%s' (%s) — %s",
        function_name,
        trigger_type,
        resolved.name,
    )


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
    logger.info(
        "Registered HTTP agent '%s' at /%s (%s) — %s",
        function_name,
        route,
        methods,
        resolved.name,
    )


def _register_connector_agent(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
    function_name: str,
    trigger_params: dict[str, Any],
    trigger_type: str,
) -> None:
    app_key = id(app)
    connectors_instance = _CONNECTORS_INSTANCES.get(app_key)

    if connectors_instance is None:
        try:
            import azure.functions_connectors as fc

            connectors_instance = fc.FunctionsConnectors(app)
            _CONNECTORS_INSTANCES[app_key] = connectors_instance
        except ImportError:
            logger.error(
                "Skipping '%s': azure-functions-connectors package not installed. "
                "Install from: https://github.com/anthonychu/azure-functions-connectors-python",
                function_name,
            )
            return

    parts = trigger_type.removeprefix("connectors.").split(".")
    obj: Any = connectors_instance
    try:
        for part in parts:
            obj = getattr(obj, part)
    except AttributeError:
        logger.warning(
            "Skipping '%s': could not resolve connector trigger '%s'",
            function_name,
            trigger_type,
        )
        return

    if not callable(obj):
        logger.warning(
            "Skipping '%s': resolved connector trigger '%s' is not callable",
            function_name,
            trigger_type,
        )
        return

    decorator_fn = cast(Callable[..., Callable[[Any], Any]], obj)

    handler = make_agent_handler(resolved, trigger_type, capabilities)

    decorated = decorator_fn(**trigger_params)(handler)
    decorated = app.function_name(name=function_name)(decorated)
    logger.info(
        "Registered '%s' (%s) — %s",
        function_name,
        trigger_type,
        resolved.name,
    )


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
            "Skipping '%s': resolved agent has no trigger",
            resolved.name,
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

    _configure_connector_tools_if_needed(resolved, capabilities)

    if resolved.is_main and trigger_type == "http_trigger":
        logger.debug(
            "Skipping http_trigger registration for main agent '%s'; "
            "HTTP routes are provided by the debug endpoints.",
            resolved.name,
        )
        return

    if trigger_type == "http_trigger":
        _register_http_agent(app, resolved, capabilities, function_name, trigger_params)
        if registered_names is not None:
            registered_names.add(function_name)
        return

    if "." in trigger_type:
        _register_connector_agent(
            app,
            resolved,
            capabilities,
            function_name,
            trigger_params,
            trigger_type,
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
    if registered_names is not None:
        registered_names.add(function_name)
