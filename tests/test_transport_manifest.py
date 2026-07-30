"""Security-focused tests for the P4a state-row/live-manifest handshake."""

from __future__ import annotations

import json
from dataclasses import asdict, replace

import pytest

from azure_functions_agents.transport.manifest import (
    ExpectedSandboxManifestBinding,
    SandboxManifestMismatchError,
    parse_sandbox_manifest_binding,
    verify_sandbox_manifest,
)
from azure_functions_agents.transport.models import ProvisionedSandboxIdentity

_GROUP = (
    "/subscriptions/sub-123/resourceGroups/rg-agent/"
    "providers/Microsoft.App/sandboxGroups/session-group"
)


def _expected() -> ExpectedSandboxManifestBinding:
    return ExpectedSandboxManifestBinding(
        manifest_version=1,
        protocol_version="maf-session-v1",
        session_id="session-123",
        owner_hash_version="o1",
        owner_hash="owner-hash-secret-value",
        app_hash="app-hash-secret-value",
        sandbox_group_resource_id=_GROUP,
        sandbox_id="sandbox-123",
        generation=4,
        digest_kind="funcs_zip",
        digest="sha256:digest-secret-value",
    )


def _observed_payload(expected: ExpectedSandboxManifestBinding) -> bytes:
    return json.dumps(asdict(expected), sort_keys=True).encode("utf-8")


def _live_identity(expected: ExpectedSandboxManifestBinding) -> ProvisionedSandboxIdentity:
    return ProvisionedSandboxIdentity(
        sandbox_id=expected.sandbox_id,
        group_resource_id=expected.sandbox_group_resource_id,
        region="westus2",
    )


def test_manifest_parse_and_verify_accepts_exact_authoritative_binding() -> None:
    expected = _expected()

    observed = parse_sandbox_manifest_binding(_observed_payload(expected))

    verify_sandbox_manifest(expected, observed, _live_identity(expected))


def test_manifest_parser_accepts_fields_owned_by_later_harness_contracts() -> None:
    expected = _expected()
    payload = {
        **asdict(expected),
        "capabilities": {"events": "v1"},
        "created_at": "2026-07-30T00:00:00Z",
    }

    observed = parse_sandbox_manifest_binding(json.dumps(payload).encode("utf-8"))

    verify_sandbox_manifest(expected, observed, _live_identity(expected))


@pytest.mark.parametrize(
    ("field_name", "forged_value"),
    [
        ("sandbox_id", "forged-sandbox"),
        (
            "sandbox_group_resource_id",
            "/subscriptions/sub-123/resourceGroups/rg-agent/providers/Microsoft.App/"
            "sandboxGroups/repointed-group",
        ),
        ("generation", 3),
        ("protocol_version", "forged-protocol"),
        ("owner_hash_version", "o2"),
        ("owner_hash", "forged-owner"),
        ("app_hash", "forged-app"),
        ("session_id", "forged-session"),
        ("digest_kind", "other_digest"),
        ("digest", "sha256:forged"),
    ],
)
def test_manifest_verifier_rejects_each_routing_critical_forgery(
    field_name: str, forged_value: object
) -> None:
    expected = _expected()
    forged = replace(expected, **{field_name: forged_value})
    observed = parse_sandbox_manifest_binding(_observed_payload(forged))

    with pytest.raises(SandboxManifestMismatchError) as exc_info:
        verify_sandbox_manifest(expected, observed, _live_identity(expected))

    assert field_name in exc_info.value.fields
    rendered = str(exc_info.value)
    assert "owner-hash-secret-value" not in rendered
    assert "digest-secret-value" not in rendered


def test_manifest_verifier_rejects_repointed_live_sandbox_even_if_manifest_matches() -> None:
    expected = _expected()
    observed = parse_sandbox_manifest_binding(_observed_payload(expected))
    repointed_live = ProvisionedSandboxIdentity(
        sandbox_id="repointed-sandbox",
        group_resource_id=expected.sandbox_group_resource_id,
        region="westus2",
    )

    with pytest.raises(SandboxManifestMismatchError) as exc_info:
        verify_sandbox_manifest(expected, observed, repointed_live)

    assert exc_info.value.fields == frozenset({"sandbox_id"})


def test_manifest_verifier_rejects_repointed_live_group_even_if_manifest_matches() -> None:
    expected = _expected()
    observed = parse_sandbox_manifest_binding(_observed_payload(expected))
    repointed_live = ProvisionedSandboxIdentity(
        sandbox_id=expected.sandbox_id,
        group_resource_id=(
            "/subscriptions/sub-123/resourceGroups/rg-agent/providers/Microsoft.App/"
            "sandboxGroups/repointed-group"
        ),
        region="westus2",
    )

    with pytest.raises(SandboxManifestMismatchError) as exc_info:
        verify_sandbox_manifest(expected, observed, repointed_live)

    assert exc_info.value.fields == frozenset({"sandbox_group_resource_id"})


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"[]",
        b'{"session_id": "partial"}',
        json.dumps({**asdict(_expected()), "generation": "4"}).encode("utf-8"),
        json.dumps(
            {
                **asdict(_expected()),
                "sandbox_group_resource_id": "/not/a/sandbox/group",
            }
        ).encode("utf-8"),
        json.dumps(
            {
                **asdict(_expected()),
                "sandbox_group_resource_id": (
                    "/subscriptions/ /resourceGroups/rg-agent/providers/Microsoft.App/"
                    "sandboxGroups/session-group"
                ),
            }
        ).encode("utf-8"),
        (
            b'{"manifest_version":1,"manifest_version":2,'
            b'"protocol_version":"v","session_id":"s","owner_hash_version":"o1",'
            b'"owner_hash":"o","app_hash":"a","sandbox_group_resource_id":"'
            + _GROUP.encode()
            + b'","sandbox_id":"sb","generation":1,"digest_kind":"k","digest":"d"}'
        ),
    ],
)
def test_manifest_parser_fails_closed_without_echoing_content(payload: bytes) -> None:
    with pytest.raises(SandboxManifestMismatchError) as exc_info:
        parse_sandbox_manifest_binding(payload)

    assert exc_info.value.fields == frozenset({"manifest"})
    assert payload.decode("utf-8", errors="replace") not in str(exc_info.value)
