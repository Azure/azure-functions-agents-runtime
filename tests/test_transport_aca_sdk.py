"""Unit tests for the injected-factory ACA Sandbox SDK adapter."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from inspect import signature
from typing import Any

import aiohttp
import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError, ServiceRequestError

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
    SandboxEgressHeader,
    SandboxEgressHostRule,
    SandboxEgressPolicy,
    SandboxEgressRule,
    SandboxEgressRuleAction,
    SandboxEgressRuleMatch,
    SandboxEgressSecretRef,
    SandboxFileNotFoundError,
    SandboxFileOperationError,
    SandboxGroupBinding,
    SandboxGroupBindingError,
    SandboxLifecyclePolicy,
    SandboxProvisioningError,
    SandboxProvisioningLabels,
)
from tests.doubles.fake_aca_sdk import (
    FakeCredential,
    FakeSdkEgressPolicy,
    FakeSdkEnvironment,
    FakeSdkFileInfo,
    FakeSdkSnapshot,
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
        state_store_fingerprint="s1-" + ("c" * 52),
    )


def _request(**overrides: Any) -> SandboxCreateRequest:
    values: dict[str, Any] = {
        "source": DiskSource.create("runtime-bootstrap"),
        "labels": SandboxProvisioningLabels.create(
            owner_hash_version="o1",
            owner_kind="function_app",
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
            traffic_inspection="Full",
        ),
    }
    values.update(overrides)
    return SandboxCreateRequest.create(**values)


def test_real_b4_models_accept_rule_bearing_policy_projection() -> None:
    module_name = ".".join(("azure", "containerapps", "sandbox"))
    try:
        sdk = import_module(module_name)
        installed_version = version("azure-containerapps-sandbox")
    except (ImportError, PackageNotFoundError):
        pytest.skip("The optional ACA Sandbox SDK is not installed.")
    if installed_version != "0.1.0b4":
        pytest.skip("This regression runs only against the pinned ACA Sandbox SDK.")
    assert {
        name: tuple(signature(getattr(sdk, name)).parameters)
        for name in (
            "EgressPolicy",
            "EgressHostRule",
            "EgressRule",
            "EgressRuleMatch",
            "EgressRuleAction",
            "EgressHeader",
            "EgressHeaderValueRef",
            "EgressSecretRef",
        )
    } == {
        "EgressPolicy": ("default_action", "host_rules", "rules", "traffic_inspection"),
        "EgressHostRule": ("pattern", "action"),
        "EgressRule": ("name", "match", "action"),
        "EgressRuleMatch": ("host", "path", "methods"),
        "EgressRuleAction": ("type", "host", "path", "scheme", "headers"),
        "EgressHeader": ("operation", "name", "value", "value_ref"),
        "EgressHeaderValueRef": ("secret_ref", "managed_identity_ref"),
        "EgressSecretRef": ("secret_id", "secret_key", "format"),
    }
    secret_ref = SandboxEgressSecretRef.create(
        secret_id="mcp-token",
        secret_key="TOKEN",
        format="Bearer " + "{" + "value}",
    )
    policy = SandboxEgressPolicy.create(
        host_rules=(SandboxEgressHostRule.create(host="mcp.example.com", action="Allow"),),
        rules=(
            SandboxEgressRule.create(
                name="mcp-auth",
                match=SandboxEgressRuleMatch.create(
                    host="mcp.example.com",
                    path="/v1",
                    methods=("POST",),
                ),
                action=SandboxEgressRuleAction.create(
                    type="Transform",
                    headers=(
                        SandboxEgressHeader.create(
                            operation="Set",
                            name="Authorization",
                            secret_ref=secret_ref,
                        ),
                        SandboxEgressHeader.create(
                            operation="Set",
                            name="X-Static",
                            value="static-value",
                        ),
                    ),
                ),
            ),
            SandboxEgressRule.create(
                name="rewrite-route",
                match=SandboxEgressRuleMatch.create(host="old.example.com", path="/old"),
                action=SandboxEgressRuleAction.create(
                    type="Rewrite",
                    host="new.example.com",
                    path="/new",
                    scheme="https",
                ),
            ),
        ),
    )

    projected = aca_sdk._compile_egress_policy(aca_sdk._load_sdk_factories(), policy)

    assert projected.default_action == "Deny"
    assert projected.traffic_inspection == "Full"
    assert projected.host_rules[0].pattern == "mcp.example.com"
    assert projected.rules[0].match.methods == ["POST"]
    assert projected.rules[0].action.headers[0].value_ref.secret_ref.secret_id == "mcp-token"
    assert projected.rules[0].action.headers[1].value == "static-value"
    assert projected.rules[1].action.scheme == "https"


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
async def test_read_arm_group_sends_bearer_token_without_logging_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_headers: dict[str, str] = {}

    class _Response:
        status = 200

        async def __aenter__(self) -> _Response:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        async def json(self, *, content_type: object) -> dict[str, str]:
            del content_type
            return {"id": _GROUP_ID, "location": "westus2"}

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

        def get(self, *args: object, **kwargs: object) -> _Response:
            del args
            headers = kwargs["headers"]
            assert isinstance(headers, dict)
            captured_headers.update(headers)
            return _Response()

    monkeypatch.setattr(aca_sdk.aiohttp, "ClientSession", lambda **_: _Session())
    credential = FakeCredential()

    await aca_sdk._read_arm_group(credential, _GROUP_ID)

    assert captured_headers["Authorization"] == "Bearer test-token"


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
        "owner_kind": "function_app",
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
        traffic_inspection="Full",
    )
    assert environment.group_client.add_port_calls == 0

    await handle.close()
    await adapter.close()


@pytest.mark.asyncio
async def test_create_reuses_a_stable_operation_label_after_ambiguous_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())
    labels = SandboxProvisioningLabels.create(
        owner_hash_version="o1",
        owner_kind="function_app",
        owner_hash=_OWNER_HASH,
        app_hash=_APP_HASH,
        session_id="session-123",
        operation_label="op-session-123-1",
    )
    request = _request(labels=labels)

    first = await adapter.create(request, persisted_group=_binding())
    second = await adapter.create(request, persisted_group=_binding())

    assert first.identity.sandbox_id == second.identity.sandbox_id
    assert len(environment.group_client.create_calls) == 1
    assert environment.group_client.create_calls[0]["labels"]["operation_label"] == (
        "op-session-123-1"
    )
    await first.close()
    await second.close()
    await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch",
    ["app_hash", "owner_hash", "session_id", "unexpected_provider_label"],
)
async def test_create_rejects_stable_label_collision_with_foreign_binding(
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())
    labels = SandboxProvisioningLabels.create(
        owner_hash_version="o1",
        owner_kind="function_app",
        owner_hash=_OWNER_HASH,
        app_hash=_APP_HASH,
        session_id="session-123",
        operation_label="op-session-123-1",
    )
    foreign = environment.add_sandbox("foreign")
    foreign.labels = {
        **labels.to_provider_labels(),
        mismatch: f"other-{mismatch}",
    }

    with pytest.raises(SandboxProvisioningError, match="collision"):
        await adapter.create(_request(labels=labels), persisted_group=_binding())

    assert environment.group_client.create_calls == []
    await adapter.close()


@pytest.mark.asyncio
async def test_create_rejects_multiple_exact_stable_label_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())
    labels = SandboxProvisioningLabels.create(
        owner_hash_version="o1",
        owner_kind="function_app",
        owner_hash=_OWNER_HASH,
        app_hash=_APP_HASH,
        session_id="session-123",
        operation_label="op-session-123-1",
    )
    for sandbox_id in ("duplicate-a", "duplicate-b"):
        environment.add_sandbox(sandbox_id).labels = labels.to_provider_labels()

    with pytest.raises(SandboxProvisioningError, match="multiple"):
        await adapter.create(_request(labels=labels), persisted_group=_binding())

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
async def test_adapter_projects_complete_lifecycle_policy_without_group_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())
    handle = await adapter.create(_request(), persisted_group=_binding())

    policy = SandboxLifecyclePolicy.create(
        auto_suspend_seconds=None,
        auto_suspend_mode="Disk",
        auto_delete_seconds=90_300,
    )
    await handle.set_lifecycle_policy(policy)

    assert await handle.get_lifecycle_policy() == policy
    await handle.close()
    await adapter.close()


@pytest.mark.asyncio
async def test_adapter_projects_group_inventory_and_snapshot_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())
    handle = await adapter.create(_request(), persisted_group=_binding())
    environment.group_client.snapshots["snapshot-1"] = FakeSdkSnapshot(
        id="snapshot-1",
        sandbox_id=handle.identity.sandbox_id,
    )

    inventory = await adapter.list_sandboxes(labels={"session_id": "session-123"})
    snapshots = await adapter.list_snapshots()
    await adapter.delete_snapshot("snapshot-1")
    await adapter.delete_sandbox(handle.identity.sandbox_id)

    assert inventory[0].sandbox_id == handle.identity.sandbox_id
    assert snapshots[0].snapshot_id == "snapshot-1"
    assert environment.group_client.deleted_snapshot_ids == ["snapshot-1"]
    assert environment.group_client.deleted_sandbox_ids == [handle.identity.sandbox_id]
    await handle.close()
    await adapter.close()


@pytest.mark.asyncio
async def test_snapshot_delete_translates_sdk_failures_to_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())
    rejection = HttpResponseError("service rejected the request")
    rejection.status_code = 503

    async def rejected_delete_snapshot(_: str, **__: Any) -> None:
        raise rejection

    monkeypatch.setattr(
        environment.group_client,
        "begin_delete_snapshot",
        rejected_delete_snapshot,
    )

    with pytest.raises(SandboxProvisioningError, match="Snapshot delete failed"):
        await adapter.delete_snapshot("snapshot-1")

    await adapter.close()


@pytest.mark.asyncio
async def test_translate_file_errors_maps_resource_not_found_to_sandbox_file_not_found() -> None:
    """The pinned SDK raises ``ResourceNotFoundError`` for a missing path, not ``FileNotFoundError``."""

    async def _raise_resource_not_found() -> None:
        raise ResourceNotFoundError("no such file")

    with pytest.raises(SandboxFileNotFoundError):
        await aca_sdk._translate_file_errors(_raise_resource_not_found())


@pytest.mark.asyncio
async def test_translate_file_errors_maps_http_response_error_to_sandbox_file_operation_error() -> (
    None
):
    async def _raise_http_response_error() -> None:
        error = HttpResponseError("service rejected the request")
        error.status_code = 503
        raise error

    with pytest.raises(SandboxFileOperationError) as exc_info:
        await aca_sdk._translate_file_errors(_raise_http_response_error())

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_translate_file_errors_maps_service_request_error_to_sandbox_file_operation_error() -> (
    None
):
    async def _raise_service_request_error() -> None:
        raise ServiceRequestError("network failure before any response")

    with pytest.raises(SandboxFileOperationError) as exc_info:
        await aca_sdk._translate_file_errors(_raise_service_request_error())

    assert exc_info.value.status_code is None


@pytest.mark.asyncio
async def test_read_file_surfaces_a_resource_not_found_error_as_sandbox_file_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An end-to-end proof through the real handle, not just the translator helper."""
    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())
    handle = await adapter.create(_request(), persisted_group=_binding())

    async def _raise_resource_not_found(_path: str) -> bytes:
        raise ResourceNotFoundError("no such file")

    monkeypatch.setattr(
        environment.sandboxes[handle.identity.sandbox_id], "read_file", _raise_resource_not_found
    )

    with pytest.raises(SandboxFileNotFoundError):
        await handle.read_file("/missing.txt")

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


def test_create_request_rejects_ports_and_unsafe_egress_without_name_filtering() -> None:
    with pytest.raises(SandboxProvisioningError, match="inbound ports"):
        _request(ports=("tcp/80",))

    with pytest.raises(SandboxProvisioningError, match="egress"):
        _request(
            egress_policy=SandboxEgressPolicy.create(default_action="Allow")  # type: ignore[arg-type]
        )

    request = _request(environment={"AZURE_CLIENT_SECRET": "explicit-value"})
    assert request.environment == {"AZURE_CLIENT_SECRET": "explicit-value"}

    with pytest.raises(SandboxProvisioningError, match="egress proxy"):
        _request(skip_egress_proxy=True)


def test_create_request_requires_positive_explicit_setup_budget() -> None:
    with pytest.raises(SandboxProvisioningError, match="positive"):
        _request(remaining_setup_budget_seconds=0)


@pytest.mark.asyncio
async def test_snapshot_like_source_is_rejected_before_adapter_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SnapshotLikeSource:
        snapshot_id = "snapshot-1"

    environment = FakeSdkEnvironment()
    _install_fake_adapter_boundary(monkeypatch, environment)
    adapter = await aca_sdk.AcaSandboxAdapter.open(_GROUP_ID, persisted_group=_binding())

    with pytest.raises(SandboxProvisioningError, match="exactly one"):
        _request(
            source=SnapshotLikeSource(),
            environment={"HARNESS_MODE": "would-not-project"},
            entrypoint=("would-not-project",),
            cmd=("would-not-project",),
        )

    assert environment.group_client.create_calls == []
    assert environment.sandboxes == {}
    await adapter.close()


@pytest.mark.parametrize(
    "field_name",
    ["owner_hash_version", "owner_kind", "owner_hash", "app_hash", "session_id"],
)
def test_provisioning_labels_reject_values_over_aca_limit(field_name: str) -> None:
    values = {
        "owner_hash_version": "o1",
        "owner_kind": "function_app",
        "owner_hash": _OWNER_HASH,
        "app_hash": _APP_HASH,
        "session_id": "session-123",
    }
    values[field_name] = "x" * 64

    with pytest.raises(SandboxProvisioningError, match="63 characters"):
        SandboxProvisioningLabels.create(**values)


def test_file_projections_accept_live_numeric_posix_mode() -> None:
    file_info = FakeSdkFileInfo(
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
