"""MAF-owned recognition of native tool objects."""

from __future__ import annotations

from ..._tool_descriptor import register_harness_object_predicate

_MAF_MODULE_ROOT = "agent_framework"


def is_maf_sdk_object(candidate: object) -> bool:
    return any(
        base.__module__.split(".", 1)[0] == _MAF_MODULE_ROOT
        for base in type(candidate).__mro__
    )


def register_maf_tool_predicate() -> None:
    register_harness_object_predicate(is_maf_sdk_object)
