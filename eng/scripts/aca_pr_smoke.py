#!/usr/bin/env python3
"""Fail-closed ARM preflight for the current-checkout ACA model smoke."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence

_REQUIRED = (
    "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID",
    "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_DISK",
    "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_AZURE_OPENAI_ENDPOINT",
    "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_AZURE_OPENAI_DEPLOYMENT",
)
_FORBIDDEN = ("AZURE_CLIENT_ID",)


class PreflightError(Exception):
    """A redacted, actionable protected-resource preflight failure."""


def required(environment: Mapping[str, str], name: str) -> str:
    """Read a configured value without accepting unresolved pipeline variables."""

    value = environment.get(name, "").strip()
    if not value or "$(" in value:
        raise PreflightError(f"required_environment_invalid:{name}")
    return value


def forbidden(environment: Mapping[str, str], name: str) -> None:
    """Reject an explicit host identity that would mask the guest identity."""

    if environment.get(name, "").strip():
        raise PreflightError(f"forbidden_environment_set:{name}")


def assert_guest_identity(group: Mapping[str, object]) -> None:
    """Require exactly one user-assigned identity and no system-assigned identity."""

    identity = group.get("identity")
    if not isinstance(identity, dict):
        raise PreflightError("sandbox_group_identity_missing")
    identities = identity.get("userAssignedIdentities")
    if (
        identity.get("type") != "UserAssigned"
        or not isinstance(identities, dict)
        or len(identities) != 1
        or not all(isinstance(resource_id, str) and resource_id for resource_id in identities)
    ):
        raise PreflightError("sandbox_group_identity_ambiguous")


def az_json(arguments: Sequence[str]) -> object:
    """Run an audit-only Azure CLI query without returning its sensitive payload."""

    result = subprocess.run(
        ["az", *arguments, "--output", "json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise PreflightError("arm_audit_query_failed")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise PreflightError("arm_audit_response_invalid") from error


def preflight(environment: Mapping[str, str] = os.environ) -> None:
    """Verify environment and the sole guest UAMI through the controller connection."""

    values = {name: required(environment, name) for name in _REQUIRED}
    for name in _FORBIDDEN:
        forbidden(environment, name)
    group = az_json(
        [
            "rest",
            "--method",
            "get",
            "--url",
            f"{values['AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID']}?api-version=2026-02-01-preview",
        ]
    )
    if not isinstance(group, dict):
        raise PreflightError("sandbox_group_arm_response_invalid")
    assert_guest_identity(group)


def main() -> int:
    """Run the preflight and surface only stable failure categories."""

    try:
        preflight()
    except PreflightError as error:
        print(f"ACA PR smoke preflight failed: {error}", file=sys.stderr)
        return 1
    print("ACA PR smoke identity preflight completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
