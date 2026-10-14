"""Copilot-owned recognition of native SDK values."""

from __future__ import annotations

from ..._tool_descriptor import register_harness_object_predicate

_COPILOT_MODULE_ROOT = "copilot"
_FOREIGN_TOOL_MODULE_ROOTS = frozenset({"agent_framework", _COPILOT_MODULE_ROOT})


def is_copilot_sdk_object(candidate: object) -> bool:
    return any(
        base.__module__.split(".", 1)[0] == _COPILOT_MODULE_ROOT
        for base in type(candidate).__mro__
    )


def is_native_tool_result_object(candidate: object) -> bool:
    return any(
        base.__module__.split(".", 1)[0] in _FOREIGN_TOOL_MODULE_ROOTS
        for base in type(candidate).__mro__
    )


def register_copilot_tool_predicate() -> None:
    register_harness_object_predicate(is_copilot_sdk_object)
