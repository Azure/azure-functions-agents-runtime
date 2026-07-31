"""Unit tests for the injected-factory ACA Sandbox SDK adapter."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace
from typing import Any

import aiohttp
import pytest
from azure.core.exceptions import HttpResponseError, ServiceRequestError

from azure_functions_agents.transport import aca_sdk
from azure_functions_agents.transport.manifest import (
    SESSION_MANIFEST_PATH,
    ExpectedSandboxManifestBinding,
    SandboxManifestMismatchError,
)
from azure_functions_agents.transport.transport_models import (
    AcaSandboxDependencyError,
    DiskIdSource,
    DiskSource,
    PersistedSandboxBinding,
    PresetSource,
    SandboxCreateRequest,
    SandboxEgressPolicy,
    SandboxGroupBinding,
    SandboxGroupBindingError,
    SandboxProvisioningError,
    SandboxProvisioningLabels,
)
from tests.doubles.fake_aca_sdk import (
    FakeCredential,
    FakeSdkEgressPolicy,
    FakeSdkEnvironment,
)

_GROUP_ID = (
    "/subscriptions/sub-123/resourceGroups/rg-agent/"
    "providers/Microsoft.App/sandboxGroups/session-group"
)
_APP_HASH = "a1-" + ("b" * 52)
_OWNER_HASH = "o1-" + ("a" * 52)


def _binding(*, region: str = "westus2") -> SandboxGroupBinding:
    return SandboxGroupBinding.create(resource_id=_GROUP_ID, region=region)


def _expected(sandbox_id: str) -> ExpectedSandboxManifestBinding:
    return ExpectedSandboxManifestBinding.create(
        manifest_version=1,
        protocol_version="maf-session-v1",
        session_id="session-123",
        owner_hash_version="o1",
        owner_hash=_OWNER_HASH,
        app_hash=_APP_HASH,
        sandbox_group_resource_id=_GROUP_ID,
        sandbox_id=sandbox_id,
        generation=1,
        digest_kind="funcs_zip",
        digest="sha256:content-fingerprint",
    )


def _request(**overrides: Any) -> SandboxCreateRequest:
    values: dict[str, Any] = {
        "source": DiskSource.create("runtime-bootstrap"),
        "labels": SandboxProvisioningLabels.create(
            owner_hash_version="o1",
            owner_hash=_OWNER_HASH,
            app_hash=_APP_HASH,
            session_id="session-123",
        ),
        "remaining_setup_budget_seconds": 45.0,
        "environment": {"HARNESS_MODE": "test"},
        "entrypoint": ("python",),
        "cmd": ("-m", "harness"),
        "egress_policy": SandboxEgressPolicy.create(
            default_action="Deny",
            traffic_inspection="Partial",
        ),
    }
    values.update(overrides)
    return SandboxCreateRequest.create(**values)


def _install_fake_adapter_boundary(
    monkeypatch: pytest.MonkeyPatch, environment: FakeSdkEnvironment
) -> FakeCredential:
    credential = FakeCredential()

    async def arm_reader(_: object, resource_id: str) -> dict[str, object]:
        return {"id": resource_id, "location": "WestUS2"}

    monkeypatch.setattr(aca_sdk, "_SDK_FACTORIES", environment.factories)
    monkeypatch.setattr(aca_sdk, "_CREDENTIAL_FACTORY", lambda: credential)
    monkeypatch.setattr(aca_sdk, "_ARM_GROUP_READER", arm_reader)
    return credential


@pytest.mark.asyncio
async def test_open_resolves_customer_group_and_constructs_one_data_plane_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = FakeSdkEnvironment()
    credential = _install_fake_adapter_boundary(monkeypatch, environment)

    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())

    assert adapter.group.resource_id == (
        "/subscriptions/sub-123/resourceGroups/rg-agent/"
        "providers/Microsoft.App/sandboxGroups/session-group"
    )
    assert adapter.group.region == "westus2"
    assert environment.group_client.constructor_kwargs == {
        "subscription_id": "sub-123",
        "resource_group": "rg-agent",
        "sandbox_group": "session-group",
    }
    assert environment.endpoint_regions == ["westus2"]

    await adapter.close()

    assert environment.group_client.closed
    assert credential.closed


@pytest.mark.asyncio
async def test_read_arm_group_uses_an_explicit_timeout_and_translates_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_read_arm_group`` must bound its outbound call and never leak raw aiohttp errors."""

    session_kwargs: dict[str, Any] = {}

    class _FailingSession:
        def __init__(self, **kwargs: Any) -> None:
            session_kwargs.update(kwargs)

        async def __aenter__(self) -> _FailingSession:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        def get(self, *args: Any, **kwargs: Any) -> Any:
            raise aiohttp.ClientConnectionError("connection refused")

    monkeypatch.setattr(aca_sdk.aiohttp, "ClientSession", _FailingSession)
    credential = FakeCredential()

    with pytest.raises(SandboxGroupBindingError, match="transport or decode error"):
        await aca_sdk._read_arm_group(credential, "/subscriptions/sub-123/resourceGroups/rg-agent")

    assert isinstance(session_kwargs.get("timeout"), aiohttp.ClientTimeout)


@pytest.mark.asyncio
async def test_create_passes_explicit_safe_values_and_returns_only_session_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())

    handle = await adapter.create(_request(), persisted_group=_binding())

    call = environment.group_client.create_calls[0]
    assert handle.identity.sandbox_id == "created-1"
    assert {key for key in call if key in {"disk", "disk_id", "preset"}} == {"disk"}
    assert call["disk"] == "runtime-bootstrap"
    assert call["ports"] == []
    assert call["skip_egress_proxy"] is False
    assert call["polling_timeout"] == 30.0
    assert call["polling_interval"] == 3
    assert {
        key: value
        for key, value in call["labels"].items()
        if key != "provisioning_attempt_id"
    } == {
        "owner_hash_version": "o1",
        "owner_hash": _OWNER_HASH,
        "app_hash": _APP_HASH,
        "session_id": "session-123",
    }
    assert len(call["labels"]["provisioning_attempt_id"]) == 32
    assert call["environment"] == {"HARNESS_MODE": "test"}
    assert call["entrypoint"] == ["python"]
    assert call["cmd"] == ["-m", "harness"]
    assert call["egress_policy"] == FakeSdkEgressPolicy(
        default_action="Deny",
        traffic_inspection="Partial",
    )
    assert environment.group_client.add_port_calls == 0

    await handle.close()
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "source_key"),
    [
        (DiskSource.create("runtime-bootstrap"), "disk"),
        (DiskIdSource.create("disk-id"), "disk_id"),
        (PresetSource.create("copilot"), "preset"),
    ],
)
async def test_create_accepts_each_single_explicit_source_and_forwards_remaining_budget(
    monkeypatch: pytest.MonkeyPatch,
    source: DiskSource | DiskIdSource | PresetSource,
    source_key: str,
) -> None:
    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())

    handle = await adapter.create(
        _request(source=source, remaining_setup_budget_seconds=12.5),
        persisted_group=_binding(),
    )

    call = environment.group_client.create_calls[0]
    source_keys = {"disk", "disk_id", "preset"}
    assert {key for key in call if key in source_keys} == {source_key}
    # The real SDK's polling_timeout is int-typed; the adapter rounds the
    # fractional budget up to the next whole second (never under-delivers it).
    assert call["polling_timeout"] == 13

    await handle.close()
    await adapter.close()


@pytest.mark.asyncio
async def test_create_reconciles_and_deletes_remote_sandbox_when_polling_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())
    environment.group_client.create_result_error = TimeoutError("polling timed out")

    with pytest.raises(TimeoutError, match="polling timed out"):
        await adapter.create(_request(), persisted_group=_binding())

    assert environment.group_client.deleted_sandbox_ids == ["created-1"]
    assert environment.sandboxes == {}
    await adapter.close()


@pytest.mark.asyncio
async def test_create_preserves_cancellation_when_cleanup_cannot_find_a_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())

    async def cancelled_create(**_: Any) -> None:
        raise asyncio.CancelledError

    async def no_delay(_: float) -> None:
        return None

    monkeypatch.setattr(environment.group_client, "begin_create_sandbox", cancelled_create)
    monkeypatch.setattr(aca_sdk, "_sleep", no_delay)

    with pytest.raises(asyncio.CancelledError):
        await adapter.create(_request(), persisted_group=_binding())

    assert environment.sandboxes == {}
    await adapter.close()


@pytest.mark.asyncio
async def test_create_preserves_definitive_request_rejection_without_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())
    rejection = HttpResponseError("request rejected")
    rejection.status_code = 400

    async def rejected_create(**_: Any) -> None:
        raise rejection

    async def unexpected_cleanup(_: str) -> None:
        raise AssertionError("A definitive request rejection cannot have created a sandbox")

    monkeypatch.setattr(environment.group_client, "begin_create_sandbox", rejected_create)
    monkeypatch.setattr(adapter, "_cleanup_failed_create", unexpected_cleanup)

    with pytest.raises(HttpResponseError, match="request rejected"):
        await adapter.create(_request(), persisted_group=_binding())

    assert environment.sandboxes == {}
    await adapter.close()


@pytest.mark.asyncio
async def test_create_preserves_original_error_when_cleanup_confirms_nothing_was_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful cleanup list that finds no sandbox must not mask the real failure.

    Regression test for the pre-acceptance masking bug: a non-HTTP
    ``AzureError`` (the request never reached the service) used to be
    replaced by ``_cleanup_failed_create``'s own reconciliation error once
    its bounded list retries confirmed nothing existed to delete.
    """

    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())

    async def unreachable_create(**_: Any) -> None:
        raise ServiceRequestError("network unreachable before create was accepted")

    async def no_delay(_: float) -> None:
        return None

    monkeypatch.setattr(environment.group_client, "begin_create_sandbox", unreachable_create)
    monkeypatch.setattr(aca_sdk, "_sleep", no_delay)

    with pytest.raises(ServiceRequestError, match="network unreachable"):
        await adapter.create(_request(), persisted_group=_binding())

    assert environment.sandboxes == {}
    await adapter.close()


@pytest.mark.asyncio
async def test_create_raises_reconciliation_error_when_cleanup_list_itself_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup list call that itself fails cannot confirm anything either way.

    Unlike a successful-but-empty list (previous test), this case genuinely
    cannot tell whether a sandbox was created, so the explicit reconciliation
    error is the correct, fail-closed signal to surface.
    """

    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())

    async def unreachable_create(**_: Any) -> None:
        raise ServiceRequestError("network unreachable before create was accepted")

    def failing_list_sandboxes(**_: Any) -> None:
        raise ServiceRequestError("cleanup list call itself failed")

    monkeypatch.setattr(environment.group_client, "begin_create_sandbox", unreachable_create)
    monkeypatch.setattr(environment.group_client, "list_sandboxes", failing_list_sandboxes)

    with pytest.raises(SandboxProvisioningError, match="could not be reconciled"):
        await adapter.create(_request(), persisted_group=_binding())

    await adapter.close()


@pytest.mark.asyncio
async def test_adapter_maps_all_six_file_operations_directly_and_keeps_exec_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())
    handle = await adapter.create(_request(), persisted_group=_binding())

    await handle.mkdir("/journal")
    await handle.write_file("/journal/input.bin", b"\x00payload")
    listed = await handle.list_files("/journal")
    stat = await handle.stat_file("/journal/input.bin")
    read = await handle.read_file("/journal/input.bin")
    await handle.delete_file("/journal/input.bin")
    result = await handle.exec("harness --status", timeout_seconds=1)

    assert listed[0].name == "input.bin"
    assert stat.size == len(b"\x00payload")
    assert read == b"\x00payload"
    assert result.exit_code == 0
    assert [
        call.operation for call in environment.sandboxes[handle.identity.sandbox_id].calls
    ] == [
        "mkdir",
        "write_file",
        "list_files",
        "stat_file",
        "read_file",
        "delete_file",
        "exec",
    ]

    await handle.close()
    await adapter.close()


@pytest.mark.asyncio
async def test_attach_uses_direct_manifest_read_before_any_advisory_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    sandbox = environment.add_sandbox("persisted-1")
    expected = _expected(sandbox.sandbox_id)
    sandbox.transport.seed_file(SESSION_MANIFEST_PATH, json.dumps(asdict(expected)).encode())
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())

    handle = await adapter.attach(
        PersistedSandboxBinding.create(sandbox_id=sandbox.sandbox_id, group=_binding()),
        expected,
        readiness_timeout_seconds=1,
    )

    assert handle.identity.sandbox_id == sandbox.sandbox_id
    assert [call.operation for call in sandbox.calls] == ["read_file"]
    assert not sandbox.closed

    await handle.close()
    await adapter.close()


@pytest.mark.asyncio
async def test_resume_requires_manifest_handshake_after_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    sandbox = environment.add_sandbox("persisted-1")
    expected = _expected(sandbox.sandbox_id)
    sandbox.transport.seed_file(SESSION_MANIFEST_PATH, json.dumps(asdict(expected)).encode())
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())

    handle = await adapter.resume(
        PersistedSandboxBinding.create(sandbox_id=sandbox.sandbox_id, group=_binding()),
        expected,
        readiness_timeout_seconds=1,
    )

    assert [call.operation for call in sandbox.calls] == ["resume", "read_file"]

    await handle.delete()
    await handle.close()
    await adapter.close()


@pytest.mark.asyncio
async def test_resume_closes_handle_when_the_resume_call_itself_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing ``resume()`` call must not leak the handle or its SDK client.

    Regression test: every sibling path (``open()``, a persisted-ID mismatch
    in ``_make_handle``, and ``_verify_manifest_handshake``'s ``finally``)
    already closes correctly on failure; ``resume()`` was the one asymmetric
    path that attached a handle and then leaked it if ``resume()`` raised.
    """

    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    sandbox = environment.add_sandbox("persisted-1")
    expected = _expected(sandbox.sandbox_id)

    async def failing_resume() -> None:
        raise ServiceRequestError("resume rejected")

    monkeypatch.setattr(sandbox, "resume", failing_resume)
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())

    with pytest.raises(ServiceRequestError, match="resume rejected"):
        await adapter.resume(
            PersistedSandboxBinding.create(sandbox_id=sandbox.sandbox_id, group=_binding()),
            expected,
            readiness_timeout_seconds=1,
        )

    assert sandbox.closed
    await adapter.close()


@pytest.mark.asyncio
async def test_resume_retries_direct_manifest_until_nonblocking_resume_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    sandbox = environment.add_sandbox("persisted-1")
    expected = _expected(sandbox.sandbox_id)
    sandbox.transport.seed_file(SESSION_MANIFEST_PATH, json.dumps(asdict(expected)).encode())
    sandbox.transport.read_errors.append(ServiceRequestError("data plane not ready"))

    async def no_delay(_: float) -> None:
        return None

    monkeypatch.setattr(aca_sdk, "_sleep", no_delay)
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())

    handle = await adapter.resume(
        PersistedSandboxBinding.create(sandbox_id=sandbox.sandbox_id, group=_binding()),
        expected,
        readiness_timeout_seconds=1,
    )

    assert [call.operation for call in sandbox.calls] == [
        "resume",
        "read_file",
        "read_file",
    ]
    await handle.close()
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["attach", "resume"])
async def test_readiness_timeout_is_validated_before_constructing_a_handle(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    sandbox = environment.add_sandbox("persisted-1")
    expected = _expected(sandbox.sandbox_id)
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())
    persisted = PersistedSandboxBinding.create(sandbox_id=sandbox.sandbox_id, group=_binding())

    with pytest.raises(SandboxProvisioningError, match="readiness_timeout_seconds"):
        if operation == "attach":
            await adapter.attach(persisted, expected, readiness_timeout_seconds=0)
        else:
            await adapter.resume(persisted, expected, readiness_timeout_seconds=0)

    assert environment.sandbox_client_ids == []
    assert sandbox.calls == []
    await adapter.close()


@pytest.mark.asyncio
async def test_attach_closes_suspect_handle_on_forged_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    sandbox = environment.add_sandbox("persisted-1")
    expected = _expected(sandbox.sandbox_id)
    forged = {**asdict(expected), "digest": "sha256:forged"}
    sandbox.transport.seed_file(SESSION_MANIFEST_PATH, json.dumps(forged).encode())
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())

    with pytest.raises(SandboxManifestMismatchError, match="digest"):
        await adapter.attach(
            PersistedSandboxBinding.create(sandbox_id=sandbox.sandbox_id, group=_binding()),
            expected,
            readiness_timeout_seconds=1,
        )

    assert sandbox.closed
    await adapter.close()


@pytest.mark.asyncio
async def test_adapter_rejects_repointed_group_or_region_before_creating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = FakeSdkEnvironment()
    credential = _install_fake_adapter_boundary(monkeypatch, environment)

    with pytest.raises(SandboxGroupBindingError, match="region"):
        await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding(region="eastus"))

    assert environment.group_clients == []
    assert credential.closed


@pytest.mark.asyncio
async def test_adapter_rejects_repointed_arm_identity_before_constructing_group_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = FakeSdkEnvironment()
    credential = FakeCredential()

    async def arm_reader(_: object, resource_id: str) -> dict[str, object]:
        return {
            "id": resource_id.replace("session-group", "repointed-group"),
            "location": "westus2",
        }

    monkeypatch.setattr(aca_sdk, "_SDK_FACTORIES", environment.factories)
    monkeypatch.setattr(aca_sdk, "_CREDENTIAL_FACTORY", lambda: credential)
    monkeypatch.setattr(aca_sdk, "_ARM_GROUP_READER", arm_reader)

    with pytest.raises(SandboxGroupBindingError, match="ARM-resolved"):
        await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID)

    assert environment.group_clients == []
    assert credential.closed


@pytest.mark.asyncio
async def test_attach_rejects_a_live_handle_repointed_from_the_persisted_sandbox_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    sandbox = environment.add_sandbox("persisted-1")
    sandbox.sandbox_id = "repointed-1"
    expected = _expected("persisted-1")
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())

    with pytest.raises(SandboxGroupBindingError, match="Live Sandbox handle ID"):
        await adapter.attach(
            PersistedSandboxBinding.create(sandbox_id="persisted-1", group=_binding()),
            expected,
            readiness_timeout_seconds=1,
        )

    assert sandbox.closed
    await adapter.close()


@pytest.mark.asyncio
async def test_missing_optional_sdk_fails_before_constructing_or_using_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_constructed = False

    def missing_sdk() -> aca_sdk.SdkFactories:
        raise AcaSandboxDependencyError("install aca_sandbox")

    def credential_factory() -> FakeCredential:
        nonlocal credential_constructed
        credential_constructed = True
        return FakeCredential()

    monkeypatch.setattr(aca_sdk, "_SDK_FACTORIES", missing_sdk)
    monkeypatch.setattr(aca_sdk, "_CREDENTIAL_FACTORY", credential_factory)

    with pytest.raises(AcaSandboxDependencyError):
        await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID)

    assert not credential_constructed


def test_create_request_rejects_ports_unsafe_egress_and_controller_credentials() -> None:
    with pytest.raises(SandboxProvisioningError, match="inbound ports"):
        _request(ports=("tcp/80",))

    with pytest.raises(SandboxProvisioningError, match="egress"):
        _request(
            egress_policy=SandboxEgressPolicy.create(default_action="Allow")  # type: ignore[arg-type]
        )

    with pytest.raises(SandboxProvisioningError, match="credentials"):
        _request(environment={"AZURE_CLIENT_SECRET": "not-allowed"})

    with pytest.raises(SandboxProvisioningError, match="egress proxy"):
        _request(skip_egress_proxy=True)


def test_create_request_requires_positive_explicit_setup_budget() -> None:
    with pytest.raises(SandboxProvisioningError, match="positive"):
        _request(remaining_setup_budget_seconds=0)


@pytest.mark.parametrize(
    "field_name",
    ["owner_hash_version", "owner_hash", "app_hash", "session_id"],
)
def test_provisioning_labels_reject_values_over_aca_limit(field_name: str) -> None:
    values = {
        "owner_hash_version": "o1",
        "owner_hash": _OWNER_HASH,
        "app_hash": _APP_HASH,
        "session_id": "session-123",
    }
    values[field_name] = "x" * 64

    with pytest.raises(SandboxProvisioningError, match="63 characters"):
        SandboxProvisioningLabels.create(**values)


def test_file_projections_accept_live_numeric_posix_mode() -> None:
    # Reproduce the SDK's own annotation defect (FileInfo.mode is typed
    # str | None, but the wire actually sends an int) with a duck-typed
    # stand-in shaped like the real FileInfo response, without importing the
    # optional preview SDK from a test module (see test_transport_import_graph).
    file_info = SimpleNamespace(
        name="file.bin",
        path="/tmp/file.bin",
        size=7,
        is_directory=False,
        modified_at=None,
        mode=0o644,
    )

    entry = aca_sdk._project_file_entry(file_info)
    stat = aca_sdk._project_file_stat(file_info)

    assert entry.mode == 0o644
    assert stat.mode == 0o644
