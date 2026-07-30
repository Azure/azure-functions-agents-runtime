from __future__ import annotations

import hashlib
from dataclasses import FrozenInstanceError
from uuid import UUID

import pytest

import azure_functions_agents.session_state.identity as session_identity
from azure_functions_agents.session_state import (
    APP_CANONICALIZERS,
    OWNER_CANONICALIZERS,
    AppIdentity,
    AppIdentityResolutionError,
    EntraPrincipal,
    EntraUserOwnerContext,
    FunctionAppOwnerContext,
    FunctionAppPrincipal,
    OwnerPartition,
    OwnerResolutionError,
    SessionStateContractError,
    TriggerBindingOwnerContext,
    TriggerBindingPrincipal,
    compute_app_hash,
    compute_owner_hash,
    frame_canonical_components,
    hash_idempotency_key,
    idempotency_row_key,
    mint_run_id,
    mint_session_id,
    owner_partition,
    parse_row_key,
    resolve_function_app_identity,
    resolve_owner_context,
    run_row_key,
    session_row_key,
    validate_run_id,
    validate_session_id,
    verify_app_hash,
    verify_owner_hash,
)

_SUBSCRIPTION_ID = "11111111-2222-3333-4444-555555555555"
_TENANT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_OBJECT_ID = "01234567-89ab-cdef-0123-456789abcdef"
_PRODUCTION_APP_HASH = (
    "a1-bc3822ff85a843160dff861af0be268447dfb1b5ee6c55220268e2471c64e914"
)
_PRODUCTION_FUNCTION_OWNER_HASH = (
    "o1-dc2751e67656dea8efdadd4c85e1c89914bfa0419c4f308b3ec4806f7a7fff9b"
)
_PRODUCTION_ENTRA_OWNER_HASH = (
    "o1-d047f88d43aa3f12b03d754c0d318ae0dfe36f3e2c2c4c4002da83a55825d7dc"
)
_SLOT_APP_HASH = "a1-2f1a153c5f36a9e899e14fcbb900db41ac1e36aed491f1f0595a6c0541de3a9d"


def _app(*, slot_name: str | None = None) -> AppIdentity:
    return AppIdentity(
        subscription_id=_SUBSCRIPTION_ID,
        resource_group="Agents-RG",
        site_name="Agent-App",
        slot_name=slot_name,
    )


def test_app_and_owner_hash_golden_vectors() -> None:
    app = _app()
    function_owner = FunctionAppOwnerContext(app, "main")
    entra_owner = EntraUserOwnerContext(app, "main", _TENANT_ID, _OBJECT_ID)

    assert compute_app_hash(app) == _PRODUCTION_APP_HASH
    assert compute_owner_hash(function_owner) == _PRODUCTION_FUNCTION_OWNER_HASH
    assert compute_owner_hash(entra_owner) == _PRODUCTION_ENTRA_OWNER_HASH
    assert compute_app_hash(_app(slot_name="blue")) == _SLOT_APP_HASH


def test_v1_canonicalizers_do_not_depend_on_mutable_current_version_globals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app()
    owner = FunctionAppOwnerContext(app, "main")
    monkeypatch.setattr(session_identity, "APP_HASH_VERSION", "a2")
    monkeypatch.setattr(session_identity, "OWNER_HASH_VERSION", "o2")

    assert compute_app_hash(app, "a1") == _PRODUCTION_APP_HASH
    assert compute_owner_hash(owner, "o1") == _PRODUCTION_FUNCTION_OWNER_HASH


def test_case_normalization_preserves_stable_identity() -> None:
    upper = AppIdentity(
        subscription_id=_SUBSCRIPTION_ID.upper(),
        resource_group="AGENTS-RG",
        site_name="AGENT-APP",
        slot_name="PRODUCTION",
    )
    assert upper == _app()
    assert EntraPrincipal(_TENANT_ID.upper(), _OBJECT_ID.upper()) == EntraPrincipal(
        _TENANT_ID,
        _OBJECT_ID,
    )
    assert compute_app_hash(upper) == _PRODUCTION_APP_HASH


def test_canonical_framing_is_ordered_delimiter_safe_and_unicode_normalized() -> None:
    first = frame_canonical_components(("ab", "c"))
    second = frame_canonical_components(("a", "bc"))

    assert first.hex() == "000000020000000261620000000163"
    assert second.hex() == "000000020000000161000000026263"
    assert first != second
    assert frame_canonical_components(("", "Cafe\u0301")).hex() == (
        "000000020000000000000005436166c3a9"
    )
    assert frame_canonical_components(("a:b", "c")) != frame_canonical_components(
        ("a", "b:c")
    )


def test_owner_hash_verifies_under_stored_historical_version_without_migration() -> None:
    owner = FunctionAppOwnerContext(_app(), "main")
    legacy_bytes = frame_canonical_components(("function_app", "o0", "legacy"))
    expected = f"o0-{hashlib.sha256(legacy_bytes).hexdigest()}"
    canonicalizers = {
        "o0": lambda _owner: legacy_bytes,
        "o1": OWNER_CANONICALIZERS["o1"],
    }

    assert verify_owner_hash(
        owner,
        expected,
        "o0",
        canonicalizers=canonicalizers,
    )
    assert compute_owner_hash(owner) == _PRODUCTION_FUNCTION_OWNER_HASH
    assert compute_owner_hash(owner) != expected


def test_app_hash_verifies_under_stored_historical_version_without_migration() -> None:
    app = _app()
    legacy_bytes = frame_canonical_components(("app", "a0", "legacy"))
    expected = f"a0-{hashlib.sha256(legacy_bytes).hexdigest()}"
    canonicalizers = {
        "a0": lambda _app_identity: legacy_bytes,
        "a1": APP_CANONICALIZERS["a1"],
    }

    assert verify_app_hash(
        app,
        expected,
        "a0",
        canonicalizers=canonicalizers,
    )
    assert not verify_app_hash(
        app,
        expected,
        "a1",
        canonicalizers=canonicalizers,
    )
    assert compute_app_hash(app) == _PRODUCTION_APP_HASH
    assert compute_app_hash(app) != expected


@pytest.mark.parametrize("slot_value", [None, "", "Production", "pRoDuCtIoN"])
def test_function_app_identity_resolves_production_slot(
    slot_value: str | None,
) -> None:
    values = {
        "WEBSITE_OWNER_NAME": f"{_SUBSCRIPTION_ID}+westuswebspace",
        "WEBSITE_RESOURCE_GROUP": "Agents-RG",
        "WEBSITE_SITE_NAME": "Agent-App",
    }
    if slot_value is not None:
        values["WEBSITE_SLOT_NAME"] = slot_value

    assert resolve_function_app_identity(values.get) == _app()


def test_function_app_identity_resolves_named_slot_without_hostname() -> None:
    values = {
        "WEBSITE_OWNER_NAME": f"{_SUBSCRIPTION_ID}+westuswebspace",
        "WEBSITE_RESOURCE_GROUP": "Agents-RG",
        "WEBSITE_SITE_NAME": "Agent-App",
        "WEBSITE_SLOT_NAME": "Blue",
        "WEBSITE_HOSTNAME": "unstable.example.invalid",
    }

    identity = resolve_function_app_identity(values.get)

    assert identity == _app(slot_name="blue")
    assert identity.resource_id.endswith("/sites/agent-app/slots/blue")


@pytest.mark.parametrize(
    "missing",
    ["WEBSITE_OWNER_NAME", "WEBSITE_RESOURCE_GROUP", "WEBSITE_SITE_NAME"],
)
def test_function_app_identity_fails_closed_when_stable_input_is_missing(
    missing: str,
) -> None:
    values = {
        "WEBSITE_OWNER_NAME": f"{_SUBSCRIPTION_ID}+westuswebspace",
        "WEBSITE_RESOURCE_GROUP": "Agents-RG",
        "WEBSITE_SITE_NAME": "Agent-App",
    }
    del values[missing]

    with pytest.raises(AppIdentityResolutionError, match=missing):
        resolve_function_app_identity(values.get)


def test_function_app_identity_fails_closed_on_invalid_owner_name() -> None:
    values = {
        "WEBSITE_OWNER_NAME": _SUBSCRIPTION_ID,
        "WEBSITE_RESOURCE_GROUP": "Agents-RG",
        "WEBSITE_SITE_NAME": "Agent-App",
    }
    with pytest.raises(AppIdentityResolutionError, match="subscription prefix"):
        resolve_function_app_identity(values.get)


def test_function_app_identity_fails_closed_on_invalid_subscription_guid() -> None:
    values = {
        "WEBSITE_OWNER_NAME": "not-a-guid+westuswebspace",
        "WEBSITE_RESOURCE_GROUP": "Agents-RG",
        "WEBSITE_SITE_NAME": "Agent-App",
    }
    with pytest.raises(AppIdentityResolutionError, match="inputs are invalid"):
        resolve_function_app_identity(values.get)


def test_owner_resolution_is_explicit_and_trigger_binding_is_reserved() -> None:
    app = _app()
    function_owner = resolve_owner_context(app, "main", FunctionAppPrincipal())
    entra_owner = resolve_owner_context(
        app,
        "main",
        EntraPrincipal(_TENANT_ID, _OBJECT_ID),
    )

    assert isinstance(function_owner, FunctionAppOwnerContext)
    assert isinstance(entra_owner, EntraUserOwnerContext)
    with pytest.raises(OwnerResolutionError, match="could not be resolved"):
        resolve_owner_context(app, "main", None)
    with pytest.raises(OwnerResolutionError, match="reserved"):
        resolve_owner_context(app, "main", TriggerBindingPrincipal())
    with pytest.raises(OwnerResolutionError, match="reserved"):
        compute_owner_hash(TriggerBindingOwnerContext(app, "main"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", ""),
        ("tenant_id", "not-a-guid"),
        ("object_id", ""),
        ("object_id", "not-a-guid"),
    ],
)
def test_entra_principal_rejects_invalid_immutable_claims(
    field: str,
    value: str,
) -> None:
    values = {"tenant_id": _TENANT_ID, "object_id": _OBJECT_ID}
    values[field] = value
    with pytest.raises(SessionStateContractError, match=field):
        EntraPrincipal(**values)


def test_owner_partition_has_exact_discriminator_aware_shape() -> None:
    partition = owner_partition(FunctionAppOwnerContext(_app(), "main"))

    assert partition.partition_key == (
        f"o1:{_PRODUCTION_APP_HASH}:function_app:{_PRODUCTION_FUNCTION_OWNER_HASH}"
    )
    assert type(partition).parse(partition.partition_key) == partition


@pytest.mark.parametrize(
    "invalid",
    [
        "o1:a1-deadbeef:function_app",
        (
            "o1:o1-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:"
            "function_app:o1-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        ),
        (
            "o1:a1-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:"
            "function_app:o2-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        ),
    ],
)
def test_owner_partition_parser_rejects_ambiguous_or_inconsistent_keys(
    invalid: str,
) -> None:
    with pytest.raises(SessionStateContractError):
        OwnerPartition.parse(invalid)


def test_server_minted_ids_and_row_keys_round_trip() -> None:
    fixed_uuid = UUID("12345678-1234-5678-9abc-def012345678")
    session_id = mint_session_id(lambda: fixed_uuid)
    run_id = mint_run_id(lambda: fixed_uuid)

    assert session_id == "12345678123456789abcdef012345678"
    assert run_id == session_id
    for key in (
        session_row_key(session_id),
        run_row_key(session_id, run_id),
        idempotency_row_key(session_id, "rotate-me"),
    ):
        assert parse_row_key(str(key)) == key


@pytest.mark.parametrize(
    "invalid",
    ["run:s:r:extra", "session:a:b", "bogus:x", "idem:s"],
)
def test_row_key_parser_rejects_unknown_or_extra_components(invalid: str) -> None:
    with pytest.raises(SessionStateContractError):
        parse_row_key(invalid)


@pytest.mark.parametrize(
    "invalid",
    [":", "/", "\\", "#", "?", "\x00", "a" * 129, ""],
)
def test_table_key_identifiers_reject_forbidden_or_overlong_values(
    invalid: str,
) -> None:
    with pytest.raises(SessionStateContractError):
        validate_session_id(invalid)
    with pytest.raises(SessionStateContractError):
        validate_run_id(invalid)


def test_idempotency_key_is_hashed_and_never_retained_in_key_or_repr() -> None:
    raw_key = "rotate-me"
    row_key = idempotency_row_key("session-1", raw_key)

    assert hash_idempotency_key(raw_key) == (
        "467f61fcb0f1734bee6b3ee9933e43700c39357e00f420f70318239d2a128f34"
    )
    assert raw_key not in str(row_key)
    assert raw_key not in repr(row_key)
    with pytest.raises(SessionStateContractError):
        hash_idempotency_key("")
    with pytest.raises(SessionStateContractError):
        hash_idempotency_key("x" * 1025)
    assert len(hash_idempotency_key("x" * 1024)) == 64
    with pytest.raises(SessionStateContractError):
        hash_idempotency_key("\u00e9" * 513)


def test_identity_repr_and_hash_labels_redact_raw_claims() -> None:
    principal = EntraPrincipal(_TENANT_ID, _OBJECT_ID)
    owner = resolve_owner_context(_app(), "main", principal)
    label = compute_owner_hash(owner)

    for rendered in (repr(principal), repr(owner), label):
        assert _TENANT_ID not in rendered
        assert _OBJECT_ID not in rendered
    with pytest.raises(FrozenInstanceError):
        principal.tenant_id = "changed"  # type: ignore[misc]
