"""ACA Sandbox execution lane for durable single-call and retained profiles."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .._logger import logger
from ..config.paths import get_app_root
from ..controller.package import CapturedContentPackage, get_content_package
from ..strict_json import canonical_json_bytes
from ..transport.manifest import ExpectedSandboxManifestBinding
from ..transport.ports import SandboxSessionProvider
from ..transport.transport_models import PersistedSandboxBinding, SandboxGroupBinding
from .durable_loop_activities import (
    DurableContentStore,
    get_protocol_model,
    put_protocol_model,
)
from .durable_loop_config import DurableLoopSettings
from .durable_loop_observability import (
    DurableLoopOutcome,
    DurableLoopPhase,
    DurableLoopTimer,
)
from .durable_loop_protocol import (
    ContentRefV1,
    DurableFaultProfile,
    ErrorDisposition,
    ErrorEnvelopeV1,
    FrozenToolDescriptorV1,
    SandboxExecutionProfile,
    ToolBehavior,
    ToolProvenance,
    ToolRequestV1,
    ToolResultStatus,
    ToolResultV1,
    WorkspaceArtifactV1,
    canonical_hash,
)
from .durable_loop_receipts import (
    ActivityReceiptStatus,
    ActivityReceiptV1,
    DurableKeyedDocumentStore,
    DurableOneShotFaults,
    create_activity_receipt,
    read_activity_receipt,
    replace_activity_receipt,
)
from .durable_loop_tools import (
    DurableRetainedSandboxInspection,
    DurableToolInspectionError,
)
from .hybrid_config import HybridSandboxSettings
from .hybrid_protocol import (
    HybridInvocationStatus,
    HybridToolInvocationResult,
    HybridToolManifest,
    parse_hybrid_tool_manifest,
)
from .hybrid_tools import InvocationSandboxLease

type SandboxProviderFactory = Callable[[], Awaitable[SandboxSessionProvider]]
_DURABLE_WAVE_OWNER_KIND = "durable_loop_wave"
_DURABLE_RETAINED_OWNER_KIND = "durable_loop_retained"


class DurableSandboxError(DurableToolInspectionError):
    """The durable ACA lane could not safely complete one call."""


class _CapacityLeaseV1(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    operation_key: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    expires_at: datetime


class _RetainedSandboxV1(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    run_id: str
    session_id: str
    generation: Annotated[int, Field(ge=1)]
    sandbox_id: str
    group_resource_id: str
    region: str
    operation_id: str
    expected_manifest: dict[str, object]
    tool_manifest: dict[str, object]
    package_digest_kind: str
    package_digest: str
    workspace_ref: ContentRefV1 | None = None
    owner_call_key: str | None = None
    capacity_key: str
    capacity_revision: str
    expires_at: datetime


class DurableSandboxCapacityCoordinator:
    """Cross-worker fixed-slot capacity admission over keyed Blob CAS."""

    def __init__(
        self,
        store: DurableKeyedDocumentStore,
        *,
        slots: int,
    ) -> None:
        self._store = store
        self._slots = slots

    async def acquire(
        self,
        operation_key: str,
        *,
        deadline: datetime,
    ) -> tuple[str, str]:
        """Acquire one expiring capacity slot or fail without creating."""
        timer = DurableLoopTimer(DurableLoopPhase.SANDBOX_CAPACITY_WAIT)
        now = datetime.now(UTC)
        expires_at = deadline.astimezone(UTC)
        payload = canonical_json_bytes(
            _CapacityLeaseV1(
                operation_key=operation_key,
                expires_at=expires_at,
            )
        )
        for index in range(self._slots):
            key = f"sandbox-capacity/slot-{index}"
            if await self._store.create(key, payload):
                current = await self._store.get(key)
                if current is None:
                    raise DurableSandboxError("sandbox capacity receipt disappeared")
                timer.finish(DurableLoopOutcome.COMPLETED)
                return key, current.revision
            current = await self._store.get(key)
            if current is None:
                continue
            lease = _CapacityLeaseV1.model_validate_json(current.payload)
            if lease.operation_key == operation_key:
                timer.finish(DurableLoopOutcome.COMPLETED)
                return key, current.revision
            if lease.expires_at.astimezone(UTC) <= now and await self._store.replace(
                key,
                payload,
                revision=current.revision,
            ):
                replaced = await self._store.get(key)
                if replaced is None:
                    raise DurableSandboxError("sandbox capacity receipt disappeared")
                timer.finish(DurableLoopOutcome.COMPLETED)
                return key, replaced.revision
        timer.finish(DurableLoopOutcome.FAILED)
        raise DurableSandboxError("sandbox_capacity_exhausted")

    async def release(self, key: str, revision: str) -> None:
        """Release one slot without deleting a newer owner's lease."""
        await self._store.delete(key, revision=revision)


class DurableAcaSandboxLane:
    """One-call ACA waves with optional fenced retained-session reuse."""

    def __init__(
        self,
        *,
        settings: HybridSandboxSettings,
        loop_settings: DurableLoopSettings,
        content: DurableContentStore,
        receipts: DurableKeyedDocumentStore,
        provider_factory: SandboxProviderFactory | None = None,
        package_factory: Callable[
            [Path],
            Awaitable[CapturedContentPackage],
        ] | None = None,
        faults: DurableOneShotFaults | None = None,
        local_tools_enabled: bool = True,
    ) -> None:
        self._settings = settings
        self._loop_settings = loop_settings
        self._content = content
        self._receipts = receipts
        self._provider_factory = provider_factory or _provider_factory(settings)
        self._package_factory = package_factory or get_content_package
        self._faults = faults or DurableOneShotFaults(
            receipts,
            enabled=loop_settings.fault_injection_enabled,
        )
        self._capacity = DurableSandboxCapacityCoordinator(
            receipts,
            slots=loop_settings.max_app_owned_sandboxes,
        )
        self._local_tools_enabled = local_tools_enabled
        self._catalog_lock = asyncio.Lock()
        self._catalog_manifest: HybridToolManifest | None = None
        self._catalog_package: CapturedContentPackage | None = None
        self._catalog_package_ref: ContentRefV1 | None = None

    async def discover(
        self,
    ) -> tuple[tuple[FrozenToolDescriptorV1, ...], str]:
        """Discover local schemas in ACA and cache the immutable package binding."""
        if not self._local_tools_enabled:
            return (), canonical_hash({"local_tools": []})
        async with self._catalog_lock:
            if self._catalog_manifest is None or self._catalog_package is None:
                package = await self._package_factory(_package_root(self._settings))
                slot_key, revision = await self._capacity.acquire(
                    canonical_hash({"kind": "catalog-discovery", "digest": package.digest}),
                    deadline=datetime.now(UTC)
                    + timedelta(seconds=self._settings.create_timeout_seconds),
                )
                lease: InvocationSandboxLease | None = None
                try:
                    create_timer = DurableLoopTimer(
                        DurableLoopPhase.SANDBOX_CREATE,
                        provenance="per_call",
                    )
                    lease = await InvocationSandboxLease.acquire(
                        self._settings,
                        provider_factory=self._provider_factory,
                        package_factory=lambda _root: _return_package(package),
                    )
                    create_timer.finish(DurableLoopOutcome.COMPLETED)
                    self._catalog_manifest = lease.manifest
                    self._catalog_package = package
                    self._catalog_package_ref = await self._content.put_bytes(
                        kind="sandbox-tool-package",
                        payload=package.archive_bytes,
                        media_type="application/zip",
                        retention_class="deployment",
                    )
                finally:
                    if lease is not None:
                        delete_timer = DurableLoopTimer(
                            DurableLoopPhase.SANDBOX_DELETE,
                            provenance="per_call",
                        )
                        await lease.delete()
                        delete_timer.finish(DurableLoopOutcome.COMPLETED)
                    await self._capacity.release(slot_key, revision)
            assert self._catalog_manifest is not None
            assert self._catalog_package is not None
            descriptors = tuple(
                FrozenToolDescriptorV1(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.parameters,
                    provenance=ToolProvenance.LOCAL,
                    behavior=ToolBehavior.MUTATING,
                    parallel_safe=False,
                )
                for tool in self._catalog_manifest.tools
            )
            return descriptors, _package_hash(self._catalog_package)

    async def dispatch(self, request: ToolRequestV1) -> ToolResultV1:
        """Execute or reconcile one request-hash-bound local tool call."""
        if request.provenance is not ToolProvenance.LOCAL:
            raise DurableSandboxError("sandbox lane received a non-local tool")
        receipt_key = f"activities/sandbox-tool/{request.call_key}"
        existing = await read_activity_receipt(self._receipts, receipt_key)
        if existing is not None:
            receipt, _revision = existing
            _validate_receipt(receipt, request)
            if receipt.result_ref is not None:
                return (
                    await get_protocol_model(
                        self._content,
                        receipt.result_ref,
                        ToolResultV1,
                    )
                ).model_copy(update={"deduplicated": True})
            if request.behavior is ToolBehavior.MUTATING:
                return _ambiguous_result(request)
        else:
            started = ActivityReceiptV1(
                operation_key=request.call_key,
                request_hash=request.request_hash,
                kind="sandbox_tool",
                status=ActivityReceiptStatus.STARTED,
                attempt=1,
                updated_at=datetime.now(UTC),
                workspace_ref=request.workspace_ref,
            )
            if not await create_activity_receipt(
                self._receipts,
                receipt_key,
                started,
            ):
                return await self.dispatch(request)
        if request.sandbox_profile is SandboxExecutionProfile.RETAINED_SESSION:
            return await self._dispatch_retained(request, receipt_key)
        return await self._dispatch_per_call(request, receipt_key)

    async def cleanup(
        self,
        *,
        run_id: str,
        session_id: str,
        profile: SandboxExecutionProfile,
        fault_profile: DurableFaultProfile,
    ) -> None:
        """Explicitly delete a retained sandbox; per-call waves are already deleted."""
        if profile is SandboxExecutionProfile.PER_CALL:
            return
        timer = DurableLoopTimer(DurableLoopPhase.CLEANUP, provenance="sandbox")
        key = _retained_key(session_id)
        current = await self._receipts.get(key)
        if current is None:
            return
        document = _RetainedSandboxV1.model_validate_json(current.payload)
        if document.run_id != run_id:
            raise DurableSandboxError("retained sandbox run fence changed")
        provider = await self._provider_factory()
        try:
            if await self._faults.consume(
                fault_profile,
                run_id=run_id,
                point="cleanup_failure",
            ):
                raise DurableSandboxError("injected sandbox cleanup failure")
            await provider.delete_sandbox(document.sandbox_id)
        finally:
            await provider.close()
        await self._capacity.release(
            document.capacity_key,
            document.capacity_revision,
        )
        await self._receipts.delete(key, revision=current.revision)
        timer.finish(DurableLoopOutcome.COMPLETED)

    async def inspect_retained_sandbox(
        self,
        *,
        run_id: str,
        session_id: str,
    ) -> DurableRetainedSandboxInspection | None:
        """Return content-free state for the exact retained sandbox."""
        current = await self._receipts.get(_retained_key(session_id))
        if current is None:
            return None
        try:
            document = _RetainedSandboxV1.model_validate_json(current.payload)
        except Exception as exc:
            raise DurableToolInspectionError(
                "retained sandbox inspection unavailable"
            ) from exc
        _validated_inspection_manifest(
            document,
            run_id=run_id,
            session_id=session_id,
        )
        try:
            provider = await self._provider_factory()
            try:
                summary = await provider.get_sandbox_summary(document.sandbox_id)
            finally:
                await provider.close()
        except DurableToolInspectionError:
            raise
        except Exception as exc:
            raise DurableToolInspectionError(
                "retained sandbox inspection unavailable"
            ) from exc
        if summary is None:
            return None
        return DurableRetainedSandboxInspection(
            sandbox_instance_alias=(
                f"sandbox-{canonical_hash({'sandbox_id': document.sandbox_id})[:8]}"
            ),
            generation=document.generation,
            state=summary.state or "Unknown",
            workspace_checkpoint_present=document.workspace_ref is not None,
        )

    async def _dispatch_per_call(
        self,
        request: ToolRequestV1,
        receipt_key: str,
    ) -> ToolResultV1:
        manifest, package = await self._require_catalog()
        slot_key, revision = await self._capacity.acquire(
            request.call_key,
            deadline=request.deadline,
        )
        lease: InvocationSandboxLease | None = None
        timer = DurableLoopTimer(
            DurableLoopPhase.SANDBOX_EXECUTE,
            provenance="per_call",
        )
        try:
            lease = await InvocationSandboxLease.acquire(
                self._settings,
                maximum_run_seconds=self._loop_settings.local_tool_timeout_seconds,
                session_id=request.session_id,
                owner_hash=request.owner_hash[:52],
                owner_kind=_DURABLE_WAVE_OWNER_KIND,
                provider_factory=self._provider_factory,
                package_factory=lambda _root: _return_package(package),
            )
            _verify_manifest(lease.manifest, manifest)
            await self._restore_workspace(lease, request)
            invocation = await lease.invoke(
                call_id=request.call_key,
                tool_name=request.tool_name,
                arguments=request.arguments,
                deadline=asyncio.get_running_loop().time()
                + min(
                    self._loop_settings.local_tool_timeout_seconds,
                    max(
                        0.001,
                        (
                            request.deadline.astimezone(UTC) - datetime.now(UTC)
                        ).total_seconds(),
                    ),
                ),
                request_hash=request.request_hash,
            )
            workspace_ref = await self._export_workspace(
                lease,
                request,
                manifest,
                package,
            )
            result = _sandbox_result(request, invocation, workspace_ref)
            await self._commit_result(receipt_key, request, result)
            if await self._faults.consume(
                request.fault_profile,
                run_id=request.run_id,
                point="cleanup_failure",
            ):
                await lease.close(retain=True)
                lease = None
            timer.finish(
                DurableLoopOutcome.COMPLETED
                if result.status is ToolResultStatus.SUCCEEDED
                else DurableLoopOutcome.FAILED
            )
            return result
        finally:
            if lease is not None:
                await lease.delete()
            await self._capacity.release(slot_key, revision)

    async def _dispatch_retained(  # noqa: PLR0912
        self,
        request: ToolRequestV1,
        receipt_key: str,
    ) -> ToolResultV1:
        manifest, package = await self._require_catalog()
        key = _retained_key(request.session_id)
        current = await self._receipts.get(key)
        created = False
        if current is None:
            document, lease = await self._create_retained(
                request,
                manifest,
                package,
                generation=1,
            )
            if not await self._receipts.create(
                key,
                canonical_json_bytes(document),
            ):
                await lease.delete()
                return await self._dispatch_retained(request, receipt_key)
            current = await self._receipts.get(key)
            if current is None:
                await lease.delete()
                raise DurableSandboxError("retained sandbox receipt disappeared")
            created = True
        else:
            document = _RetainedSandboxV1.model_validate_json(current.payload)
            lease, replacement = await self._attach_or_recreate(
                request,
                document,
                manifest,
                package,
            )
            if replacement is not None:
                created = True
                document = replacement

        claimed = document.model_copy(
            update={
                "expires_at": datetime.now(UTC)
                + timedelta(
                    seconds=self._loop_settings.retained_sandbox_auto_delete_seconds
                ),
                "owner_call_key": request.call_key,
                "run_id": request.run_id,
            }
        )
        if document.owner_call_key not in {None, request.call_key}:
            await lease.close(
                retain=True,
                retain_auto_delete_seconds=(
                    self._loop_settings.retained_sandbox_auto_delete_seconds
                ),
            )
            raise DurableSandboxError("retained sandbox already has an active call")
        if not await self._receipts.replace(
            key,
            canonical_json_bytes(claimed),
            revision=current.revision,
        ):
            await lease.close(
                retain=True,
                retain_auto_delete_seconds=(
                    self._loop_settings.retained_sandbox_auto_delete_seconds
                ),
            )
            return await self._dispatch_retained(request, receipt_key)
        claimed_state = await self._receipts.get(key)
        if claimed_state is None:
            await lease.close(
                retain=True,
                retain_auto_delete_seconds=(
                    self._loop_settings.retained_sandbox_auto_delete_seconds
                ),
            )
            raise DurableSandboxError("retained sandbox claim disappeared")
        try:
            reconciled = await lease.read_result(
                request.call_key,
                tool_name=request.tool_name,
                request_hash=request.request_hash,
            )
            if reconciled is None:
                if created:
                    await self._restore_workspace(lease, request)
                reconciled = await lease.invoke(
                    call_id=request.call_key,
                    tool_name=request.tool_name,
                    arguments=request.arguments,
                    deadline=asyncio.get_running_loop().time()
                    + self._loop_settings.local_tool_timeout_seconds,
                    request_hash=request.request_hash,
                )
            workspace_ref = await self._export_workspace(
                lease,
                request,
                manifest,
                package,
            )
            result = _sandbox_result(request, reconciled, workspace_ref)
            await self._commit_result(receipt_key, request, result)
            released = claimed.model_copy(
                update={
                    "owner_call_key": None,
                    "workspace_ref": workspace_ref,
                }
            )
            if not await self._receipts.replace(
                key,
                canonical_json_bytes(released),
                revision=claimed_state.revision,
            ):
                _raise_retained_release_conflict()
            if await self._faults.consume(
                request.fault_profile,
                run_id=request.run_id,
                point="sandbox_loss_after_checkpoint",
            ):
                await lease.delete()
            else:
                await lease.close(
                    retain=True,
                    retain_auto_delete_seconds=(
                        self._loop_settings.retained_sandbox_auto_delete_seconds
                    ),
                )
            return result
        except BaseException:
            await self._handle_failed_retained_dispatch(
                key,
                request,
                claimed,
                claimed_revision=claimed_state.revision,
                lease=lease,
            )
            raise

    async def _handle_failed_retained_dispatch(
        self,
        key: str,
        request: ToolRequestV1,
        claimed: _RetainedSandboxV1,
        *,
        claimed_revision: str,
        lease: InvocationSandboxLease,
    ) -> None:
        await self._release_failed_read_claim(
            key,
            request,
            claimed,
            claimed_revision=claimed_revision,
        )
        try:
            await lease.close(
                retain=True,
                retain_auto_delete_seconds=(
                    self._loop_settings.retained_sandbox_auto_delete_seconds
                ),
            )
        except BaseException:
            logger.warning(
                "Durable retained sandbox close failed after tool failure.",
                exc_info=True,
            )

    async def _release_failed_read_claim(
        self,
        key: str,
        request: ToolRequestV1,
        claimed: _RetainedSandboxV1,
        *,
        claimed_revision: str,
    ) -> None:
        if request.behavior is not ToolBehavior.READ_ONLY:
            return
        try:
            current = await self._receipts.get(key)
            if current is None or current.revision != claimed_revision:
                return
            document = _RetainedSandboxV1.model_validate_json(current.payload)
            if (
                document.sandbox_id != claimed.sandbox_id
                or document.session_id != claimed.session_id
                or document.generation != claimed.generation
                or document.owner_call_key != request.call_key
            ):
                return
            released = document.model_copy(update={"owner_call_key": None})
            if not await self._receipts.replace(
                key,
                canonical_json_bytes(released),
                revision=claimed_revision,
            ):
                logger.warning(
                    "Durable retained read claim changed before failure release."
                )
        except BaseException:
            logger.warning(
                "Durable retained read claim release failed.",
                exc_info=True,
            )

    async def _attach_or_recreate(
        self,
        request: ToolRequestV1,
        document: _RetainedSandboxV1,
        manifest: HybridToolManifest,
        package: CapturedContentPackage,
    ) -> tuple[InvocationSandboxLease, _RetainedSandboxV1 | None]:
        provider = await self._provider_factory()
        try:
            summary = await provider.get_sandbox_summary(document.sandbox_id)
        finally:
            await provider.close()
        if summary is None:
            _verify_retained_bindings(document, request, manifest, package)
            await self._capacity.release(
                document.capacity_key,
                document.capacity_revision,
            )
            new_document, lease = await self._create_retained(
                request,
                manifest,
                package,
                generation=document.generation + 1,
            )
            return lease, new_document
        _verify_retained_bindings(document, request, manifest, package)
        expected = _expected_manifest(document)
        persisted = PersistedSandboxBinding.create(
            document.sandbox_id,
            SandboxGroupBinding.create(
                document.group_resource_id,
                document.region,
            ),
        )
        return (
            await InvocationSandboxLease.attach(
                self._settings,
                operation_id=document.operation_id,
                persisted=persisted,
                expected_manifest=expected,
                manifest=parse_hybrid_tool_manifest(
                    canonical_json_bytes(document.tool_manifest)
                ),
                package=package,
                resume=True,
                restart_executor=summary.state == "Stopped",
                provider_factory=self._provider_factory,
            ),
            None,
        )

    async def _create_retained(
        self,
        request: ToolRequestV1,
        manifest: HybridToolManifest,
        package: CapturedContentPackage,
        *,
        generation: int,
    ) -> tuple[_RetainedSandboxV1, InvocationSandboxLease]:
        expires_at = datetime.now(UTC) + timedelta(
            seconds=self._loop_settings.retained_sandbox_auto_delete_seconds
        )
        slot_key, revision = await self._capacity.acquire(
            canonical_hash(
                {
                    "generation": generation,
                    "session_id": request.session_id,
                }
            ),
            deadline=expires_at,
        )
        try:
            lease = await InvocationSandboxLease.acquire(
                self._settings,
                maximum_run_seconds=self._loop_settings.local_tool_timeout_seconds,
                session_id=request.session_id,
                generation=generation,
                owner_hash=request.owner_hash[:52],
                owner_kind=_DURABLE_RETAINED_OWNER_KIND,
                persist_attach_manifest=True,
                provider_factory=self._provider_factory,
                package_factory=lambda _root: _return_package(package),
            )
            _verify_manifest(lease.manifest, manifest)
            return (
                _retained_document(
                    request,
                    lease,
                    capacity_key=slot_key,
                    capacity_revision=revision,
                    expires_at=expires_at,
                ),
                lease,
            )
        except BaseException:
            await self._capacity.release(slot_key, revision)
            raise

    async def _restore_workspace(
        self,
        lease: InvocationSandboxLease,
        request: ToolRequestV1,
    ) -> None:
        if request.workspace_ref is None:
            return
        timer = DurableLoopTimer(DurableLoopPhase.SANDBOX_RESTORE)
        artifact = await get_protocol_model(
            self._content,
            request.workspace_ref,
            WorkspaceArtifactV1,
        )
        _verify_workspace_bindings(artifact, request)
        archive = await self._content.get_bytes(artifact.archive_ref)
        await lease.restore_workspace(
            archive,
            f"sha256:{artifact.archive_ref.sha256}",
        )
        timer.finish(DurableLoopOutcome.COMPLETED)

    async def _export_workspace(
        self,
        lease: InvocationSandboxLease,
        request: ToolRequestV1,
        manifest: HybridToolManifest,
        package: CapturedContentPackage,
    ) -> ContentRefV1:
        timer = DurableLoopTimer(DurableLoopPhase.SANDBOX_EXPORT)
        archive, digest = await lease.export_workspace()
        archive_ref = await self._content.put_bytes(
            kind="sandbox-workspace",
            payload=archive,
            media_type="application/zip",
            retention_class="run",
        )
        if digest != f"sha256:{archive_ref.sha256}":
            raise DurableSandboxError("workspace content hash changed")
        artifact = WorkspaceArtifactV1(
            generation=request.step_index + 1,
            archive_ref=archive_ref,
            parent_ref=request.workspace_ref,
            manifest_hash=canonical_hash(manifest.model_dump(mode="json")),
            package_hash=request.package_hash,
            policy_hash=request.policy_hash,
            catalog_hash=request.catalog_hash,
        )
        reference = await put_protocol_model(
            self._content,
            kind="workspace-checkpoint",
            model=artifact,
        )
        timer.finish(DurableLoopOutcome.COMPLETED)
        return reference

    async def _commit_result(
        self,
        receipt_key: str,
        request: ToolRequestV1,
        result: ToolResultV1,
    ) -> None:
        result_ref = await put_protocol_model(
            self._content,
            kind="tool-result",
            model=result,
        )
        current = await read_activity_receipt(self._receipts, receipt_key)
        if current is None:
            raise DurableSandboxError("sandbox tool receipt disappeared")
        receipt, revision = current
        _validate_receipt(receipt, request)
        updated = receipt.model_copy(
            update={
                "result_ref": result_ref,
                "status": (
                    ActivityReceiptStatus.SUCCEEDED
                    if result.status is ToolResultStatus.SUCCEEDED
                    else ActivityReceiptStatus.FAILED
                ),
                "updated_at": datetime.now(UTC),
                "workspace_ref": result.workspace_ref,
            }
        )
        if not await replace_activity_receipt(
            self._receipts,
            receipt_key,
            updated,
            revision=revision,
        ):
            raise DurableSandboxError("sandbox tool receipt fence changed")

    async def _require_catalog(
        self,
    ) -> tuple[HybridToolManifest, CapturedContentPackage]:
        await self.discover()
        assert self._catalog_manifest is not None
        assert self._catalog_package is not None
        return self._catalog_manifest, self._catalog_package


async def _return_package(
    package: CapturedContentPackage,
) -> CapturedContentPackage:
    return package


def _retained_document(
    request: ToolRequestV1,
    lease: InvocationSandboxLease,
    *,
    capacity_key: str,
    capacity_revision: str,
    expires_at: datetime,
    workspace_ref: ContentRefV1 | None = None,
) -> _RetainedSandboxV1:
    expected = lease.expected_manifest
    return _RetainedSandboxV1(
        run_id=request.run_id,
        session_id=request.session_id,
        generation=expected.generation,
        sandbox_id=lease.persisted_binding.sandbox_id,
        group_resource_id=lease.persisted_binding.group.resource_id,
        region=lease.persisted_binding.group.region,
        operation_id=request.call_key,
        expected_manifest=asdict(expected),
        tool_manifest=lease.manifest.model_dump(mode="json"),
        package_digest_kind=lease.package.digest_kind,
        package_digest=lease.package.digest,
        workspace_ref=workspace_ref,
        capacity_key=capacity_key,
        capacity_revision=capacity_revision,
        expires_at=expires_at,
    )


def _sandbox_result(
    request: ToolRequestV1,
    invocation: HybridToolInvocationResult,
    workspace_ref: ContentRefV1,
) -> ToolResultV1:
    status = invocation.status
    if status is HybridInvocationStatus.SUCCESS:
        return ToolResultV1(
            run_id=request.run_id,
            step_index=request.step_index,
            call_ordinal=request.call_ordinal,
            provider_call_id=request.provider_call_id,
            call_key=request.call_key,
            request_hash=request.request_hash,
            tool_name=request.tool_name,
            status=ToolResultStatus.SUCCEEDED,
            value={
                "exit_code": invocation.exit_code,
                "stderr": invocation.stderr,
                "stdout": invocation.stdout,
                "timings": invocation.timings.model_dump(mode="json"),
                "value": invocation.value,
            },
            elapsed_ms=invocation.timings.execution_ms,
            workspace_ref=workspace_ref,
        )
    error = invocation.error
    return ToolResultV1(
        run_id=request.run_id,
        step_index=request.step_index,
        call_ordinal=request.call_ordinal,
        provider_call_id=request.provider_call_id,
        call_key=request.call_key,
        request_hash=request.request_hash,
        tool_name=request.tool_name,
        status=ToolResultStatus.FAILED,
        elapsed_ms=invocation.timings.execution_ms,
        workspace_ref=workspace_ref,
        error=ErrorEnvelopeV1(
            code=error.code if error is not None else "sandbox_tool_failed",
            classification="sandbox",
            retryable=bool(error is not None and error.retryable),
            phase="tool_step",
            step_index=request.step_index,
            call_key=request.call_key,
        ),
    )


def _ambiguous_result(request: ToolRequestV1) -> ToolResultV1:
    return ToolResultV1(
        run_id=request.run_id,
        step_index=request.step_index,
        call_ordinal=request.call_ordinal,
        provider_call_id=request.provider_call_id,
        call_key=request.call_key,
        request_hash=request.request_hash,
        tool_name=request.tool_name,
        status=ToolResultStatus.AMBIGUOUS,
        elapsed_ms=0.0,
        error=ErrorEnvelopeV1(
            code="sandbox_effect_unreconciled",
            classification="sandbox",
            retryable=False,
            disposition=ErrorDisposition.AMBIGUOUS,
            possibly_committed=True,
            phase="tool_step",
            step_index=request.step_index,
            call_key=request.call_key,
        ),
    )


def _validate_receipt(
    receipt: ActivityReceiptV1,
    request: ToolRequestV1,
) -> None:
    if receipt.operation_key != request.call_key or receipt.request_hash != request.request_hash:
        raise DurableSandboxError("sandbox tool receipt request changed")


def _verify_manifest(
    observed: HybridToolManifest,
    expected: HybridToolManifest,
) -> None:
    if observed != expected:
        raise DurableSandboxError("sandbox tool manifest changed")


def _verify_workspace_bindings(
    artifact: WorkspaceArtifactV1,
    request: ToolRequestV1,
) -> None:
    if (
        artifact.package_hash != request.package_hash
        or artifact.policy_hash != request.policy_hash
        or artifact.catalog_hash != request.catalog_hash
    ):
        raise DurableSandboxError("workspace checkpoint binding changed")


def _verify_retained_bindings(
    document: _RetainedSandboxV1,
    request: ToolRequestV1,
    manifest: HybridToolManifest,
    package: CapturedContentPackage,
) -> None:
    if (
        document.session_id != request.session_id
        or document.package_digest != package.digest
        or document.tool_manifest != manifest.model_dump(mode="json")
    ):
        raise DurableSandboxError("retained sandbox binding changed")


def _raise_retained_release_conflict() -> None:
    raise DurableSandboxError("retained sandbox release fence changed")


def _package_hash(package: CapturedContentPackage) -> str:
    prefix, separator, digest = package.digest.partition(":")
    if prefix != "sha256" or not separator or len(digest) != 64:
        raise DurableSandboxError("sandbox package digest is invalid")
    return digest


def _retained_key(session_id: str) -> str:
    return f"retained-sandboxes/{canonical_hash({'session_id': session_id})}"


def _package_root(settings: HybridSandboxSettings) -> Path:
    return settings.tool_bundle_root or get_app_root()


def _required_string(value: dict[str, object], name: str) -> str:
    observed = value.get(name)
    if not isinstance(observed, str) or not observed:
        raise DurableSandboxError("retained sandbox manifest is invalid")
    return observed


def _required_int(value: dict[str, object], name: str) -> int:
    observed = value.get(name)
    if isinstance(observed, bool) or not isinstance(observed, int) or observed < 0:
        raise DurableSandboxError("retained sandbox manifest is invalid")
    return observed


def _expected_manifest(
    document: _RetainedSandboxV1,
) -> ExpectedSandboxManifestBinding:
    return ExpectedSandboxManifestBinding.create(
        manifest_version=_required_int(
            document.expected_manifest,
            "manifest_version",
        ),
        protocol_version=_required_string(
            document.expected_manifest,
            "protocol_version",
        ),
        session_id=_required_string(
            document.expected_manifest,
            "session_id",
        ),
        owner_hash_version=_required_string(
            document.expected_manifest,
            "owner_hash_version",
        ),
        owner_hash=_required_string(
            document.expected_manifest,
            "owner_hash",
        ),
        app_hash=_required_string(
            document.expected_manifest,
            "app_hash",
        ),
        sandbox_group_resource_id=_required_string(
            document.expected_manifest,
            "sandbox_group_resource_id",
        ),
        sandbox_id=_required_string(
            document.expected_manifest,
            "sandbox_id",
        ),
        generation=_required_int(
            document.expected_manifest,
            "generation",
        ),
        digest_kind=_required_string(
            document.expected_manifest,
            "digest_kind",
        ),
        digest=_required_string(
            document.expected_manifest,
            "digest",
        ),
        state_store_fingerprint=_required_string(
            document.expected_manifest,
            "state_store_fingerprint",
        ),
    )


def _validated_inspection_manifest(
    document: _RetainedSandboxV1,
    *,
    run_id: str,
    session_id: str,
) -> ExpectedSandboxManifestBinding:
    del run_id
    if document.session_id != session_id:
        raise DurableSandboxError("retained sandbox inspection fence changed")
    expected = _expected_manifest(document)
    if (
        expected.sandbox_id != document.sandbox_id
        or expected.generation != document.generation
        or expected.session_id != document.session_id
        or expected.sandbox_group_resource_id != document.group_resource_id
    ):
        raise DurableSandboxError("retained sandbox inspection binding changed")
    return expected


def _provider_factory(
    settings: HybridSandboxSettings,
) -> SandboxProviderFactory:
    async def open_provider() -> SandboxSessionProvider:
        from ..transport.aca_sdk import AcaSandboxAdapter

        return await AcaSandboxAdapter.open(
            settings.group_resource_id,
            region=settings.region,
        )

    return open_provider
