"""Security-focused tests for the state-row/live-manifest handshake."""

from __future__ import annotations

import json
from dataclasses import asdict, replace

import pytest

from azure_functions_agents.journal_paths import SESSION_MANIFEST_PATH as JOURNAL_MANIFEST_PATH
from azure_functions_agents.transport.manifest import (
    SESSION_MANIFEST_PATH,
    ExpectedSandboxManifestBinding,
    SandboxManifestMismatchError,
    parse_sandbox_manifest_binding,
    render_sandbox_manifest_binding,
    verify_sandbox_manifest,
)
from azure_functions_agents.transport.transport_models import ProvisionedSandboxIdentity

_GROUP = (
    "/subscriptions/sub-123/resourceGroups/rg-agent/"
    "providers/Microsoft.App/sandboxGroups/session-group"
)
_STATE_STORE_FINGERPRINT = "s1-" + ("f" * 52)


def test_manifest_path_is_reexported_from_central_journal_paths() -> None:
    assert SESSION_MANIFEST_PATH == JOURNAL_MANIFEST_PATH


def _expected() -> ExpectedSandboxManifestBinding:
    return ExpectedSandboxManifestBinding.create(
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
        state_store_fingerprint=_STATE_STORE_FINGERPRINT,
    )


def _observed_payload(expected: ExpectedSandboxManifestBinding) -> bytes:
    return json.dumps(asdict(expected), sort_keys=True).encode("utf-8")


def _live_identity(expected: ExpectedSandboxManifestBinding) -> ProvisionedSandboxIdentity:
    return ProvisionedSandboxIdentity.create(
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
        ("state_store_fingerprint", "s1-" + ("e" * 52)),
        ("manifest_version", 2),
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
    assert _STATE_STORE_FINGERPRINT not in rendered


def test_manifest_verifier_rejects_repointed_live_sandbox_even_if_manifest_matches() -> None:
    expected = _expected()
    observed = parse_sandbox_manifest_binding(_observed_payload(expected))
    repointed_live = ProvisionedSandboxIdentity.create(
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
    repointed_live = ProvisionedSandboxIdentity.create(
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


def test_manifest_verifier_rejects_stale_fingerprint_even_when_row_and_manifest_agree() -> None:
    """A caller that stamps the currently resolved fingerprint (not a stale row
    copy) into ``expected`` catches a manifest that only agrees with an old row.
    """
    expected = replace(_expected(), state_store_fingerprint="s1-" + ("c" * 52))
    stale_manifest = replace(_expected(), state_store_fingerprint="s1-" + ("a" * 52))
    observed = parse_sandbox_manifest_binding(_observed_payload(stale_manifest))

    with pytest.raises(SandboxManifestMismatchError) as exc_info:
        verify_sandbox_manifest(expected, observed, _live_identity(expected))

    assert exc_info.value.fields == frozenset({"state_store_fingerprint"})


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"[]",
        b'{"session_id": "partial"}',
        json.dumps({**asdict(_expected()), "generation": "4"}).encode("utf-8"),
        json.dumps({**asdict(_expected()), "generation": True}).encode("utf-8"),
        json.dumps({**asdict(_expected()), "generation": -1}).encode("utf-8"),
        json.dumps({**asdict(_expected()), "session_id": ""}).encode("utf-8"),
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
        # Missing state_store_fingerprint entirely.
        json.dumps(
            {k: v for k, v in asdict(_expected()).items() if k != "state_store_fingerprint"}
        ).encode("utf-8"),
        # Wrong type (int instead of string).
        json.dumps({**asdict(_expected()), "state_store_fingerprint": 12345}).encode("utf-8"),
        # Empty string.
        json.dumps({**asdict(_expected()), "state_store_fingerprint": ""}).encode("utf-8"),
        # Duplicate state_store_fingerprint key with a different second value.
        (
            b'{"manifest_version":1,'
            b'"protocol_version":"v","session_id":"s","owner_hash_version":"o1",'
            b'"owner_hash":"o","app_hash":"a","sandbox_group_resource_id":"'
            + _GROUP.encode()
            + b'","sandbox_id":"sb","generation":1,"digest_kind":"k","digest":"d",'
            b'"state_store_fingerprint":"' + _STATE_STORE_FINGERPRINT.encode() + b'",'
            b'"state_store_fingerprint":"s1-' + (b"z" * 52) + b'"}'
        ),
    ],
)
def test_manifest_parser_fails_closed_without_echoing_content(payload: bytes) -> None:
    with pytest.raises(SandboxManifestMismatchError) as exc_info:
        parse_sandbox_manifest_binding(payload)

    assert exc_info.value.fields == frozenset({"manifest"})
    assert payload.decode("utf-8", errors="replace") not in str(exc_info.value)


def test_canonical_renderer_round_trips_through_the_strict_parser() -> None:
    expected = _expected()

    rendered = render_sandbox_manifest_binding(expected)
    observed = parse_sandbox_manifest_binding(rendered)

    verify_sandbox_manifest(expected, observed, _live_identity(expected))
    assert rendered.endswith(b"\n")
    assert rendered.count(b"\n") == 1
    assert b" " not in rendered.rstrip(b"\n")


def test_canonical_renderer_output_is_deterministic_regardless_of_field_order() -> None:
    expected = _expected()

    first = render_sandbox_manifest_binding(expected)
    second = render_sandbox_manifest_binding(replace(expected))

    assert first == second
