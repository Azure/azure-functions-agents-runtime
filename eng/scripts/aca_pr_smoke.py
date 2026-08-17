#!/usr/bin/env python3
"""Fail-closed ARM preflight for the current-checkout ACA model smoke."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from urllib.parse import urlparse

_REQUIRED = (
    "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID",
    "ACA_SMOKE_MODEL_RESOURCE_ID",
    "ACA_SMOKE_MODEL_ROLE_DEFINITION_ID",
    "AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_AZURE_OPENAI_ENDPOINT",
)
_DATA_PLANE_ROLE_MARKERS = ("storage", "table", "blob", "queue", "sandbox")


class PreflightError(Exception):
    """A redacted, actionable protected-resource preflight failure."""


def required(environment: Mapping[str, str], name: str) -> str:
    """Read a configured value without accepting unresolved pipeline variables."""

    value = environment.get(name, "").strip()
    if not value or value.startswith("$("):
        raise PreflightError(f"required_environment_invalid:{name}")
    return value


def guest_identity_principal_id(group: Mapping[str, object]) -> str:
    """Require precisely one guest user-assigned identity and no system identity."""

    identity = group.get("identity")
    if not isinstance(identity, dict):
        raise PreflightError("sandbox_group_identity_missing")
    identity_type = identity.get("type")
    identities = identity.get("userAssignedIdentities")
    if identity_type != "UserAssigned" or not isinstance(identities, dict) or len(identities) != 1:
        raise PreflightError("sandbox_group_identity_ambiguous")
    resource_id = next(iter(identities))
    if not isinstance(resource_id, str) or not resource_id:
        raise PreflightError("sandbox_group_identity_ambiguous")
    principal_id = identities[resource_id].get("principalId")
    if not isinstance(principal_id, str) or not principal_id:
        raise PreflightError("sandbox_group_guest_principal_id_missing")
    return principal_id


def model_host(endpoint: str) -> str:
    """Normalize the one permitted model host."""

    host = urlparse(endpoint).hostname
    if host is None:
        raise PreflightError("model_endpoint_invalid")
    return host.casefold()


def _strings(value: object) -> set[str]:
    if isinstance(value, str):
        return {value.casefold()}
    if isinstance(value, dict):
        return set().union(*(_strings(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_strings(item) for item in value))
    return set()


def assert_model_only_egress(group: Mapping[str, object], endpoint: str) -> None:
    """Reject missing or broader-than-model-host egress policy evidence."""

    properties = group.get("properties")
    if not isinstance(properties, dict):
        raise PreflightError("sandbox_group_egress_not_model_only")
    policy = properties.get("egressPolicy", properties)
    if not isinstance(policy, dict):
        raise PreflightError("sandbox_group_egress_not_model_only")
    allowed_hosts = policy.get("allowedHosts")
    if not isinstance(allowed_hosts, list) or not all(
        isinstance(host, str) for host in allowed_hosts
    ):
        raise PreflightError("sandbox_group_egress_not_model_only")
    hosts = {host.casefold().rstrip(".") for host in allowed_hosts}
    expected = model_host(endpoint)
    if hosts != {expected}:
        raise PreflightError("sandbox_group_egress_not_model_only")


def assert_model_only_roles(
    assignments: Sequence[Mapping[str, object]],
    model_role_definition_id: str,
    model_resource_id: str,
) -> None:
    """Require one model role and reject any state or Sandbox data-plane role."""

    expected_role = model_role_definition_id.casefold()
    expected_scope = model_resource_id.rstrip("/").casefold()
    if len(assignments) != 1:
        raise PreflightError("guest_role_assignment_ambiguous")
    assignment = assignments[0]
    role_id = assignment.get("roleDefinitionId")
    scope = assignment.get("scope")
    if not isinstance(role_id, str) or role_id.casefold() != expected_role:
        raise PreflightError("guest_model_role_missing")
    if not isinstance(scope, str) or scope.rstrip("/").casefold() != expected_scope:
        raise PreflightError("guest_role_assignment_ambiguous")
    names = " ".join(sorted(_strings(assignment)))
    if any(marker in names for marker in _DATA_PLANE_ROLE_MARKERS):
        raise PreflightError("guest_data_plane_role_forbidden")


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
    """Verify guest identity, RBAC, and egress using the controller connection."""

    values = {name: required(environment, name) for name in _REQUIRED}
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
    guest_principal_id = guest_identity_principal_id(group)
    assert_model_only_egress(group, values["AZURE_FUNCTIONS_AGENTS_ACA_SMOKE_AZURE_OPENAI_ENDPOINT"])
    assignments = az_json(
        [
            "role",
            "assignment",
            "list",
            "--assignee-object-id",
            guest_principal_id,
            "--all",
            "--include-inherited",
        ]
    )
    if not isinstance(assignments, list) or not all(isinstance(item, dict) for item in assignments):
        raise PreflightError("guest_role_assignments_unavailable")
    assert_model_only_roles(
        assignments,
        values["ACA_SMOKE_MODEL_ROLE_DEFINITION_ID"],
        values["ACA_SMOKE_MODEL_RESOURCE_ID"],
    )


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
