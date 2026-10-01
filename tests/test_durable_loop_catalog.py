from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from azure_functions_agents.experimental.durable_loop_activities import (
    InMemoryDurableContentStore,
    get_protocol_model,
)
from azure_functions_agents.experimental.durable_loop_catalog import (
    DurableLoopToolPolicyError,
    DurableSkillCatalogAdmissionError,
    FrozenDurableSkillCatalogV1,
    admit_durable_skill_catalog,
    freeze_durable_tool_catalog,
    inject_skill_runtime_tools,
    load_durable_tool_policy,
)
from azure_functions_agents.experimental.durable_loop_protocol import (
    FrozenToolCatalogV1,
    FrozenToolDescriptorV1,
    ToolBehavior,
    ToolProvenance,
)
from azure_functions_agents.experimental.durable_skill_providers import (
    PackagedFilesystemDurableSkillProvider,
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


def test_catalog_can_omit_human_input_for_unattended_runs(tmp_path: Path) -> None:
    catalog = freeze_durable_tool_catalog(
        (),
        None,
        package_hash="a" * 64,
        base_policy_hash="b" * 64,
        include_human_input=False,
    )

    assert "request_human_input" not in catalog.by_name()


def test_skill_runtime_rejects_customer_reserved_name() -> None:
    catalog = FrozenToolCatalogV1.create(
        tools=(_descriptor("load_skill", ToolProvenance.LOCAL),),
        policy_hash="a" * 64,
        package_hash="b" * 64,
    )

    with pytest.raises(DurableSkillCatalogAdmissionError, match="reserved"):
        inject_skill_runtime_tools(
            catalog,
            skill_count=1,
            initial_metadata_count=1,
        )


def test_skill_runtime_injects_search_only_when_initial_metadata_is_incomplete() -> None:
    catalog = FrozenToolCatalogV1.create(
        tools=(),
        policy_hash="a" * 64,
        package_hash="b" * 64,
    )

    complete = inject_skill_runtime_tools(
        catalog,
        skill_count=1,
        initial_metadata_count=1,
    )
    incomplete = inject_skill_runtime_tools(
        catalog,
        skill_count=2,
        initial_metadata_count=1,
    )

    assert set(complete.by_name()) == {"load_skill"}
    assert set(incomplete.by_name()) == {"load_skill", "search_skills"}


@pytest.mark.asyncio
async def test_admission_materializes_enabled_packaged_skills_and_injects_runtime_tools(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skills" / "alpha"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Alpha guidance\n---\nUse alpha.",
        encoding="utf-8",
    )
    provider = PackagedFilesystemDurableSkillProvider(tmp_path)
    store = InMemoryDurableContentStore()
    customer_catalog = FrozenToolCatalogV1.create(
        tools=(),
        policy_hash="a" * 64,
        package_hash="b" * 64,
    )

    admission = await admit_durable_skill_catalog(
        provider=provider,
        content=store,
        agent_slug="main",
        enabled_skill_ids=("alpha",),
        retain_until=datetime.now(UTC) + timedelta(days=31),
        tool_catalog=customer_catalog,
    )

    frozen = await get_protocol_model(
        store,
        admission.catalog_ref,
        FrozenDurableSkillCatalogV1,
    )
    assert admission.catalog_hash == frozen.snapshot.catalog_hash
    assert [item.skill_id for item in frozen.snapshot.metadata] == ["alpha"]
    assert frozen.materialized[0].content_ref.byte_length > 0
    assert set(admission.tool_catalog.by_name()) == {"load_skill"}
