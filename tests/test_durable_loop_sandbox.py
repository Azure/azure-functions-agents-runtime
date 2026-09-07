from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from azure_functions_agents.controller.package import CapturedContentPackage
from azure_functions_agents.experimental.durable_loop_activities import (
    InMemoryDurableContentStore,
    get_protocol_model,
)
from azure_functions_agents.experimental.durable_loop_config import DurableLoopSettings
from azure_functions_agents.experimental.durable_loop_protocol import (
    DurableFaultProfile,
    SandboxExecutionProfile,
    ToolBehavior,
    ToolProvenance,
    ToolRequestV1,
    ToolResultStatus,
    WorkspaceArtifactV1,
    canonical_hash,
    tool_request_hash,
)
from azure_functions_agents.experimental.durable_loop_receipts import (
    ActivityReceiptStatus,
    ActivityReceiptV1,
    DurableOneShotFaults,
    InMemoryDurableKeyedDocumentStore,
    create_activity_receipt,
)
from azure_functions_agents.experimental.durable_loop_sandbox import (
    DurableAcaSandboxLane,
    DurableSandboxCapacityCoordinator,
)
from azure_functions_agents.experimental.hybrid_config import HybridSandboxSettings
from azure_functions_agents.experimental.hybrid_executor import (
    export_workspace_archive,
    restore_workspace_archive,
)
from azure_functions_agents.experimental.hybrid_protocol import (
    HYBRID_TOOL_PROTOCOL_VERSION,
    HybridInvocationStatus,
    HybridToolDescriptor,
    HybridToolInternalTimings,
    HybridToolInvocationResult,
    HybridToolManifest,
    HybridToolProvenance,
    canonical_hybrid_json_bytes,
)
from azure_functions_agents.experimental.hybrid_tools import InvocationSandboxLease
from azure_functions_agents.transport.manifest import ExpectedSandboxManifestBinding
from azure_functions_agents.transport.transport_models import (
    PersistedSandboxBinding,
    SandboxGroupBinding,
)


def _package() -> CapturedContentPackage:
    payload = b"package"
    return CapturedContentPackage.create(
        archive_bytes=payload,
        digest_kind="funcs_zip",
        digest="sha256:" + hashlib.sha256(payload).hexdigest(),
    )


def _manifest() -> HybridToolManifest:
    return HybridToolManifest(
        protocol_version=HYBRID_TOOL_PROTOCOL_VERSION,
        tools=(
            HybridToolDescriptor(
                name="write_file",
                description="Write a file.",
                parameters={"additionalProperties": True, "type": "object"},
                provenance=HybridToolProvenance.GENERIC,
            ),
        ),
    )


class _Lease:
    def __init__(self, generation: int) -> None:
        self.manifest = _manifest()
        self.package = _package()
        self.expected_manifest = ExpectedSandboxManifestBinding.create(
            manifest_version=1,
            protocol_version=HYBRID_TOOL_PROTOCOL_VERSION,
            session_id="session-1",
            owner_hash_version="h1",
            owner_hash="owner",
            app_hash="h1-app",
            sandbox_group_resource_id=(
                "/subscriptions/s/resourceGroups/r/providers/"
                "Microsoft.App/sandboxGroups/g"
            ),
            sandbox_id=f"sandbox-{generation}",
            generation=generation,
            digest_kind=self.package.digest_kind,
            digest=self.package.digest,
            state_store_fingerprint="s1-state",
        )
        self.persisted_binding = PersistedSandboxBinding.create(
            f"sandbox-{generation}",
            SandboxGroupBinding.create(
                self.expected_manifest.sandbox_group_resource_id,
                "eastus2",
            ),
        )
        self.restored: list[bytes] = []
        self.deleted = 0
        self.retained = 0
        self.invocations = 0

    async def restore_workspace(self, archive: bytes, digest: str) -> None:
        assert digest == "sha256:" + hashlib.sha256(archive).hexdigest()
        self.restored.append(archive)

    async def export_workspace(self) -> tuple[bytes, str]:
        archive = f"workspace-{self.expected_manifest.generation}".encode()
        return archive, "sha256:" + hashlib.sha256(archive).hexdigest()

    async def invoke(self, **kwargs: object) -> HybridToolInvocationResult:
        self.invocations += 1
        return HybridToolInvocationResult(
            protocol_version=HYBRID_TOOL_PROTOCOL_VERSION,
            call_id=str(kwargs["call_id"]),
            tool_name=str(kwargs["tool_name"]),
            status=HybridInvocationStatus.SUCCESS,
            value={"written": True},
            stdout="",
            stderr="",
            exit_code=0,
            error=None,
            timings=HybridToolInternalTimings(
                queue_wait_ms=0,
                execution_ms=1,
                serialization_ms=0,
            ),
        )

    async def read_result(self, _call_id: str, **_kwargs):
        return None

    async def delete(self) -> None:
        self.deleted += 1

    async def close(
        self,
        *,
        cancelled: bool = False,
        retain: bool = False,
        retain_auto_delete_seconds: int = 600,
    ) -> None:
        del cancelled, retain_auto_delete_seconds
        if retain:
            self.retained += 1
        else:
            self.deleted += 1


class _Provider:
    group = SimpleNamespace(
        resource_id=(
            "/subscriptions/s/resourceGroups/r/providers/"
            "Microsoft.App/sandboxGroups/g"
        ),
        region="eastus2",
    )

    def __init__(self, *, present: bool) -> None:
        self.present = present
        self.closed = 0

    async def get_sandbox_summary(self, _sandbox_id: str):
        return SimpleNamespace() if self.present else None

    async def delete_sandbox(self, _sandbox_id: str) -> None:
        return None

    async def close(self) -> None:
        self.closed += 1


def _settings() -> HybridSandboxSettings:
    return HybridSandboxSettings(
        group_resource_id=(
            "/subscriptions/s/resourceGroups/r/providers/"
            "Microsoft.App/sandboxGroups/g"
        ),
        region="eastus2",
        allowed_hosts=(),
        sandbox_disk="python-3.13",
        create_timeout_seconds=30,
        ready_timeout_seconds=30,
        drain_timeout_seconds=5,
        orphan_age_seconds=1200,
    )


def _request(
    *,
    call: int,
    workspace_ref=None,
    profile: SandboxExecutionProfile = SandboxExecutionProfile.PER_CALL,
    fault: DurableFaultProfile = DurableFaultProfile.NONE,
) -> ToolRequestV1:
    arguments = {"path": "value.txt", "content": str(call)}
    request_hash = tool_request_hash(
        tool_name="write_file",
        arguments=arguments,
        behavior=ToolBehavior.IDEMPOTENT_WRITE,
        provenance=ToolProvenance.LOCAL,
        policy_hash="a" * 64,
        catalog_hash="b" * 64,
        package_hash="c" * 64,
        workspace_ref=workspace_ref,
        sandbox_profile=profile,
        fault_profile=fault,
    )
    return ToolRequestV1(
        run_id="run-1",
        session_id="session-1",
        step_index=call - 1,
        call_ordinal=0,
        provider_call_id=f"provider-{call}",
        call_key=canonical_hash({"call": call}),
        tool_name="write_file",
        provenance=ToolProvenance.LOCAL,
        behavior=ToolBehavior.IDEMPOTENT_WRITE,
        arguments=arguments,
        request_hash=request_hash,
        policy_hash="a" * 64,
        catalog_hash="b" * 64,
        package_hash="c" * 64,
        workspace_ref=workspace_ref,
        sandbox_profile=profile,
        fault_profile=fault,
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )


def test_workspace_export_restore_is_integrity_bound(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "value.txt").write_text("hello", encoding="utf-8")
    archive = tmp_path / "workspace.zip"

    digest = export_workspace_archive(workspace, archive)
    restored = tmp_path / "restored"
    restore_workspace_archive(archive, restored, digest)

    assert (restored / "value.txt").read_text(encoding="utf-8") == "hello"
    with pytest.raises(Exception, match="digest"):
        restore_workspace_archive(
            archive,
            tmp_path / "corrupt",
            "sha256:" + "0" * 64,
        )


@pytest.mark.asyncio
async def test_capacity_coordinator_is_bounded_and_releases_slots() -> None:
    store = InMemoryDurableKeyedDocumentStore()
    coordinator = DurableSandboxCapacityCoordinator(store, slots=1)
    deadline = datetime.now(UTC) + timedelta(minutes=1)

    key, revision = await coordinator.acquire("a" * 64, deadline=deadline)
    with pytest.raises(Exception, match="capacity"):
        await coordinator.acquire("b" * 64, deadline=deadline)
    await coordinator.release(key, revision)
    second, _revision = await coordinator.acquire("b" * 64, deadline=deadline)

    assert second == key


@pytest.mark.asyncio
async def test_expired_capacity_lease_is_reclaimed_without_extra_slot() -> None:
    store = InMemoryDurableKeyedDocumentStore()
    coordinator = DurableSandboxCapacityCoordinator(store, slots=1)
    expired_key, _expired_revision = await coordinator.acquire(
        "a" * 64,
        deadline=datetime.now(UTC) - timedelta(seconds=1),
    )

    reclaimed_key, _revision = await coordinator.acquire(
        "b" * 64,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )

    assert reclaimed_key == expired_key


@pytest.mark.asyncio
async def test_retained_capacity_slot_uses_full_retention_deadline() -> None:
    store = InMemoryDurableKeyedDocumentStore()
    coordinator = DurableSandboxCapacityCoordinator(store, slots=1)
    deadline = datetime.now(UTC) + timedelta(hours=24)

    key, _revision = await coordinator.acquire("a" * 64, deadline=deadline)
    document = await store.get(key)

    assert document is not None
    payload = json.loads(document.payload)
    expires_at = datetime.fromisoformat(
        payload["expires_at"].replace("Z", "+00:00")
    )
    assert expires_at == deadline


@pytest.mark.asyncio
async def test_per_call_wave_checkpoints_workspace_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leases = [_Lease(0), _Lease(1)]

    async def acquire(_cls, *_args, **_kwargs):
        return leases.pop(0)

    monkeypatch.setattr(
        InvocationSandboxLease,
        "acquire",
        classmethod(acquire),
    )
    content = InMemoryDurableContentStore()
    lane = DurableAcaSandboxLane(
        settings=_settings(),
        loop_settings=DurableLoopSettings(),
        content=content,
        receipts=InMemoryDurableKeyedDocumentStore(),
        provider_factory=lambda: _provider(False),
        package_factory=lambda _root: _async_value(_package()),
    )
    request = _request(call=1)

    first = await lane.dispatch(request)
    replay = await lane.dispatch(request)

    assert first.status is ToolResultStatus.SUCCEEDED
    assert first.workspace_ref is not None
    artifact = await get_protocol_model(
        content,
        first.workspace_ref,
        WorkspaceArtifactV1,
    )
    assert await content.get_bytes(artifact.archive_ref) == b"workspace-1"
    assert replay.deduplicated is True
    assert replay.workspace_ref == first.workspace_ref
    assert not leases


@pytest.mark.asyncio
async def test_unreconciled_local_mutation_is_never_returned_as_success() -> None:
    receipts = InMemoryDurableKeyedDocumentStore()
    request = _request(call=1).model_copy(
        update={"behavior": ToolBehavior.MUTATING}
    )
    request = request.model_copy(
        update={
            "request_hash": tool_request_hash(
                tool_name=request.tool_name,
                arguments=request.arguments,
                behavior=ToolBehavior.MUTATING,
                provenance=request.provenance,
                policy_hash=request.policy_hash,
                catalog_hash=request.catalog_hash,
                package_hash=request.package_hash,
                workspace_ref=request.workspace_ref,
                sandbox_profile=request.sandbox_profile,
                fault_profile=request.fault_profile,
            )
        }
    )
    await create_activity_receipt(
        receipts,
        f"activities/sandbox-tool/{request.call_key}",
        ActivityReceiptV1(
            operation_key=request.call_key,
            request_hash=request.request_hash,
            kind="sandbox_tool",
            status=ActivityReceiptStatus.STARTED,
            attempt=1,
            updated_at=datetime.now(UTC),
        ),
    )
    lane = DurableAcaSandboxLane(
        settings=_settings(),
        loop_settings=DurableLoopSettings(),
        content=InMemoryDurableContentStore(),
        receipts=receipts,
        provider_factory=lambda: _provider(False),
        package_factory=lambda _root: _async_value(_package()),
    )

    result = await lane.dispatch(request)

    assert result.status is ToolResultStatus.AMBIGUOUS
    assert result.error is not None
    assert result.error.possibly_committed is True


@pytest.mark.asyncio
async def test_retained_journal_result_must_match_request_hash_and_tool() -> None:
    result = HybridToolInvocationResult(
        protocol_version=HYBRID_TOOL_PROTOCOL_VERSION,
        call_id="a" * 64,
        tool_name="write_file",
        status=HybridInvocationStatus.SUCCESS,
        value={"written": True},
        stdout="",
        stderr="",
        exit_code=0,
        error=None,
        timings=HybridToolInternalTimings(
            queue_wait_ms=0,
            execution_ms=1,
            serialization_ms=0,
        ),
        request_hash="1" * 64,
    )

    class Handle:
        async def read_file(self, _path: str) -> bytes:
            return canonical_hybrid_json_bytes(result)

    lease = InvocationSandboxLease(
        settings=_settings(),
        operation_id="operation",
        provider=SimpleNamespace(),
        handle=Handle(),  # type: ignore[arg-type]
        manifest=_manifest(),
    )

    with pytest.raises(RuntimeError, match="different request"):
        await lease.read_result(
            "a" * 64,
            tool_name="write_file",
            request_hash="2" * 64,
        )
    with pytest.raises(RuntimeError, match="different request"):
        await lease.read_result(
            "a" * 64,
            tool_name="other_tool",
            request_hash="1" * 64,
        )


@pytest.mark.asyncio
async def test_retained_drain_timeout_still_closes_controller_resources() -> None:
    class Handle:
        def __init__(self) -> None:
            self.lifecycle = 0
            self.closed = 0

        async def set_lifecycle_policy(self, _policy) -> None:
            self.lifecycle += 1

        async def close(self) -> None:
            self.closed += 1

    class Provider:
        def __init__(self) -> None:
            self.closed = 0

        async def close(self) -> None:
            self.closed += 1

    handle = Handle()
    provider = Provider()
    lease = InvocationSandboxLease(
        settings=replace(_settings(), drain_timeout_seconds=0.001),
        operation_id="operation",
        provider=provider,  # type: ignore[arg-type]
        handle=handle,  # type: ignore[arg-type]
        manifest=_manifest(),
    )
    lease._active_calls = 1

    await lease.close(retain=True)

    assert handle.lifecycle == 1
    assert handle.closed == 1
    assert provider.closed == 1


@pytest.mark.asyncio
async def test_retained_loss_recreates_from_last_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_lease = _Lease(0)
    first_lease = _Lease(1)
    second_lease = _Lease(2)
    leases = [catalog_lease, first_lease, second_lease]

    async def acquire(_cls, *_args, **_kwargs):
        return leases.pop(0)

    monkeypatch.setattr(
        InvocationSandboxLease,
        "acquire",
        classmethod(acquire),
    )
    receipts = InMemoryDurableKeyedDocumentStore()
    faults = DurableOneShotFaults(receipts, enabled=True)
    providers = [_Provider(present=False)]

    async def provider_factory():
        if providers:
            return providers.pop(0)
        return _Provider(present=False)

    content = InMemoryDurableContentStore()
    lane = DurableAcaSandboxLane(
        settings=_settings(),
        loop_settings=DurableLoopSettings(
            retained_sandbox_enabled=True,
            fault_injection_enabled=True,
        ),
        content=content,
        receipts=receipts,
        provider_factory=provider_factory,
        package_factory=lambda _root: _async_value(_package()),
        faults=faults,
    )
    first = await lane.dispatch(
        _request(
            call=1,
            profile=SandboxExecutionProfile.RETAINED_SESSION,
            fault=DurableFaultProfile.SANDBOX_LOSS_AFTER_CHECKPOINT,
        )
    )
    assert first.workspace_ref is not None
    second = await lane.dispatch(
        _request(
            call=2,
            workspace_ref=first.workspace_ref,
            profile=SandboxExecutionProfile.RETAINED_SESSION,
        )
    )

    assert first_lease.deleted == 1
    assert second.status is ToolResultStatus.SUCCEEDED
    assert second_lease.expected_manifest.generation == 2
    assert second_lease.restored == [b"workspace-1"]
    assert not leases


@pytest.mark.asyncio
async def test_retained_session_resumes_existing_inventory_without_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_lease = _Lease(0)
    first_lease = _Lease(1)
    resumed_lease = _Lease(1)
    leases = [catalog_lease, first_lease]
    maximum_run_seconds: list[float | None] = []

    async def acquire(_cls, *_args, **kwargs):
        maximum_run_seconds.append(kwargs.get("maximum_run_seconds"))
        return leases.pop(0)

    async def attach(_cls, *_args, **_kwargs):
        return resumed_lease

    monkeypatch.setattr(
        InvocationSandboxLease,
        "acquire",
        classmethod(acquire),
    )
    monkeypatch.setattr(
        InvocationSandboxLease,
        "attach",
        classmethod(attach),
    )
    receipts = InMemoryDurableKeyedDocumentStore()
    providers = [_Provider(present=True)]

    async def provider_factory():
        if providers:
            return providers.pop(0)
        return _Provider(present=True)

    content = InMemoryDurableContentStore()
    lane = DurableAcaSandboxLane(
        settings=_settings(),
        loop_settings=DurableLoopSettings(retained_sandbox_enabled=True),
        content=content,
        receipts=receipts,
        provider_factory=provider_factory,
        package_factory=lambda _root: _async_value(_package()),
    )
    first = await lane.dispatch(
        _request(
            call=1,
            profile=SandboxExecutionProfile.RETAINED_SESSION,
        )
    )
    assert first.workspace_ref is not None
    second = await lane.dispatch(
        _request(
            call=2,
            workspace_ref=first.workspace_ref,
            profile=SandboxExecutionProfile.RETAINED_SESSION,
        )
    )

    assert second.status is ToolResultStatus.SUCCEEDED
    assert resumed_lease.restored == []
    assert resumed_lease.retained == 1
    assert maximum_run_seconds == [None, 300]
    assert not leases


@pytest.mark.asyncio
async def test_retained_attach_failure_does_not_recreate_present_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_lease = _Lease(0)
    first_lease = _Lease(1)
    leases = [catalog_lease, first_lease]

    async def acquire(_cls, *_args, **_kwargs):
        return leases.pop(0)

    async def attach(_cls, *_args, **_kwargs):
        raise RuntimeError("transient attach failure")

    monkeypatch.setattr(
        InvocationSandboxLease,
        "acquire",
        classmethod(acquire),
    )
    monkeypatch.setattr(
        InvocationSandboxLease,
        "attach",
        classmethod(attach),
    )
    content = InMemoryDurableContentStore()
    lane = DurableAcaSandboxLane(
        settings=_settings(),
        loop_settings=DurableLoopSettings(retained_sandbox_enabled=True),
        content=content,
        receipts=InMemoryDurableKeyedDocumentStore(),
        provider_factory=lambda: _provider(True),
        package_factory=lambda _root: _async_value(_package()),
    )
    first = await lane.dispatch(
        _request(
            call=1,
            profile=SandboxExecutionProfile.RETAINED_SESSION,
        )
    )
    assert first.workspace_ref is not None

    with pytest.raises(RuntimeError, match="attach failure"):
        await lane.dispatch(
            _request(
                call=2,
                workspace_ref=first.workspace_ref,
                profile=SandboxExecutionProfile.RETAINED_SESSION,
            )
        )

    assert first_lease.invocations == 1
    assert not leases


async def _async_value(value):
    return value


async def _provider(present: bool):
    return _Provider(present=present)
