"""Trigger registration for resolved non-main agents."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import azure.functions as func

from .._logger import logger
from ..config import ResolvedAgent, resolve_env_var
from ..system_tools.connectors.cache import configure_connector_tools
from ._handlers import (
    AUTH_LEVEL_MAP,
    make_agent_handler,
    make_http_agent_handler,
    normalize_timer_schedule,
)
from .capabilities import AgentCapabilities

_CONNECTORS_INSTANCES: dict[int, object] = {}


def _dump_connector_specs(resolved: ResolvedAgent) -> list[dict[str, Any]]:
    return [spec.model_dump() for spec in resolved.connector_specs]


def _safe_function_name(raw_name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_]", "_", raw_name).strip("_")
    if not name:
        return "agent_function"
    if name[0].isdigit():
        return f"fn_{name}"
    return name


def _function_name_from_source(source_file: str | Path | None, fallback_name: str) -> str:
    source_value = str(source_file).strip() if source_file is not None else ""
    if not source_value:
        logger.warning(
            "Resolved agent '%s' is missing source_file; falling back to sanitized display name for function registration.",
            fallback_name,
        )
        return _safe_function_name(fallback_name)

    source_name = Path(source_value).name
    base_name = source_name.removesuffix(".agent.md")
    if base_name == source_name:
        base_name = Path(source_name).stem
    return _safe_function_name(base_name)


def _resolve_trigger_params(trigger_params: dict[str, Any]) -> dict[str, Any]:
    """Resolve env vars on all string values in trigger params."""
    resolved = {}
    for key, value in trigger_params.items():
        if isinstance(value, str):
            resolved[key] = resolve_env_var(value)
        else:
            resolved[key] = value
    return resolved


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

    try:
        decorated = decorator_fn(**trigger_params)(handler)
        decorated = app.function_name(name=function_name)(decorated)
        logger.info(
            "Registered '%s' (%s) — %s",
            function_name,
            trigger_type,
            resolved.name,
        )
    except Exception as exc:
        logger.error(
            "Failed to register '%s' (%s): %s",
            function_name,
            trigger_type,
            exc,
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
        logger.warning("Skipping '%s': http_trigger requires 'route'", function_name)
        return

    methods = trigger_params.get("methods", ["POST"])
    auth_str = str(trigger_params.get("auth_level", "FUNCTION")).lower()
    auth_level = AUTH_LEVEL_MAP.get(auth_str, func.AuthLevel.FUNCTION)
    handler = make_http_agent_handler(resolved, capabilities)

    try:
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
    except Exception as exc:
        logger.error("Failed to register HTTP agent '%s': %s", function_name, exc)


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

    try:
        decorated = decorator_fn(**trigger_params)(handler)
        decorated = app.function_name(name=function_name)(decorated)
        logger.info(
            "Registered '%s' (%s) — %s",
            function_name,
            trigger_type,
            resolved.name,
        )
    except Exception as exc:
        logger.error(
            "Failed to register '%s' (%s): %s",
            function_name,
            trigger_type,
            exc,
        )


def register_agent(
    app: func.FunctionApp,
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
) -> None:
    """Register a non-main agent's trigger on the FunctionApp."""
    if resolved.is_main:
        logger.debug(
            "register_agent called with a main agent (%s) — ignoring; main agents are registered via debug endpoints.",
            resolved.name,
        )
        return

    if resolved.trigger is None:
        logger.warning(
            "Skipping '%s': resolved agent has no trigger",
            resolved.name,
        )
        return

    trigger_type = resolved.trigger.type.strip()
    trigger_params = _resolve_trigger_params(dict(resolved.trigger.args or {}))
    function_name = _function_name_from_source(resolved.source_file, resolved.name)

    _configure_connector_tools_if_needed(resolved, capabilities)

    if trigger_type == "http_trigger":
        _register_http_agent(app, resolved, capabilities, function_name, trigger_params)
        return

    if trigger_type.startswith("connectors."):
        _register_connector_agent(
            app,
            resolved,
            capabilities,
            function_name,
            trigger_params,
            trigger_type,
        )
        return

    _register_builtin_agent(
        app,
        resolved,
        capabilities,
        function_name,
        trigger_params,
        trigger_type,
    )
