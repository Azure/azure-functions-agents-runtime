from __future__ import annotations

import json
from pathlib import Path

import pytest

from azure_functions_agents.experimental.durable_loop_catalog import (
    DurableLoopToolPolicyError,
    freeze_durable_tool_catalog,
    load_durable_tool_policy,
)
from azure_functions_agents.experimental.durable_loop_protocol import (
    FrozenToolDescriptorV1,
    ToolBehavior,
    ToolProvenance,
)


def _descriptor(name: str, provenance: ToolProvenance) -> FrozenToolDescriptorV1:
    return FrozenToolDescriptorV1(
        name=name,
        description=f"{name} tool",
        parameters={"additionalProperties": True, "type": "object"},
        provenance=provenance,
        behavior=ToolBehavior.MUTATING,
    )


def _write_policy(root: Path, tools: dict[str, object]) -> None:
    (root / "durable-loop-tools.json").write_text(
        json.dumps({"schema_version": "1", "tools": tools}),
        encoding="utf-8",
    )


def test_finalized_sample_policy_shape_is_accepted(tmp_path: Path) -> None:
    policy_tools = {
        "adaptive_probe": {
            "provenance": "local",
            "behavior": "read_only",
            "parallel_safe": False,
        },
        "chain_probe": {
            "provenance": "local",
            "behavior": "read_only",
            "parallel_safe": False,
        },
        "customer_probe": {
            "provenance": "local",
            "behavior": "read_only",
            "parallel_safe": False,
        },
        "delayed_probe": {
            "provenance": "local",
            "behavior": "read_only",
            "parallel_safe": False,
        },
        "microsoft_code_sample_search": {
            "provenance": "remote",
            "behavior": "read_only",
            "parallel_safe": True,
        },
        "microsoft_docs_fetch": {
            "provenance": "remote",
            "behavior": "read_only",
            "parallel_safe": True,
        },
        "microsoft_docs_search": {
            "provenance": "remote",
            "behavior": "read_only",
            "parallel_safe": True,
        },
        "read_file": {
            "provenance": "local",
            "behavior": "read_only",
            "parallel_safe": False,
        },
        "run_shell": {
            "provenance": "local",
            "behavior": "mutating",
            "parallel_safe": False,
        },
        "search_files": {
            "provenance": "local",
            "behavior": "read_only",
            "parallel_safe": False,
        },
        "unsafe_write_probe": {
            "provenance": "local",
            "behavior": "mutating",
            "parallel_safe": False,
        },
        "write_file": {
            "provenance": "local",
            "behavior": "idempotent_write",
            "parallel_safe": False,
        },
    }
    _write_policy(tmp_path, policy_tools)
    descriptors = tuple(
        _descriptor(
            name,
            (
                ToolProvenance.REMOTE
                if value["provenance"] == "remote"
                else ToolProvenance.LOCAL
            ),
        )
        for name, value in policy_tools.items()
    )

    policy = load_durable_tool_policy(tmp_path, required=True)
    catalog = freeze_durable_tool_catalog(
        descriptors,
        policy,
        package_hash="a" * 64,
        base_policy_hash="b" * 64,
    )

    by_name = catalog.by_name()
    assert by_name["write_file"].behavior is ToolBehavior.IDEMPOTENT_WRITE
    assert by_name["unsafe_write_probe"].behavior is ToolBehavior.MUTATING
    assert by_name["microsoft_docs_search"].parallel_safe is True
    assert by_name["request_human_input"].provenance is ToolProvenance.RUNTIME


def test_policy_requires_exact_names_and_provenance(tmp_path: Path) -> None:
    _write_policy(
        tmp_path,
        {
            "lookup": {
                "provenance": "remote",
                "behavior": "read_only",
                "parallel_safe": True,
            }
        },
    )
    policy = load_durable_tool_policy(tmp_path, required=True)

    with pytest.raises(DurableLoopToolPolicyError, match="provenance"):
        freeze_durable_tool_catalog(
            (_descriptor("lookup", ToolProvenance.LOCAL),),
            policy,
            package_hash="a" * 64,
            base_policy_hash="b" * 64,
        )
    with pytest.raises(DurableLoopToolPolicyError, match="does not match"):
        freeze_durable_tool_catalog(
            (
                _descriptor("lookup", ToolProvenance.REMOTE),
                _descriptor("extra", ToolProvenance.LOCAL),
            ),
            policy,
            package_hash="a" * 64,
            base_policy_hash="b" * 64,
        )


def test_policy_rejects_parallel_local_tool(tmp_path: Path) -> None:
    _write_policy(
        tmp_path,
        {
            "lookup": {
                "provenance": "local",
                "behavior": "read_only",
                "parallel_safe": True,
            }
        },
    )

    with pytest.raises(DurableLoopToolPolicyError, match="invalid"):
        load_durable_tool_policy(tmp_path, required=True)
