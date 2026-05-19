"""Public registration helpers."""

from .capabilities import AgentCapabilities, build_capabilities
from .endpoints import register_debug_endpoints, reset_debug_slug_registry
from .triggers import register_agent

__all__ = [
    "AgentCapabilities",
    "build_capabilities",
    "register_agent",
    "register_debug_endpoints",
    "reset_debug_slug_registry",
]
