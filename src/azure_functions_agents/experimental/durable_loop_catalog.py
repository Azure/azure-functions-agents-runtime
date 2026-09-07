"""Private operator-authored policy for durable-loop tool routing."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from ..strict_json import DuplicateJsonKeyError, canonical_json_bytes, decode_json_object
from .durable_loop_protocol import (
    FrozenToolCatalogV1,
    FrozenToolDescriptorV1,
    ToolBehavior,
    ToolProvenance,
    canonical_hash,
)
from .durable_loop_tools import human_input_tool_descriptor

DURABLE_LOOP_TOOL_POLICY_FILENAME = "durable-loop-tools.json"


class DurableLoopToolPolicyError(RuntimeError):
    """The private durable-loop tool policy is missing or inconsistent."""


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
) -> FrozenToolCatalogV1:
    """Bind every discovered tool to one explicit policy entry."""
    by_name = {descriptor.name: descriptor for descriptor in discovered}
    if len(by_name) != len(discovered):
        raise DurableLoopToolPolicyError(
            "durable tool discovery produced duplicate model-visible names"
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
        tools=(*frozen, human_input_tool_descriptor()),
        policy_hash=policy_hash,
        package_hash=package_hash,
    )
    if len(canonical_json_bytes(catalog.model_dump(mode="json"))) > 1024 * 1024:
        raise DurableLoopToolPolicyError("durable tool catalog exceeds the byte limit")
    return catalog
