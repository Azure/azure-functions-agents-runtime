"""Private operator-authored policy for durable-loop tool routing."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..strict_json import DuplicateJsonKeyError, canonical_json_bytes, decode_json_object
from .durable_loop_activities import DurableContentStore, put_protocol_model
from .durable_loop_protocol import (
    MAX_INITIAL_SKILL_METADATA_BYTES,
    MAX_INITIAL_SKILL_METADATA_RECORDS,
    MAX_SKILL_CATALOG_RECORDS,
    ContentRefV1,
    DurableSkillCatalogSnapshotV1,
    DurableSkillMetadataV1,
    FrozenToolCatalogV1,
    FrozenToolDescriptorV1,
    ToolBehavior,
    ToolProvenance,
    canonical_hash,
)
from .durable_loop_tools import (
    LOAD_SKILL_TOOL_NAME,
    SEARCH_SKILLS_TOOL_NAME,
    human_input_tool_descriptor,
    load_skill_tool_descriptor,
    search_skills_tool_descriptor,
)
from .durable_skill_providers import DurableSkillProvider

DURABLE_LOOP_TOOL_POLICY_FILENAME = "durable-loop-tools.json"
_RUNTIME_SKILL_TOOL_NAMES = frozenset(
    {SEARCH_SKILLS_TOOL_NAME, LOAD_SKILL_TOOL_NAME}
)


class DurableLoopToolPolicyError(RuntimeError):
    """The private durable-loop tool policy is missing or inconsistent."""


class DurableSkillCatalogAdmissionError(RuntimeError):
    """A skill catalog cannot be frozen safely for durable execution."""


class _DurableSkillCatalogModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class MaterializedDurableSkillV1(_DurableSkillCatalogModel):
    """One admitted skill's immutable content reference."""

    schema_version: Literal["1"] = "1"
    skill_id: Annotated[str, Field(min_length=1, max_length=128)]
    version: Annotated[str, Field(min_length=1, max_length=128)]
    content_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    content_ref: ContentRefV1


class FrozenDurableSkillCatalogV1(_DurableSkillCatalogModel):
    """Persisted provider snapshot plus materialized exact-version content refs."""

    schema_version: Literal["1"] = "1"
    snapshot: DurableSkillCatalogSnapshotV1
    materialized: Annotated[
        tuple[MaterializedDurableSkillV1, ...],
        Field(max_length=MAX_SKILL_CATALOG_RECORDS),
    ] = ()

    @model_validator(mode="after")
    def validate_materialized_entries(self) -> FrozenDurableSkillCatalogV1:
        metadata = {item.skill_id: item for item in self.snapshot.metadata}
        if len(self.materialized) != len(metadata):
            raise ValueError("every admitted skill must be materialized")
        if tuple(item.skill_id for item in self.materialized) != tuple(metadata):
            raise ValueError("materialized skills must use catalog order")
        for item in self.materialized:
            admitted = metadata[item.skill_id]
            if (
                item.version != admitted.version
                or item.content_hash != admitted.content_hash
            ):
                raise ValueError("materialized skill does not match admitted metadata")
        return self

    def by_id(self) -> dict[str, MaterializedDurableSkillV1]:
        """Return materialized content keyed by stable skill ID."""
        return {item.skill_id: item for item in self.materialized}


@dataclass(frozen=True, slots=True)
class DurableSkillCatalogAdmission:
    """Frozen refs and model-visible projections produced before run admission."""

    catalog_ref: ContentRefV1
    catalog_hash: str
    initial_metadata: tuple[DurableSkillMetadataV1, ...]
    tool_catalog: FrozenToolCatalogV1


class DurableToolPolicyEntryV1(BaseModel):
    """One exact model-visible tool routing decision."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    provenance: Literal["local", "remote"]
    behavior: ToolBehavior
    parallel_safe: bool = False

    @model_validator(mode="after")
    def validate_parallelism(self) -> DurableToolPolicyEntryV1:
        if self.parallel_safe and (
            self.provenance != ToolProvenance.REMOTE.value
            or self.behavior is not ToolBehavior.READ_ONLY
        ):
            raise ValueError(
                "parallel_safe is allowed only for remote read-only tools"
            )
        return self


class DurableToolPolicyDocumentV1(BaseModel):
    """Strict private policy document keyed by model-visible tool name."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["1"]
    tools: dict[str, DurableToolPolicyEntryV1]

    @model_validator(mode="after")
    def validate_names(self) -> DurableToolPolicyDocumentV1:
        if not self.tools:
            raise ValueError("tool policy must contain at least one tool")
        for name in self.tools:
            if (
                not name
                or len(name) > 128
                or not (name[0].isalpha() or name[0] == "_")
                or any(
                    not (character.isalnum() or character in "_.-")
                    for character in name
                )
            ):
                raise ValueError("tool policy contains an invalid tool name")
        return self


def load_durable_tool_policy(
    app_root: Path,
    *,
    required: bool,
) -> DurableToolPolicyDocumentV1 | None:
    """Load the private policy with duplicate-key rejection."""
    path = app_root / DURABLE_LOOP_TOOL_POLICY_FILENAME
    if not path.exists():
        if required:
            raise DurableLoopToolPolicyError(
                f"{DURABLE_LOOP_TOOL_POLICY_FILENAME} is required when durable tools are enabled"
            )
        return None
    try:
        decoded = decode_json_object(path.read_bytes())
        return DurableToolPolicyDocumentV1.model_validate_json(
            canonical_json_bytes(decoded)
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        DuplicateJsonKeyError,
        ValidationError,
        TypeError,
        ValueError,
    ) as exc:
        raise DurableLoopToolPolicyError(
            f"{DURABLE_LOOP_TOOL_POLICY_FILENAME} is invalid"
        ) from exc


def freeze_durable_tool_catalog(
    discovered: Sequence[FrozenToolDescriptorV1],
    policy: DurableToolPolicyDocumentV1 | None,
    *,
    package_hash: str,
    base_policy_hash: str,
    include_human_input: bool = True,
) -> FrozenToolCatalogV1:
    """Bind every discovered tool to one explicit policy entry."""
    by_name = {descriptor.name: descriptor for descriptor in discovered}
    if len(by_name) != len(discovered):
        raise DurableLoopToolPolicyError(
            "durable tool discovery produced duplicate model-visible names"
        )
    collisions = sorted(_RUNTIME_SKILL_TOOL_NAMES.intersection(by_name))
    if collisions:
        raise DurableLoopToolPolicyError(
            f"customer tools use reserved runtime names: {collisions}"
        )
    policy_tools: Mapping[str, DurableToolPolicyEntryV1] = (
        {} if policy is None else policy.tools
    )
    if set(policy_tools) != set(by_name):
        missing = sorted(set(by_name) - set(policy_tools))
        unknown = sorted(set(policy_tools) - set(by_name))
        raise DurableLoopToolPolicyError(
            "durable tool policy does not match discovery "
            f"(missing={missing}, unknown={unknown})"
        )
    mismatched = sorted(
        name
        for name, descriptor in by_name.items()
        if descriptor.provenance.value != policy_tools[name].provenance
    )
    if mismatched:
        raise DurableLoopToolPolicyError(
            f"durable tool policy provenance does not match discovery: {mismatched}"
        )
    frozen = tuple(
        descriptor.model_copy(
            update={
                "behavior": policy_tools[name].behavior,
                "parallel_safe": policy_tools[name].parallel_safe,
                "provenance": ToolProvenance(policy_tools[name].provenance),
            }
        )
        for name, descriptor in sorted(by_name.items())
    )
    policy_hash = canonical_hash(
        {
            "base_policy_hash": base_policy_hash,
            "schema_version": "1",
            "tools": {
                name: entry.model_dump(mode="json")
                for name, entry in sorted(policy_tools.items())
            },
        }
    )
    catalog = FrozenToolCatalogV1.create(
        tools=(
            *frozen,
            *((human_input_tool_descriptor(),) if include_human_input else ()),
        ),
        policy_hash=policy_hash,
        package_hash=package_hash,
    )
    if len(canonical_json_bytes(catalog.model_dump(mode="json"))) > 1024 * 1024:
        raise DurableLoopToolPolicyError("durable tool catalog exceeds the byte limit")
    return catalog


def initial_skill_metadata(
    metadata: Sequence[DurableSkillMetadataV1],
) -> tuple[DurableSkillMetadataV1, ...]:
    """Select complete deterministic records within the initial context budget."""
    selected: list[DurableSkillMetadataV1] = []
    for item in metadata:
        if len(selected) >= MAX_INITIAL_SKILL_METADATA_RECORDS:
            break
        candidate = (*selected, item)
        if (
            len(
                canonical_json_bytes(
                    [entry.model_dump(mode="json") for entry in candidate]
                )
            )
            > MAX_INITIAL_SKILL_METADATA_BYTES
        ):
            break
        selected.append(item)
    return tuple(selected)


def inject_skill_runtime_tools(
    catalog: FrozenToolCatalogV1,
    *,
    skill_count: int,
    initial_metadata_count: int,
) -> FrozenToolCatalogV1:
    """Inject only the runtime skill descriptors required by one frozen catalog."""
    customer_collisions = sorted(
        descriptor.name
        for descriptor in catalog.tools
        if descriptor.name in _RUNTIME_SKILL_TOOL_NAMES
    )
    if customer_collisions:
        raise DurableSkillCatalogAdmissionError(
            f"customer tools use reserved runtime names: {customer_collisions}"
        )
    runtime_tools: list[FrozenToolDescriptorV1] = []
    if skill_count:
        runtime_tools.append(load_skill_tool_descriptor())
    if skill_count > initial_metadata_count:
        runtime_tools.append(search_skills_tool_descriptor())
    return FrozenToolCatalogV1.create(
        tools=(*catalog.tools, *runtime_tools),
        policy_hash=catalog.policy_hash,
        package_hash=catalog.package_hash,
    )


async def admit_durable_skill_catalog(
    *,
    provider: DurableSkillProvider,
    content: DurableContentStore,
    agent_slug: str,
    enabled_skill_ids: Sequence[str],
    retain_until: datetime,
    tool_catalog: FrozenToolCatalogV1,
    requested_revision: str | None = None,
) -> DurableSkillCatalogAdmission:
    """Freeze enabled provider skills and materialize exact content before admission."""
    source = await provider.open_snapshot(
        agent_slug=agent_slug,
        requested_revision=requested_revision,
        retain_until=retain_until,
    )
    enabled = tuple(dict.fromkeys(enabled_skill_ids))
    if len(enabled) != len(enabled_skill_ids):
        raise DurableSkillCatalogAdmissionError(
            "enabled durable skill IDs must be unique"
        )
    source_by_id = {item.skill_id: item for item in source.metadata}
    unknown = sorted(set(enabled).difference(source_by_id))
    if unknown:
        raise DurableSkillCatalogAdmissionError(
            f"enabled durable skills are absent from the provider: {unknown}"
        )
    admitted_metadata = tuple(
        item for item in source.metadata if item.skill_id in set(enabled)
    )
    admitted = DurableSkillCatalogSnapshotV1.create(
        provider_id=source.provider_id,
        catalog_revision=source.catalog_revision,
        metadata=admitted_metadata,
        snapshot_token=source.snapshot_token,
        retain_until=source.retain_until,
    )
    materialized: list[MaterializedDurableSkillV1] = []
    for metadata_item in admitted_metadata:
        skill = await provider.get_content(
            snapshot=source,
            skill_id=metadata_item.skill_id,
            version=metadata_item.version,
            expected_hash=metadata_item.content_hash,
        )
        if (
            skill.skill_id != metadata_item.skill_id
            or skill.version != metadata_item.version
            or skill.content_hash != metadata_item.content_hash
            or skill.executable
        ):
            raise DurableSkillCatalogAdmissionError(
                f"provider content changed for skill {metadata_item.skill_id!r}"
            )
        content_ref = await put_protocol_model(
            content,
            kind="skill-content",
            model=skill,
            retention_class="skill",
        )
        materialized.append(
            MaterializedDurableSkillV1(
                skill_id=skill.skill_id,
                version=skill.version,
                content_hash=skill.content_hash,
                content_ref=content_ref,
            )
        )
    frozen = FrozenDurableSkillCatalogV1(
        snapshot=admitted,
        materialized=tuple(materialized),
    )
    catalog_ref = await put_protocol_model(
        content,
        kind="skill-catalog",
        model=frozen,
        retention_class="skill",
    )
    initial = initial_skill_metadata(admitted.metadata)
    return DurableSkillCatalogAdmission(
        catalog_ref=catalog_ref,
        catalog_hash=admitted.catalog_hash,
        initial_metadata=initial,
        tool_catalog=inject_skill_runtime_tools(
            tool_catalog,
            skill_count=len(admitted.metadata),
            initial_metadata_count=len(initial),
        ),
    )
