"""
Front matter → Azure Functions trigger translator.

Translates agent YAML frontmatter into Azure Functions trigger registrations.
This module is the single entry-point for all trigger wiring — built-in
triggers (timer, queue, blob, HTTP, …) *and* connector-backed triggers.

Key responsibilities:

* **Alias resolution** — customers write ``type: timer`` instead of
  ``type: timer_trigger``.
* **Parameter normalization** — e.g. 5-part cron → 6-part for timer triggers.
* **Decorator dispatch** — calls the correct ``app.<trigger>()`` or
  ``connectors.<service>.<trigger>()`` method with the right kwargs.
* **Handler creation** — delegates to :mod:`handlers` for the actual
  async callable that runs the agent.

Dependency graph::

    app_analyzer.py ─> translator.py ─> handlers.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import azure.functions as func

from .config import resolve_env_var
from .handlers import make_agent_handler, make_http_agent_handler


# ---------------------------------------------------------------------------
# Trigger aliases
# ---------------------------------------------------------------------------

TRIGGER_ALIASES: Dict[str, str] = {
    # Timer
    "timer": "timer_trigger",
    "schedule": "timer_trigger",
    # Queue
    "queue": "queue_trigger",
    # Blob
    "blob": "blob_trigger",
    # Service Bus
    "service_bus_queue": "service_bus_queue_trigger",
    "service_bus_topic": "service_bus_topic_trigger",
    # Event Hub
    "event_hub": "event_hub_message_trigger",
    # Event Grid
    "event_grid": "event_grid_trigger",
    # Cosmos DB
    "cosmos_db": "cosmos_db_trigger_v3",
    # HTTP
    "http": "http_trigger",
}


def resolve_trigger_alias(name: str) -> str:
    """Resolve a user-friendly trigger name to its SDK decorator name.

    Connector triggers (containing ``"."``) are returned unchanged.
    Unknown names are returned as-is — the caller will attempt ``getattr``
    and emit a warning if the decorator doesn't exist.
    """
    key = name.strip().lower()
    if "." in key:
        return name.strip()
    return TRIGGER_ALIASES.get(key, name.strip())


# ---------------------------------------------------------------------------
# Registration dataclass
# ---------------------------------------------------------------------------

@dataclass
class AgentTriggerRegistration:
    """Fully-resolved data needed to register one triggered agent."""

    function_name: str
    agent_name: str
    trigger_type: str  # raw type from frontmatter (pre-alias resolution)
    trigger_params: Dict[str, Any]
    prompt: str
    should_log: bool = True
    sandbox_config: Optional[Dict[str, Any]] = None
    response_example: Optional[str] = None
    response_schema: Optional[dict] = None


# ---------------------------------------------------------------------------
# Parameter helpers
# ---------------------------------------------------------------------------

def _normalize_timer_schedule(schedule: str) -> str:
    """Accept 5-part cron by prepending seconds; keep 6-part schedules unchanged."""
    parts = schedule.strip().split()
    if len(parts) == 5:
        return f"0 {schedule.strip()}"
    return schedule.strip()


def _resolve_trigger_params(trigger_params: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve ``$ENV_VAR`` references on all string values in trigger params."""
    resolved = {}
    for key, value in trigger_params.items():
        if isinstance(value, str):
            resolved[key] = resolve_env_var(value)
        else:
            resolved[key] = value
    return resolved


# ---------------------------------------------------------------------------
# Auth-level mapping (HTTP triggers)
# ---------------------------------------------------------------------------

_AUTH_LEVEL_MAP = {
    "anonymous": func.AuthLevel.ANONYMOUS,
    "function": func.AuthLevel.FUNCTION,
    "admin": func.AuthLevel.ADMIN,
}


# ---------------------------------------------------------------------------
# Internal registration helpers
# ---------------------------------------------------------------------------

def _register_builtin(
    app: func.FunctionApp,
    reg: AgentTriggerRegistration,
    resolved_type: str,
) -> None:
    """Register a triggered agent using a built-in Azure Functions trigger."""

    # HTTP triggers use a dedicated handler
    if resolved_type == "http_trigger":
        _register_http(app, reg)
        return

    decorator_fn = getattr(app, resolved_type, None)
    if decorator_fn is None:
        logging.warning(
            f"Skipping '{reg.function_name}': unknown trigger type '{resolved_type}'"
        )
        return

    # Defensive copy — avoid mutating the caller's dict
    params = dict(reg.trigger_params)

    # Timer triggers: normalize schedule
    if resolved_type == "timer_trigger" and "schedule" in params:
        params["schedule"] = _normalize_timer_schedule(str(params["schedule"]))

    handler = make_agent_handler(
        reg.function_name,
        reg.agent_name,
        resolved_type,
        reg.should_log,
        sandbox_config=reg.sandbox_config,
        agent_instructions=reg.prompt,
    )

    params["arg_name"] = "trigger_data"
    try:
        decorated = decorator_fn(**params)(handler)
        app.function_name(name=reg.function_name)(decorated)
        logging.info(
            f"Registered '{reg.function_name}' ({resolved_type}) — {reg.agent_name}"
        )
    except Exception as exc:
        logging.error(
            f"Failed to register '{reg.function_name}' ({resolved_type}): {exc}"
        )


def _register_http(
    app: func.FunctionApp,
    reg: AgentTriggerRegistration,
) -> None:
    """Register an HTTP-triggered agent using ``app.route()``."""
    route = reg.trigger_params.get("route")
    if not route:
        logging.warning(f"Skipping '{reg.function_name}': http_trigger requires 'route'")
        return

    methods = reg.trigger_params.get("methods", ["POST"])
    auth_str = str(reg.trigger_params.get("auth_level", "FUNCTION")).lower()
    auth_level = _AUTH_LEVEL_MAP.get(auth_str, func.AuthLevel.FUNCTION)

    handler = make_http_agent_handler(
        reg.function_name,
        reg.agent_name,
        reg.should_log,
        sandbox_config=reg.sandbox_config,
        agent_instructions=reg.prompt,
        response_example=reg.response_example,
        response_schema=reg.response_schema,
    )

    try:
        decorated = app.route(route=route, methods=methods, auth_level=auth_level)(handler)
        app.function_name(name=reg.function_name)(decorated)
        logging.info(
            f"Registered HTTP agent '{reg.function_name}' at /{route} ({methods}) — {reg.agent_name}"
        )
    except Exception as exc:
        logging.error(
            f"Failed to register HTTP agent '{reg.function_name}': {exc}"
        )


def _register_connector(
    app: func.FunctionApp,
    reg: AgentTriggerRegistration,
    connector_type: str,
    connectors_instance,
) -> Any:
    """Register a triggered agent using a connector trigger.

    Returns the (possibly freshly-created) connectors instance.
    """
    if connectors_instance is None:
        try:
            import azure.functions_connectors as fc

            connectors_instance = fc.FunctionsConnectors(app)
        except ImportError:
            logging.error(
                f"Skipping '{reg.function_name}': azure-functions-connectors package not installed. "
                "Install with: pip install azurefunctions-agents-runtime[connectors]"
            )
            return None

    # Resolve the decorator via getattr chain (e.g. "teams.new_channel_message_trigger")
    parts = connector_type.split(".")
    obj = connectors_instance
    try:
        for part in parts:
            obj = getattr(obj, part)
        decorator_fn = obj
    except AttributeError:
        logging.warning(
            f"Skipping '{reg.function_name}': could not resolve connector trigger '{connector_type}'"
        )
        return connectors_instance

    handler = make_agent_handler(
        reg.function_name,
        reg.agent_name,
        connector_type,
        reg.should_log,
        sandbox_config=reg.sandbox_config,
        agent_instructions=reg.prompt,
    )

    try:
        decorator_fn(**reg.trigger_params)(handler)
        logging.info(
            f"Registered '{reg.function_name}' ({connector_type}) — {reg.agent_name}"
        )
    except Exception as exc:
        logging.error(
            f"Failed to register '{reg.function_name}' ({connector_type}): {exc}"
        )

    return connectors_instance


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def register_agent_trigger(
    app: func.FunctionApp,
    registration: AgentTriggerRegistration,
    connectors_instance: Any = None,
) -> Any:
    """Register a single triggered agent on the ``FunctionApp``.

    This is the main entry point called by :mod:`app_analyzer` for each
    discovered agent.  Returns the (possibly updated) connector instance
    so the caller can pass it to subsequent registrations.
    """
    # Resolve env-var references in trigger params
    resolved_params = _resolve_trigger_params(registration.trigger_params)
    registration = AgentTriggerRegistration(
        function_name=registration.function_name,
        agent_name=registration.agent_name,
        trigger_type=registration.trigger_type,
        trigger_params=resolved_params,
        prompt=registration.prompt,
        should_log=registration.should_log,
        sandbox_config=registration.sandbox_config,
        response_example=registration.response_example,
        response_schema=registration.response_schema,
    )

    # Resolve alias
    resolved_type = resolve_trigger_alias(registration.trigger_type)

    # Connector vs built-in
    is_connector = "." in resolved_type
    if is_connector:
        connector_type = resolved_type.removeprefix("connectors.")
        return _register_connector(
            app, registration, connector_type, connectors_instance
        )
    else:
        _register_builtin(app, registration, resolved_type)
        return connectors_instance
