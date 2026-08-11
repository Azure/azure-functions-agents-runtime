"""Shared trigger decorator resolution for validation and registration."""

from __future__ import annotations

from typing import Any

import azure.functions as func

from azure_functions_agents.config.schema import TRIGGER_TYPES

_SUPPORTED_TRIGGER_TYPES = frozenset(TRIGGER_TYPES)


def resolve_trigger_decorator_name(owner: Any, trigger_type: str) -> str | None:
    """Return the decorator exposed by *owner* for an authored trigger type."""
    if trigger_type not in _SUPPORTED_TRIGGER_TYPES:
        return None
    if trigger_type == "http_trigger":
        return "route" if callable(getattr(owner, "route", None)) else None
    if trigger_type == "connector_trigger":
        if callable(getattr(owner, "connector_trigger", None)):
            return "connector_trigger"
        if callable(getattr(owner, "generic_trigger", None)):
            return "generic_trigger"
        return None
    if callable(getattr(owner, trigger_type, None)):
        return trigger_type
    return None


def is_supported_trigger_type(trigger_type: str) -> bool:
    """Return whether a standard FunctionApp can register this authored trigger."""
    return resolve_trigger_decorator_name(func.FunctionApp, trigger_type) is not None
