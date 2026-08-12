"""Activation and routing checks for one persistent sandbox session."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from .._logger import logger
from .._observability import current_span
from ..config import DEFAULT_TIMEOUT
from ..harness.bootstrap_report import (
    BootstrapErrorReport,
    BootstrapReportError,
    parse_bootstrap_error_report,
)
from ..harness.sandbox_capabilities import REQUIRED_HARNESS_CAPABILITIES
from ..journal_paths import (
    ATOMIC_CHECKPOINT_POINTER_PATH,
    BOOTSTRAP_ERROR_PATH,
    HARNESS_PROTOCOL_PATH,
    validate_checkpoint_name,
)
from ..sandbox_runtime_limits import lifecycle_auto_delete_seconds
from ..session_state import (
    ActiveRunConflictError,
    AppIdentity,
    ConcurrencyConflictError,
    DurableOperationPhase,
    DurableOwnerIdempotencyRecord,
    DurableRunRecord,
    DurableRunStatus,
    DurableSessionOperation,
    DurableSessionRecord,
    OperationRowNotFoundError,
    OwnerContext,
    OwnerPartition,
    ProvisionSubmitOutcome,
    ProvisionSubmitRecords,
    RunRowNotFoundError,
    SessionOperationFence,
    SessionOperationTarget,
    SessionRead,
    SessionStateContractError,
    SessionStateStore,
    StaleOperationTokenError,
    operation_correlation_label,
    operation_id_for_sequence,
    owner_idempotency_expiry,
    owner_partition,
    verify_app_hash,
    verify_owner_hash,
)
from ..session_state.errors import (
    SessionNotAdmissibleError,
    SessionRowNotFoundError,
)
from ..strict_json import DuplicateJsonKeyError, decode_json_object
from ..transport.manifest import ExpectedSandboxManifestBinding, SandboxManifestMismatchError
from ..transport.ports import SandboxSessionHandle, SandboxSessionProvider
from ..transport.transport_models import (
    PersistedSandboxBinding,
    SandboxCapacityError,
    SandboxCreateRequest,
    SandboxCreateSource,
    SandboxFileNotFoundError,
    SandboxFileOperationError,
    SandboxGroupBinding,
    SandboxLifecyclePolicy,
    SandboxProvisioningLabels,
)
from .bootstrap_delivery import deliver_content_and_bootstrap
from .idempotency import IdempotencyAttempt
from .package import (
    CapturedContentPackage,
    ContentBindingMismatchError,
    ContentPackagingError,
    LiveManifestNotReadyError,
    build_expected_manifest_binding,
    get_content_package,
    read_live_manifest_binding,
)
from .sandbox_config import SandboxCreateProfile

DEFAULT_AUTO_SUSPEND_SECONDS = 300
DEFAULT_RECLAIM_IDLE_SECONDS = 86_400
DEFAULT_PROTOCOL_VERSION = "1"
_TOUCHABLE_SESSION_STATUSES = frozenset(
    {"ready", "running", "canceling", "suspending", "suspended", "resuming"}
)
QUARANTINE_REASONS: frozenset[str] = frozenset(
    {
        "sandbox_manifest_mismatch",
        "routing_binding_changed",
        "state_store_fingerprint_mismatch",
        "protocol_version_mismatch",
        "capability_mismatch",
        "checkpoint_corrupt",
        "bootstrap_failure",
        "journal_corrupt",
        "generation_rollback",
        "platform_binding_mismatch",
    }
)
_MANIFEST_RETRY_INTERVAL_SECONDS = 0.25
_RESUMABLE_FILE_OPERATION_STATUS_CODES = frozenset({409, 423, 425, 429, 500, 502, 503, 504})

type _SessionLockKey = tuple[str, str]
type TargetedReconciler = Callable[[OwnerPartition, str], Awaitable[None]]
type BoundedReconciler = Callable[[], Awaitable[None]]


class SetupDeadline(Protocol):
    """The small deadline surface the activation gate needs from execution."""

    def remaining_setup_seconds(self) -> float:
        """Return time remaining before a run may be launched."""


class SessionActivationError(RuntimeError):
    """Base class for a session that cannot safely activate."""


class SessionActivationNotFoundError(SessionActivationError):
    """The requested owner/session binding is absent or cannot be trusted."""


class SessionActivationGoneError(SessionActivationError):
    """The requested session belongs to a retired content epoch."""


class SessionActivationSetupTimeoutError(SessionActivationError):
    """The setup deadline elapsed before a sandbox was ready to receive a run."""


class SessionCreationUnavailableError(SessionActivationError):
    """No explicit runtime-owned bootstrap source is available for a new sandbox."""


class SessionBindingChangedError(SessionActivationError):
    """The durable routing fields changed after admission."""

    def __init__(self, fields: frozenset[str]) -> None:
        self.fields = fields
        super().__init__(
            "Session routing binding changed before submission: "
            f"{', '.join(sorted(fields))}."
        )


class SessionRunOwnershipChangedError(SessionActivationError):
    """The session no longer owns the admitted run before submission."""


class SessionReadinessArtifactError(SessionActivationError):
    """A harness-published protocol, capability, or checkpoint artifact was unsafe."""

    def __init__(self, reason: str) -> None:
        self.reason = _validate_quarantine_reason(reason)
        super().__init__(f"Sandbox readiness artifact failed validation: {self.reason}.")


class _HarnessProtocolArtifact(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    protocol_version: str
    capabilities: dict[str, str]


@dataclass(frozen=True, slots=True)
class StateStoreBinding:
    """A state-store seam paired with the live, non-secret storage fingerprint."""

    store: SessionStateStore
    state_store_fingerprint: str

    @classmethod
    def create(
        cls,
        *,
        store: SessionStateStore,
        state_store_fingerprint: str,
    ) -> StateStoreBinding:
        if not state_store_fingerprint:
            raise ValueError("state_store_fingerprint must be non-empty")
        return cls(store=store, state_store_fingerprint=state_store_fingerprint)


@dataclass(slots=True)
class _AsyncSingleton[T]:
    """Lazily create one app-scoped async resource without startup I/O."""

    factory: Callable[[], Awaitable[T]]
    _value: T | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def get(self) -> T:
        if self._value is not None:
            return self._value
        async with self._lock:
            if self._value is None:
                self._value = await self.factory()
            return self._value


@dataclass(slots=True)
class _SessionLockRegistry:
    """Keep short activation and cancellation windows serialized per owner/session pair."""

    _locks: dict[_SessionLockKey, asyncio.Lock] = field(default_factory=dict)
    _waiters: dict[_SessionLockKey, int] = field(default_factory=dict)
    _guard: asyncio.Lock = field(default_factory=asyncio.Lock)

    @asynccontextmanager
    async def hold(
        self,
        key: _SessionLockKey,
        *,
        setup_deadline: SetupDeadline | None = None,
    ) -> AsyncIterator[None]:
        try:
            lock = await self._acquire(key, setup_deadline=setup_deadline)
        except TimeoutError:
            raise SessionActivationSetupTimeoutError(
                "Sandbox setup did not complete before the setup deadline."
            ) from None
        try:
            yield
        finally:
            lock.release()
            await self._release_waiter(key)

    async def _acquire(
        self,
        key: _SessionLockKey,
        *,
        setup_deadline: SetupDeadline | None,
    ) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.setdefault(key, asyncio.Lock())
            self._waiters[key] = self._waiters.get(key, 0) + 1
        try:
            if setup_deadline is None:
                await lock.acquire()
            else:
                async with asyncio.timeout(setup_deadline.remaining_setup_seconds()):
                    await lock.acquire()
        except BaseException:
            await self._release_waiter(key)
            raise
        return lock

    async def _release_waiter(self, key: _SessionLockKey) -> None:
        async with self._guard:
            remaining = self._waiters[key] - 1
            if remaining == 0:
                del self._waiters[key]
                self._locks.pop(key, None)
            else:
                self._waiters[key] = remaining


@dataclass(frozen=True, slots=True)
class SessionRuntimeBinding:
    """App-scoped dependencies for request-time sandbox activation."""

    app_identity: AppIdentity
    sandbox_group_resource_id: str
    script_root: Path
    creation_source: SandboxCreateSource | None
    create_profile: SandboxCreateProfile | None
    auto_suspend_seconds: int
    reclaim_idle_seconds: int
    protocol_version: str
    _provider: _AsyncSingleton[SandboxSessionProvider] = field(repr=False, compare=False)
    _state_store: _AsyncSingleton[StateStoreBinding] = field(repr=False, compare=False)
    _session_locks: _SessionLockRegistry = field(repr=False, compare=False)
    _targeted_reconciler: TargetedReconciler | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _post_create_reconciler: BoundedReconciler | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _capacity_reaper: BoundedReconciler | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    @classmethod
    def create(
        cls,
        *,
        app_identity: AppIdentity,
        sandbox_group_resource_id: str,
        script_root: Path,
        provider_factory: Callable[[], Awaitable[SandboxSessionProvider]],
        state_store_factory: Callable[[], Awaitable[StateStoreBinding]],
        creation_source: SandboxCreateSource | None = None,
        create_profile: SandboxCreateProfile | None = None,
        auto_suspend_seconds: int = DEFAULT_AUTO_SUSPEND_SECONDS,
        reclaim_idle_seconds: int = DEFAULT_RECLAIM_IDLE_SECONDS,
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
        targeted_reconciler: TargetedReconciler | None = None,
        post_create_reconciler: BoundedReconciler | None = None,
        capacity_reaper: BoundedReconciler | None = None,
    ) -> SessionRuntimeBinding:
        group_resource_id = sandbox_group_resource_id.strip()
        if not group_resource_id:
            raise ValueError("sandbox_group_resource_id must be non-empty")
        if auto_suspend_seconds <= 0:
            raise ValueError("auto_suspend_seconds must be positive")
        if reclaim_idle_seconds <= auto_suspend_seconds:
            raise ValueError("reclaim_idle_seconds must exceed auto_suspend_seconds")
        if not protocol_version.strip():
            raise ValueError("protocol_version must be non-empty")
        return cls(
            app_identity=app_identity,
            sandbox_group_resource_id=group_resource_id,
            script_root=script_root,
            creation_source=creation_source,
            create_profile=create_profile,
            auto_suspend_seconds=auto_suspend_seconds,
            reclaim_idle_seconds=reclaim_idle_seconds,
            protocol_version=protocol_version,
            _provider=_AsyncSingleton(provider_factory),
            _state_store=_AsyncSingleton(state_store_factory),
            _session_locks=_SessionLockRegistry(),
            _targeted_reconciler=targeted_reconciler,
            _post_create_reconciler=post_create_reconciler,
            _capacity_reaper=capacity_reaper,
        )

    async def get_provider(self) -> SandboxSessionProvider:
        """Return the one lazily opened provider for this app's Sandbox Group."""
        return await self._provider.get()

    async def get_state_store(self) -> StateStoreBinding:
        """Return the one lazily resolved state-store binding for this app."""
        return await self._state_store.get()

    async def reconcile_session(
        self,
        partition: OwnerPartition,
        session_id: str,
    ) -> None:
        """Run the shared targeted lifecycle reconciliation when configured."""
        if self._targeted_reconciler is not None:
            await self._targeted_reconciler(partition, session_id)

    async def reconcile_after_create(self) -> None:
        """Run the awaited bounded post-create cleanup when configured."""
        if self._post_create_reconciler is not None:
            await self._post_create_reconciler()

    async def reap_for_capacity(self) -> None:
        """Run the awaited bounded capacity cleanup when configured."""
        if self._capacity_reaper is not None:
            await self._capacity_reaper()

    @asynccontextmanager
    async def hold_session(
        self,
        partition: OwnerPartition,
        session_id: str,
        *,
        setup_deadline: SetupDeadline | None = None,
    ) -> AsyncIterator[None]:
        """Serialize readiness-sensitive operations for one owner/session pair."""
        async with self._session_locks.hold(
            (partition.partition_key, session_id),
            setup_deadline=setup_deadline,
        ):
            yield


@dataclass(frozen=True, slots=True)
class ActivatedSession:
    """A durable session row and an already-proven live sandbox handle."""

    handle: SandboxSessionHandle
    session: DurableSessionRecord
    etag: str
    partition: OwnerPartition
    store: SessionStateStore

    @classmethod
    def create(
        cls,
        *,
        handle: SandboxSessionHandle,
        session: DurableSessionRecord,
        etag: str,
        partition: OwnerPartition,
        store: SessionStateStore,
    ) -> ActivatedSession:
        if not etag:
            raise ValueError("etag must be non-empty")
        return cls(
            handle=handle,
            session=session,
            etag=etag,
            partition=partition,
            store=store,
        )


@dataclass(frozen=True, slots=True)
class ProvisionedSubmission:
    """A reserved first run and, unless replayed, its provisioned live sandbox."""

    outcome: ProvisionSubmitOutcome
    activated: ActivatedSession | None


async def activate_session(
    runtime: SessionRuntimeBinding,
    owner: OwnerContext,
    session_id: str,
    setup_deadline: SetupDeadline,
    *,
    allow_create: bool,
) -> ActivatedSession:
    """Resolve, cross-check, and bind one session before a run can be submitted."""
    state_binding = await _within_setup_budget(runtime.get_state_store(), setup_deadline)
    partition = owner_partition(owner)
    store = state_binding.store
    try:
        session_read = await _within_setup_budget(
            store.get_session(partition, session_id),
            setup_deadline,
        )
    except SessionRowNotFoundError:
        if not allow_create:
            raise SessionActivationNotFoundError("Session was not found for this owner.") from None
        return await _create_and_activate_session(
            runtime,
            owner,
            partition,
            session_id,
            state_binding,
            setup_deadline,
        )

    session = session_read.record
    _verify_owner_binding(owner, session)
    _verify_state_store_binding(session, state_binding.state_store_fingerprint)
    package = await _capture_current_package(runtime.script_root, setup_deadline)
    if _digest_pair(session) != _digest_pair(package):
        await _drain_changed_epoch(store, session, session_read.etag)
        raise SessionActivationGoneError(
            "Session content belongs to a retired deployment epoch."
        )
    if session.status in {"tombstoned", "deleted"}:
        raise SessionActivationGoneError("Session has been retired.")
    if session.status == "quarantined":
        raise SessionActivationNotFoundError("Session routing binding cannot be trusted.")
    if session.sandbox_id is None:
        raise SessionActivationNotFoundError("Session has no usable sandbox binding.")

    expected = build_expected_manifest_binding(
        session,
        sandbox_group_resource_id=runtime.sandbox_group_resource_id,
        state_store_fingerprint=state_binding.state_store_fingerprint,
    )
    persisted = PersistedSandboxBinding.create(
        session.sandbox_id,
        SandboxGroupBinding.create(runtime.sandbox_group_resource_id, session.region),
    )
    provider = await _within_setup_budget(runtime.get_provider(), setup_deadline)
    handle: SandboxSessionHandle | None = None
    try:
        if session.status == "suspended":
            handle = await _within_setup_budget(
                provider.resume(
                    persisted,
                    expected,
                    readiness_timeout_seconds=_remaining_setup_seconds(setup_deadline),
                ),
                setup_deadline,
            )
        else:
            handle = await _within_setup_budget(
                provider.attach(
                    persisted,
                    expected,
                    readiness_timeout_seconds=_remaining_setup_seconds(setup_deadline),
                ),
                setup_deadline,
            )
        await _within_setup_budget(
            _verify_optional_harness_artifacts(
                handle,
                session,
                require_protocol=True,
            ),
            setup_deadline,
        )
    except SandboxManifestMismatchError:
        await _quarantine_detected_binding(
            store,
            session,
            session_read.etag,
            reason="sandbox_manifest_mismatch",
        )
        _record_security_event("sandbox_manifest_mismatch", frozenset({"manifest"}))
        raise SessionActivationNotFoundError(
            "Session sandbox binding cannot be trusted."
        ) from None
    except SessionReadinessArtifactError as exc:
        if handle is not None:
            with suppress(Exception):
                await handle.close()
        await _quarantine_detected_binding(
            store,
            session,
            session_read.etag,
            reason=exc.reason,
        )
        _record_security_event(exc.reason, frozenset({"harness_artifact"}))
        raise SessionActivationNotFoundError(
            "Session sandbox readiness artifacts cannot be trusted."
        ) from None
    assert handle is not None
    return ActivatedSession.create(
        handle=handle,
        session=session,
        etag=session_read.etag,
        partition=partition,
        store=store,
    )


async def revalidate_before_submit(
    activated: ActivatedSession,
    admitted_run: DurableRunRecord,
) -> None:
    """Detect routing changes after admission before any sandbox work is launched."""
    reread = await activated.store.get_session(activated.partition, activated.session.session_id)
    if (
        reread.record.status != "running"
        or reread.record.active_run_id != admitted_run.run_id
    ):
        raise SessionRunOwnershipChangedError(
            "Session no longer owns the admitted run before submission."
        )
    fields = _routing_differences(activated.session, reread.record)
    if not fields:
        return

    failed_run = _terminal_run(admitted_run, reason="routing_binding_changed")
    await activated.store.adopt_terminal_run(failed_run)
    released = await activated.store.get_session(
        activated.partition,
        activated.session.session_id,
    )
    quarantined = _quarantined_session(
        released.record,
        reason="routing_binding_changed",
        updated_at=datetime.now(UTC),
    )
    await activated.store.update_session(
        previous=released.record,
        updated=quarantined,
        etag=released.etag,
    )
    _record_security_event("routing_binding_changed", fields)
    raise SessionBindingChangedError(fields)


def session_with_admitted_run(
    session: DurableSessionRecord,
    run_id: str,
    *,
    updated_at: datetime,
) -> DurableSessionRecord:
    """Build the session-side row for one entity-group admission."""
    return DurableSessionRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        sandbox_id=session.sandbox_id,
        generation=session.generation,
        digest_kind=session.digest_kind,
        digest=session.digest,
        protocol=session.protocol,
        status="running",
        last_activity_at=updated_at,
        expires_at=session.expires_at,
        idle_policy_armed=False,
        active_run_id=run_id,
        snapshot_ids=session.snapshot_ids,
        region=session.region,
        state_store_fingerprint=session.state_store_fingerprint,
        quarantine_reason=session.quarantine_reason,
        tombstone_reason=session.tombstone_reason,
        created_at=session.created_at,
        updated_at=updated_at,
        active_operation_id=session.active_operation_id,
        operation_sequence=session.operation_sequence,
    )


def terminal_run(
    run: DurableRunRecord,
    *,
    status: DurableRunStatus,
    result_available: bool,
    reason: str | None,
    updated_at: datetime,
) -> DurableRunRecord:
    """Build one validated terminal adoption record from an admitted run."""
    return DurableRunRecord.create(
        owner_partition=run.owner_partition,
        session_id=run.session_id,
        run_id=run.run_id,
        generation=run.generation,
        status=status,
        result_available=result_available,
        status_reason=reason,
        expires_at=run.expires_at,
        created_at=run.created_at,
        updated_at=updated_at,
        agent_slug=run.agent_slug,
    )


def lifecycle_policy_for_idle(runtime: SessionRuntimeBinding) -> SandboxLifecyclePolicy:
    """Build the complete per-sandbox policy used at create and after terminal adoption."""
    return SandboxLifecyclePolicy.create(
        auto_suspend_seconds=runtime.auto_suspend_seconds,
        auto_suspend_mode="Disk",
        auto_delete_seconds=lifecycle_auto_delete_seconds(runtime.reclaim_idle_seconds),
    )


async def rearm_idle_lifecycle(
    runtime: SessionRuntimeBinding,
    activated: ActivatedSession,
) -> bool:
    """Finalize an active submit operation before re-arming its idle policy."""
    current = await activated.store.get_session(
        activated.partition,
        activated.session.session_id,
    )
    if current.record.active_operation_id is not None:
        try:
            operation = await activated.store.get_operation(
                activated.partition,
                activated.session.session_id,
                current.record.active_operation_id,
            )
        except OperationRowNotFoundError as exc:
            raise SessionStateContractError(
                "disarmed idle lifecycle requires its active durable operation"
            ) from exc
        if (
            operation.record.kind not in {"provision_submit", "submit_run"}
            or operation.record.target.run_id is None
        ):
            return False
        return await finalize_submit_operation(
            runtime,
            activated,
            expected_run_id=operation.record.target.run_id,
        )
    if current.record.active_run_id is not None or current.record.status in {
        "deleting",
        "deleted",
        "tombstoned",
    }:
        return False
    if current.record.idle_policy_armed:
        return False
    raise SessionStateContractError(
        "disarmed idle lifecycle requires an active durable operation"
    )


async def touch_session_activity(
    runtime: SessionRuntimeBinding,
    owner: OwnerContext,
    session_id: str,
) -> None:
    """Reset one authorized session's idle wall clock for a management request."""
    state_binding = await runtime.get_state_store()
    partition = owner_partition(owner)
    for attempt in range(2):
        current = await state_binding.store.get_session(partition, session_id)
        if current.record.status not in _TOUCHABLE_SESSION_STATUSES:
            return
        updated = _session_with_touched_activity(
            current.record,
            reclaim_idle_seconds=runtime.reclaim_idle_seconds,
            updated_at=datetime.now(UTC),
        )
        try:
            await state_binding.store.update_session(
                previous=current.record,
                updated=updated,
                etag=current.etag,
            )
        except ConcurrencyConflictError:
            if attempt == 1:
                return
            continue
        return


def _session_with_touched_activity(
    session: DurableSessionRecord,
    *,
    reclaim_idle_seconds: int,
    updated_at: datetime,
) -> DurableSessionRecord:
    return DurableSessionRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        sandbox_id=session.sandbox_id,
        generation=session.generation,
        digest_kind=session.digest_kind,
        digest=session.digest,
        protocol=session.protocol,
        status=session.status,
        last_activity_at=updated_at,
        expires_at=updated_at + timedelta(seconds=reclaim_idle_seconds),
        idle_policy_armed=session.idle_policy_armed,
        active_run_id=session.active_run_id,
        snapshot_ids=session.snapshot_ids,
        region=session.region,
        state_store_fingerprint=session.state_store_fingerprint,
        quarantine_reason=session.quarantine_reason,
        tombstone_reason=session.tombstone_reason,
        created_at=session.created_at,
        updated_at=updated_at,
        active_operation_id=session.active_operation_id,
        operation_sequence=session.operation_sequence,
    )


async def begin_submit_operation(
    activated: ActivatedSession,
    run: DurableRunRecord,
    *,
    agent_slug: str = "",
) -> tuple[ActivatedSession, SessionOperationFence]:
    """Fence an existing idle session before its lifecycle policy is disabled."""
    current = await activated.store.get_session(
        activated.partition,
        activated.session.session_id,
    )
    if current.record.active_run_id is not None:
        raise ActiveRunConflictError(
            "session already has an active run",
            active_run_id=current.record.active_run_id,
        )
    if (
        current.record.active_operation_id is not None
        or current.record.sandbox_id is None
        or current.record.status not in {"ready", "suspended"}
    ):
        raise SessionNotAdmissibleError("session cannot begin a submitted run operation")
    now = run.updated_at
    sequence = current.record.operation_sequence + 1
    operation = DurableSessionOperation.create(
        owner_partition=current.record.owner_partition,
        target=SessionOperationTarget.create(
            session_id=current.record.session_id,
            sandbox_id=current.record.sandbox_id,
            generation=current.record.generation,
            digest_kind=current.record.digest_kind,
            digest=current.record.digest,
            run_id=run.run_id,
        ),
        sequence=sequence,
        kind="submit_run",
        phase="submit_disarm",
        state="active",
        correlation_label=operation_correlation_label(current.record.session_id, sequence),
        token=uuid4().hex,
        attempt_count=0,
        error_code=None,
        lease_expires_at=now + timedelta(seconds=60),
        next_attempt_at=None,
        created_at=now,
        updated_at=now,
        finished_at=None,
        agent_slug=agent_slug,
    )
    prepared = _session_with_active_operation(
        current.record,
        operation=operation,
        idle_policy_armed=False,
        updated_at=now,
    )
    fence = await activated.store.begin_operation(
        previous=current.record,
        updated=prepared,
        operation=operation,
        etag=current.etag,
    )
    current_prepared = await activated.store.get_session(
        activated.partition,
        activated.session.session_id,
    )
    return (
        ActivatedSession.create(
            handle=activated.handle,
            session=current_prepared.record,
            etag=current_prepared.etag,
            partition=activated.partition,
            store=activated.store,
        ),
        fence,
    )


async def disarm_submit_lifecycle(
    runtime: SessionRuntimeBinding,
    activated: ActivatedSession,
    fence: SessionOperationFence,
) -> tuple[ActivatedSession, SessionOperationFence]:
    """Disable suspend only while the same durable submit fence is current."""
    try:
        current = await activated.handle.get_lifecycle_policy()
        disabled = SandboxLifecyclePolicy.create(
            auto_suspend_seconds=None,
            auto_suspend_mode=current.auto_suspend_mode,
            auto_delete_seconds=current.auto_delete_seconds,
        )
        await activated.handle.set_lifecycle_policy(disabled)
    except BaseException:
        with suppress(StaleOperationTokenError):
            await activated.store.advance_operation(
                fence=fence,
                phase="submit_disarm",
                error_code="lifecycle_policy_disable_failed",
                updated_at=datetime.now(UTC),
            )
        raise
    advanced = await activated.store.advance_operation(
        fence=fence,
        phase="submit_admission",
        updated_at=datetime.now(UTC),
    )
    current_session = await activated.store.get_session(
        activated.partition,
        activated.session.session_id,
    )
    return (
        ActivatedSession.create(
            handle=activated.handle,
            session=current_session.record,
            etag=current_session.etag,
            partition=activated.partition,
            store=activated.store,
        ),
        advanced,
    )


async def finalize_submit_operation(
    runtime: SessionRuntimeBinding,
    activated: ActivatedSession,
    *,
    expected_run_id: str,
) -> bool:
    """Re-arm a terminal submitted run before releasing its durable operation."""
    current = await activated.store.get_session(
        activated.partition,
        activated.session.session_id,
    )
    if current.record.active_operation_id is None:
        return False
    if current.record.status in {"tombstoned", "deleting", "deleted"}:
        return False
    try:
        operation = await activated.store.get_operation(
            activated.partition,
            activated.session.session_id,
            current.record.active_operation_id,
        )
    except OperationRowNotFoundError:
        return False
    if operation.record.target.run_id != expected_run_id:
        return False
    fence = await activated.store.resume_operation(
        owner_partition=activated.partition,
        session_id=activated.session.session_id,
        token=uuid4().hex,
        updated_at=datetime.now(UTC),
    )
    if fence is None or fence.kind not in {"provision_submit", "submit_run"}:
        return False
    if (
        fence.target.sandbox_id != activated.session.sandbox_id
        or fence.target.generation != activated.session.generation
        or fence.target.run_id is None
        or fence.target.run_id != expected_run_id
    ):
        return False
    try:
        run = await activated.store.get_run(
            activated.partition,
            activated.session.session_id,
            fence.target.run_id,
        )
    except RunRowNotFoundError:
        await activated.handle.set_lifecycle_policy(lifecycle_policy_for_idle(runtime))
        released = _session_after_missing_submit_run(
            current.record,
            reclaim_idle_seconds=runtime.reclaim_idle_seconds,
            updated_at=datetime.now(UTC),
        )
        await activated.store.abort_operation(
            fence=fence,
            updated_session=released,
            error_code="submit_run_missing",
            updated_at=released.updated_at,
        )
        return True
    if run.record.status not in {"succeeded", "failed", "canceled", "timed_out", "abandoned"}:
        return False
    rearm_session = _session_before_submit_rearm(
        current.record,
        updated_at=datetime.now(UTC),
    )
    phase: DurableOperationPhase = (
        "provision_rearm" if fence.kind == "provision_submit" else "submit_rearm"
    )
    fence = await activated.store.advance_operation(
        fence=fence,
        phase=phase,
        updated_at=rearm_session.updated_at,
        updated_session=rearm_session,
    )
    try:
        await activated.handle.set_lifecycle_policy(lifecycle_policy_for_idle(runtime))
    except BaseException:
        with suppress(StaleOperationTokenError):
            await activated.store.advance_operation(
                fence=fence,
                phase=phase,
                error_code="lifecycle_policy_apply_failed",
                updated_at=datetime.now(UTC),
            )
        raise
    armed = _session_after_submit_rearm(
        rearm_session,
        reclaim_idle_seconds=runtime.reclaim_idle_seconds,
        updated_at=datetime.now(UTC),
    )
    try:
        await activated.store.complete_operation(
            fence=fence,
            updated_session=armed,
            updated_at=armed.updated_at,
        )
    except StaleOperationTokenError:
        return False
    return True


async def abort_submit_operation(
    runtime: SessionRuntimeBinding,
    activated: ActivatedSession,
    fence: SessionOperationFence,
) -> None:
    """Restore idle policy and release a submit operation that never admitted its run."""
    current = await activated.store.get_session(
        activated.partition,
        activated.session.session_id,
    )
    if current.record.active_run_id is not None:
        return
    if current.record.status in {"tombstoned", "deleting", "deleted"}:
        return
    await activated.handle.set_lifecycle_policy(lifecycle_policy_for_idle(runtime))
    released = _session_after_submit_rearm(
        current.record,
        reclaim_idle_seconds=runtime.reclaim_idle_seconds,
        updated_at=datetime.now(UTC),
    )
    await activated.store.abort_operation(
        fence=fence,
        updated_session=released,
        error_code="submit_admission_failed",
        updated_at=released.updated_at,
    )


def _session_with_active_operation(
    session: DurableSessionRecord,
    *,
    operation: DurableSessionOperation,
    idle_policy_armed: bool,
    updated_at: datetime,
) -> DurableSessionRecord:
    return DurableSessionRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        sandbox_id=session.sandbox_id,
        generation=session.generation,
        digest_kind=session.digest_kind,
        digest=session.digest,
        protocol=session.protocol,
        status=session.status,
        last_activity_at=session.last_activity_at,
        expires_at=session.expires_at,
        idle_policy_armed=idle_policy_armed,
        active_run_id=session.active_run_id,
        snapshot_ids=session.snapshot_ids,
        region=session.region,
        state_store_fingerprint=session.state_store_fingerprint,
        quarantine_reason=session.quarantine_reason,
        tombstone_reason=session.tombstone_reason,
        created_at=session.created_at,
        updated_at=updated_at,
        active_operation_id=operation.operation_id,
        operation_sequence=operation.sequence,
    )


def _session_before_submit_rearm(
    session: DurableSessionRecord,
    *,
    updated_at: datetime,
) -> DurableSessionRecord:
    return DurableSessionRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        sandbox_id=session.sandbox_id,
        generation=session.generation,
        digest_kind=session.digest_kind,
        digest=session.digest,
        protocol=session.protocol,
        status="quarantined" if session.status == "quarantined" else "ready",
        last_activity_at=session.last_activity_at,
        expires_at=session.expires_at,
        idle_policy_armed=False,
        active_run_id=None,
        snapshot_ids=session.snapshot_ids,
        region=session.region,
        state_store_fingerprint=session.state_store_fingerprint,
        quarantine_reason=session.quarantine_reason,
        tombstone_reason=session.tombstone_reason,
        created_at=session.created_at,
        updated_at=updated_at,
        active_operation_id=session.active_operation_id,
        operation_sequence=session.operation_sequence,
    )


def _session_after_submit_rearm(
    session: DurableSessionRecord,
    *,
    reclaim_idle_seconds: int,
    updated_at: datetime,
) -> DurableSessionRecord:
    return DurableSessionRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        sandbox_id=session.sandbox_id,
        generation=session.generation,
        digest_kind=session.digest_kind,
        digest=session.digest,
        protocol=session.protocol,
        status=session.status,
        last_activity_at=updated_at,
        expires_at=updated_at + timedelta(seconds=reclaim_idle_seconds),
        idle_policy_armed=True,
        active_run_id=session.active_run_id,
        snapshot_ids=session.snapshot_ids,
        region=session.region,
        state_store_fingerprint=session.state_store_fingerprint,
        quarantine_reason=session.quarantine_reason,
        tombstone_reason=session.tombstone_reason,
        created_at=session.created_at,
        updated_at=updated_at,
        active_operation_id=None,
        operation_sequence=session.operation_sequence,
    )


def _session_after_missing_submit_run(
    session: DurableSessionRecord,
    *,
    reclaim_idle_seconds: int,
    updated_at: datetime,
) -> DurableSessionRecord:
    return DurableSessionRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        sandbox_id=session.sandbox_id,
        generation=session.generation,
        digest_kind=session.digest_kind,
        digest=session.digest,
        protocol=session.protocol,
        status="quarantined" if session.status == "quarantined" else "ready",
        last_activity_at=updated_at,
        expires_at=updated_at + timedelta(seconds=reclaim_idle_seconds),
        idle_policy_armed=True,
        active_run_id=None,
        snapshot_ids=session.snapshot_ids,
        region=session.region,
        state_store_fingerprint=session.state_store_fingerprint,
        quarantine_reason=session.quarantine_reason,
        tombstone_reason=session.tombstone_reason,
        created_at=session.created_at,
        updated_at=updated_at,
        active_operation_id=None,
        operation_sequence=session.operation_sequence,
    )


async def provision_new_session_submit(
    runtime: SessionRuntimeBinding,
    owner: OwnerContext,
    *,
    session_id: str,
    run_id: str,
    timeout: float | None,
    attempt: IdempotencyAttempt | None,
    setup_deadline: SetupDeadline,
) -> ProvisionedSubmission:
    """Reserve the first run before any sandbox create, then provision its operation."""
    if runtime.creation_source is None and runtime.create_profile is None:
        raise SessionCreationUnavailableError(
            "No runtime bootstrap source is available for new sandbox sessions."
        )
    state_binding = await _within_setup_budget(runtime.get_state_store(), setup_deadline)
    package = await _capture_current_package(runtime.script_root, setup_deadline)
    provider = await _within_setup_budget(runtime.get_provider(), setup_deadline)
    partition = owner_partition(owner)
    now = datetime.now(UTC)
    sequence = 1
    operation = DurableSessionOperation.create(
        owner_partition=partition,
        target=SessionOperationTarget.create(
            session_id=session_id,
            sandbox_id=None,
            generation=1,
            digest_kind=package.digest_kind,
            digest=package.digest,
            run_id=run_id,
        ),
        sequence=sequence,
        kind="provision_submit",
        phase="provision_create",
        state="active",
        correlation_label=operation_correlation_label(session_id, sequence),
        token=uuid4().hex,
        attempt_count=0,
        error_code=None,
        lease_expires_at=now + timedelta(seconds=60),
        next_attempt_at=None,
        created_at=now,
        updated_at=now,
        finished_at=None,
        agent_slug=owner.agent_slug,
    )
    session = DurableSessionRecord.create(
        owner_partition=partition,
        session_id=session_id,
        sandbox_id=None,
        generation=1,
        digest_kind=package.digest_kind,
        digest=package.digest,
        protocol=runtime.protocol_version,
        status="creating",
        last_activity_at=now,
        expires_at=now + timedelta(seconds=runtime.reclaim_idle_seconds),
        idle_policy_armed=False,
        active_run_id=run_id,
        snapshot_ids=(),
        region=provider.group.region,
        state_store_fingerprint=state_binding.state_store_fingerprint,
        quarantine_reason=None,
        tombstone_reason=None,
        created_at=now,
        updated_at=now,
        active_operation_id=operation_id_for_sequence(sequence),
        operation_sequence=sequence,
    )
    run = DurableRunRecord.create(
        owner_partition=partition,
        session_id=session_id,
        run_id=run_id,
        generation=session.generation,
        status="accepted",
        result_available=False,
        status_reason=None,
        expires_at=now + timedelta(
            seconds=timeout if timeout is not None else DEFAULT_TIMEOUT
        ),
        created_at=now,
        updated_at=now,
        agent_slug=owner.agent_slug,
    )
    owner_idempotency = (
        None
        if attempt is None
        else DurableOwnerIdempotencyRecord.create(
            owner_partition=partition,
            idempotency_hash=attempt.key_hash,
            request_hash=attempt.request_hash,
            session_id=session_id,
            run_id=run_id,
            expires_at=owner_idempotency_expiry(
                session.expires_at,
                run.expires_at,
                operation.lease_expires_at,
                now,
            ),
            created_at=now,
        )
    )
    outcome = await _within_setup_budget(
        state_binding.store.begin_provision_submit(
            ProvisionSubmitRecords.create(
                session,
                run,
                operation,
                owner_idempotency,
            )
        ),
        setup_deadline,
    )
    if outcome.replayed:
        existing_session = await _within_setup_budget(
            state_binding.store.get_session(partition, outcome.run.session_id),
            setup_deadline,
        )
        if existing_session.record.active_operation_id is None:
            return ProvisionedSubmission(outcome=outcome, activated=None)
        existing_operation = await _within_setup_budget(
            state_binding.store.get_operation(
                partition,
                outcome.run.session_id,
                existing_session.record.active_operation_id,
            ),
            setup_deadline,
        )
        if existing_operation.record.kind != "provision_submit":
            return ProvisionedSubmission(outcome=outcome, activated=None)
        fence = await _within_setup_budget(
            state_binding.store.takeover_expired_operation(
                owner_partition=partition,
                session_id=outcome.run.session_id,
                token=uuid4().hex,
                updated_at=datetime.now(UTC),
            ),
            setup_deadline,
        )
        if fence is None:
            raise SessionActivationSetupTimeoutError(
                "A live provision operation still holds the session setup lease."
            )
        activated = await _provision_reserved_session(
            runtime,
            state_binding,
            provider,
            existing_session.record,
            fence,
            package,
            setup_deadline,
        )
        return ProvisionedSubmission(outcome=outcome, activated=activated)
    assert outcome.fence is not None
    activated = await _provision_reserved_session(
        runtime,
        state_binding,
        provider,
        session,
        outcome.fence,
        package,
        setup_deadline,
    )
    try:
        await _within_setup_budget(runtime.reconcile_after_create(), setup_deadline)
    except BaseException:
        try:
            await activated.handle.close()
        except Exception:
            logger.exception("Could not close sandbox handle after post-create reconciliation failure")
        raise
    return ProvisionedSubmission(outcome=outcome, activated=activated)


async def _provision_reserved_session(
    runtime: SessionRuntimeBinding,
    state_binding: StateStoreBinding,
    provider: SandboxSessionProvider,
    session: DurableSessionRecord,
    fence: SessionOperationFence,
    package: CapturedContentPackage,
    setup_deadline: SetupDeadline,
) -> ActivatedSession:
    """Provision one reserved session, preserving transient file-plane work for takeover."""
    try:
        return await _provision_reserved_session_inner(
            runtime,
            state_binding,
            provider,
            session,
            fence,
            package,
            setup_deadline,
        )
    except SandboxFileOperationError as exc:
        if exc.status_code is None or exc.status_code in _RESUMABLE_FILE_OPERATION_STATUS_CODES:
            raise SessionActivationSetupTimeoutError(
                "Sandbox file-plane provisioning is temporarily unavailable."
            ) from exc
        raise


async def _provision_reserved_session_inner(
    runtime: SessionRuntimeBinding,
    state_binding: StateStoreBinding,
    provider: SandboxSessionProvider,
    session: DurableSessionRecord,
    fence: SessionOperationFence,
    package: CapturedContentPackage,
    setup_deadline: SetupDeadline,
) -> ActivatedSession:
    source = runtime.creation_source
    if source is None and runtime.create_profile is None:
        raise SessionCreationUnavailableError(
            "No runtime bootstrap source is available for new sandbox sessions."
        )
    group = SandboxGroupBinding.create(
        runtime.sandbox_group_resource_id,
        provider.group.region,
    )
    operation = await _within_setup_budget(
        state_binding.store.get_operation(
            session.owner_partition,
            session.session_id,
            fence.operation_id,
        ),
        setup_deadline,
    )
    phase = operation.record.phase
    if phase not in {
        "provision_create",
        "provision_lifecycle",
        "provision_content",
        "provision_manifest",
        "provision_journal",
        "provision_launching",
        "provision_rearm",
    }:
        raise SessionActivationError("Provision operation is no longer resumable.")
    labels = SandboxProvisioningLabels.create(
        owner_hash_version=session.owner_partition.owner_hash_version,
        owner_kind=session.owner_partition.owner_kind,
        owner_hash=session.owner_partition.owner_hash,
        app_hash=session.owner_partition.app_hash,
        session_id=session.session_id,
        operation_label=fence.correlation_label,
    )
    create_request = _build_create_request(
        runtime,
        source=source,
        labels=labels,
        setup_deadline=setup_deadline,
    )
    current = await _within_setup_budget(
        state_binding.store.get_session(session.owner_partition, session.session_id),
        setup_deadline,
    )
    if phase in {"provision_journal", "provision_launching", "provision_rearm"}:
        if current.record.sandbox_id is None:
            raise SessionActivationError("Provisioned session has no sandbox pointer.")
        expected = build_expected_manifest_binding(
            current.record,
            sandbox_group_resource_id=runtime.sandbox_group_resource_id,
            state_store_fingerprint=state_binding.state_store_fingerprint,
        )
        handle = await _within_setup_budget(
            provider.attach(
                PersistedSandboxBinding.create(
                    current.record.sandbox_id,
                    SandboxGroupBinding.create(
                        runtime.sandbox_group_resource_id,
                        current.record.region,
                    ),
                ),
                expected,
                readiness_timeout_seconds=_remaining_setup_seconds(setup_deadline),
            ),
            setup_deadline,
        )
        return ActivatedSession.create(
            handle=handle,
            session=current.record,
            etag=current.etag,
            partition=session.owner_partition,
            store=state_binding.store,
        )
    if phase == "provision_create":
        fence = await _within_setup_budget(
            state_binding.store.advance_operation(
                fence=fence,
                phase="provision_create",
                updated_at=datetime.now(UTC),
            ),
            setup_deadline,
        )
    try:
        handle = await _within_setup_budget(
            provider.create(create_request, persisted_group=group),
            setup_deadline,
        )
    except SandboxCapacityError:
        await _within_setup_budget(runtime.reap_for_capacity(), setup_deadline)
        handle = await _within_setup_budget(
            provider.create(create_request, persisted_group=group),
            setup_deadline,
        )
    try:
        try:
            return await _finish_created_provision(
                runtime,
                state_binding,
                session,
                fence,
                package,
                phase,
                current,
                handle,
                setup_deadline,
            )
        except SessionReadinessArtifactError as exc:
            latest = await _within_setup_budget(
                state_binding.store.get_session(session.owner_partition, session.session_id),
                setup_deadline,
            )
            await _quarantine_detected_binding(
                state_binding.store,
                latest.record,
                latest.etag,
                reason=exc.reason,
            )
            _record_security_event(exc.reason, frozenset({"harness_artifact"}))
            raise SessionActivationNotFoundError(
                "Session sandbox readiness artifacts cannot be trusted."
            ) from None
    except BaseException:
        try:
            await handle.close()
        except Exception:
            logger.exception("Could not close sandbox handle after provisioning failure")
        raise


async def _finish_created_provision(
    runtime: SessionRuntimeBinding,
    state_binding: StateStoreBinding,
    session: DurableSessionRecord,
    fence: SessionOperationFence,
    package: CapturedContentPackage,
    phase: DurableOperationPhase,
    current: SessionRead,
    handle: SandboxSessionHandle,
    setup_deadline: SetupDeadline,
) -> ActivatedSession:
    if phase == "provision_create":
        bound_session = _session_with_sandbox_for_operation(
            current.record,
            sandbox_id=handle.identity.sandbox_id,
            updated_at=datetime.now(UTC),
        )
        bound_target = SessionOperationTarget.create(
            session_id=session.session_id,
            sandbox_id=handle.identity.sandbox_id,
            generation=session.generation,
            digest_kind=session.digest_kind,
            digest=session.digest,
            run_id=session.active_run_id,
        )
        fence = await _within_setup_budget(
            state_binding.store.advance_operation(
                fence=fence,
                phase="provision_lifecycle",
                updated_at=bound_session.updated_at,
                updated_session=bound_session,
                updated_target=bound_target,
            ),
            setup_deadline,
        )
        phase = "provision_lifecycle"
        current = await _within_setup_budget(
            state_binding.store.get_session(session.owner_partition, session.session_id),
            setup_deadline,
        )
    else:
        bound_session = current.record
    if phase == "provision_lifecycle":
        await _within_setup_budget(
            handle.set_lifecycle_policy(lifecycle_policy_for_idle(runtime)),
            setup_deadline,
        )
        fence = await _within_setup_budget(
            state_binding.store.advance_operation(
                fence=fence,
                phase="provision_content",
                updated_at=datetime.now(UTC),
            ),
            setup_deadline,
        )
        phase = "provision_content"
    expected = build_expected_manifest_binding(
        bound_session,
        sandbox_group_resource_id=runtime.sandbox_group_resource_id,
        state_store_fingerprint=state_binding.state_store_fingerprint,
    )
    if phase == "provision_content":
        await _within_setup_budget(
            deliver_content_and_bootstrap(handle, package, expected, handle.identity),
            setup_deadline,
        )
        fence = await _within_setup_budget(
            state_binding.store.advance_operation(
                fence=fence,
                phase="provision_manifest",
                updated_at=datetime.now(UTC),
            ),
            setup_deadline,
        )
        phase = "provision_manifest"
    if phase == "provision_manifest":
        await _wait_for_created_manifest(
            handle,
            expected=expected,
            setup_deadline=setup_deadline,
        )
        await _within_setup_budget(
            _verify_optional_harness_artifacts(
                handle,
                bound_session,
                require_protocol=True,
            ),
            setup_deadline,
        )
        current_policy = await _within_setup_budget(
            handle.get_lifecycle_policy(),
            setup_deadline,
        )
        await _within_setup_budget(
            handle.set_lifecycle_policy(
                SandboxLifecyclePolicy.create(
                    auto_suspend_seconds=None,
                    auto_suspend_mode=current_policy.auto_suspend_mode,
                    auto_delete_seconds=current_policy.auto_delete_seconds,
                )
            ),
            setup_deadline,
        )
        running_session = _running_provisioned_session(
            bound_session,
            updated_at=datetime.now(UTC),
        )
        await _within_setup_budget(
            state_binding.store.advance_operation(
                fence=fence,
                phase="provision_journal",
                updated_at=running_session.updated_at,
                updated_session=running_session,
            ),
            setup_deadline,
        )
    current = await _within_setup_budget(
        state_binding.store.get_session(session.owner_partition, session.session_id),
        setup_deadline,
    )
    return ActivatedSession.create(
        handle=handle,
        session=current.record,
        etag=current.etag,
        partition=session.owner_partition,
        store=state_binding.store,
    )


def _session_with_sandbox_for_operation(
    session: DurableSessionRecord,
    *,
    sandbox_id: str,
    updated_at: datetime,
) -> DurableSessionRecord:
    return DurableSessionRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        sandbox_id=sandbox_id,
        generation=session.generation,
        digest_kind=session.digest_kind,
        digest=session.digest,
        protocol=session.protocol,
        status="creating",
        last_activity_at=session.last_activity_at,
        expires_at=session.expires_at,
        idle_policy_armed=False,
        active_run_id=session.active_run_id,
        snapshot_ids=session.snapshot_ids,
        region=session.region,
        state_store_fingerprint=session.state_store_fingerprint,
        quarantine_reason=None,
        tombstone_reason=None,
        created_at=session.created_at,
        updated_at=updated_at,
        active_operation_id=session.active_operation_id,
        operation_sequence=session.operation_sequence,
    )


def _running_provisioned_session(
    session: DurableSessionRecord,
    *,
    updated_at: datetime,
) -> DurableSessionRecord:
    return DurableSessionRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        sandbox_id=session.sandbox_id,
        generation=session.generation,
        digest_kind=session.digest_kind,
        digest=session.digest,
        protocol=session.protocol,
        status="running",
        last_activity_at=updated_at,
        expires_at=session.expires_at,
        idle_policy_armed=False,
        active_run_id=session.active_run_id,
        snapshot_ids=session.snapshot_ids,
        region=session.region,
        state_store_fingerprint=session.state_store_fingerprint,
        quarantine_reason=None,
        tombstone_reason=None,
        created_at=session.created_at,
        updated_at=updated_at,
        active_operation_id=session.active_operation_id,
        operation_sequence=session.operation_sequence,
    )


async def _create_and_activate_session(
    runtime: SessionRuntimeBinding,
    owner: OwnerContext,
    partition: OwnerPartition,
    session_id: str,
    state_binding: StateStoreBinding,
    setup_deadline: SetupDeadline,
) -> ActivatedSession:
    if runtime.creation_source is None and runtime.create_profile is None:
        raise SessionCreationUnavailableError(
            "No runtime bootstrap source is available for new sandbox sessions."
        )

    package = await _capture_current_package(runtime.script_root, setup_deadline)
    provider = await _within_setup_budget(runtime.get_provider(), setup_deadline)
    now = datetime.now(UTC)
    initial_session = DurableSessionRecord.create(
        owner_partition=partition,
        session_id=session_id,
        sandbox_id=None,
        generation=1,
        digest_kind=package.digest_kind,
        digest=package.digest,
        protocol=runtime.protocol_version,
        status="creating",
        last_activity_at=now,
        expires_at=now + timedelta(seconds=runtime.reclaim_idle_seconds),
        idle_policy_armed=True,
        active_run_id=None,
        snapshot_ids=(),
        region=provider.group.region,
        state_store_fingerprint=state_binding.state_store_fingerprint,
        quarantine_reason=None,
        tombstone_reason=None,
        created_at=now,
        updated_at=now,
        active_operation_id=None,
        operation_sequence=0,
    )
    etag = await _within_setup_budget(
        state_binding.store.create_session(initial_session),
        setup_deadline,
    )
    group = SandboxGroupBinding.create(
        runtime.sandbox_group_resource_id,
        provider.group.region,
    )
    labels = SandboxProvisioningLabels.create(
        owner_hash_version=partition.owner_hash_version,
        owner_kind=partition.owner_kind,
        owner_hash=partition.owner_hash,
        app_hash=partition.app_hash,
        session_id=session_id,
    )
    create_request = _build_create_request(
        runtime,
        source=runtime.creation_source,
        labels=labels,
        setup_deadline=setup_deadline,
    )
    handle: SandboxSessionHandle | None = None
    persisted_session = initial_session
    succeeded = False
    try:
        try:
            handle = await _within_setup_budget(
                provider.create(create_request, persisted_group=group),
                setup_deadline,
            )
        except SandboxCapacityError:
            await _within_setup_budget(runtime.reap_for_capacity(), setup_deadline)
            handle = await _within_setup_budget(
                provider.create(create_request, persisted_group=group),
                setup_deadline,
            )
        await _within_setup_budget(
            handle.set_lifecycle_policy(lifecycle_policy_for_idle(runtime)),
            setup_deadline,
        )
        persisted_session = _session_with_sandbox(
            initial_session,
            sandbox_id=handle.identity.sandbox_id,
            updated_at=datetime.now(UTC),
        )
        etag = await _within_setup_budget(
            state_binding.store.update_session(
                previous=initial_session,
                updated=persisted_session,
                etag=etag,
            ),
            setup_deadline,
        )
        expected = build_expected_manifest_binding(
            persisted_session,
            sandbox_group_resource_id=runtime.sandbox_group_resource_id,
            state_store_fingerprint=state_binding.state_store_fingerprint,
        )
        await _within_setup_budget(
            deliver_content_and_bootstrap(handle, package, expected, handle.identity),
            setup_deadline,
        )
        await _wait_for_created_manifest(
            handle,
            expected=expected,
            setup_deadline=setup_deadline,
        )
        await _within_setup_budget(
            _verify_optional_harness_artifacts(
                handle,
                persisted_session,
                require_protocol=True,
            ),
            setup_deadline,
        )
        ready_session = _ready_session(persisted_session, updated_at=datetime.now(UTC))
        etag = await _within_setup_budget(
            state_binding.store.update_session(
                previous=persisted_session,
                updated=ready_session,
                etag=etag,
            ),
            setup_deadline,
        )
        await _within_setup_budget(runtime.reconcile_after_create(), setup_deadline)
        activated = ActivatedSession.create(
            handle=handle,
            session=ready_session,
            etag=etag,
            partition=partition,
            store=state_binding.store,
        )
        succeeded = True
        return activated
    except (SandboxManifestMismatchError, ContentBindingMismatchError):
        if handle is not None:
            await _quarantine_detected_binding(
                state_binding.store,
                persisted_session,
                etag,
                reason="sandbox_manifest_mismatch",
            )
        _record_security_event("sandbox_manifest_mismatch", frozenset({"manifest"}))
        raise SessionActivationNotFoundError(
            "Session sandbox binding cannot be trusted."
        ) from None
    except SessionReadinessArtifactError as exc:
        if handle is not None:
            await _quarantine_detected_binding(
                state_binding.store,
                persisted_session,
                etag,
                reason=exc.reason,
            )
        _record_security_event(exc.reason, frozenset({"harness_artifact"}))
        raise SessionActivationNotFoundError(
            "Session sandbox readiness artifacts cannot be trusted."
        ) from None
    except ContentPackagingError:
        raise SessionActivationError("Sandbox content delivery could not be verified.") from None
    except TimeoutError:
        raise SessionActivationSetupTimeoutError(
            "Sandbox setup did not complete before the setup deadline."
        ) from None
    finally:
        if handle is not None and not succeeded:
            await handle.close()


async def _wait_for_created_manifest(
    handle: SandboxSessionHandle,
    *,
    expected: ExpectedSandboxManifestBinding,
    setup_deadline: SetupDeadline,
) -> None:
    while True:
        try:
            await _within_setup_budget(
                read_live_manifest_binding(handle, expected, handle.identity),
                setup_deadline,
            )
            return
        except LiveManifestNotReadyError:
            report = await _read_bootstrap_error_report(handle, setup_deadline)
            if report is not None and report.permanent:
                raise SessionReadinessArtifactError("bootstrap_failure") from None
            delay = min(
                _MANIFEST_RETRY_INTERVAL_SECONDS,
                _remaining_setup_seconds(setup_deadline),
            )
            await _within_setup_budget(asyncio.sleep(delay), setup_deadline)


async def _read_bootstrap_error_report(
    handle: SandboxSessionHandle,
    setup_deadline: SetupDeadline,
) -> BootstrapErrorReport | None:
    try:
        payload = await _within_setup_budget(
            handle.read_file(BOOTSTRAP_ERROR_PATH),
            setup_deadline,
        )
    except SandboxFileNotFoundError:
        return None
    try:
        return parse_bootstrap_error_report(payload)
    except BootstrapReportError:
        raise SessionReadinessArtifactError("bootstrap_failure") from None


async def _within_setup_budget[T](
    operation: Awaitable[T],
    setup_deadline: SetupDeadline,
) -> T:
    try:
        async with asyncio.timeout(_remaining_setup_seconds(setup_deadline)):
            return await operation
    except TimeoutError:
        raise SessionActivationSetupTimeoutError(
            "Sandbox setup did not complete before the setup deadline."
        ) from None


async def _capture_current_package(
    script_root: Path,
    setup_deadline: SetupDeadline,
) -> CapturedContentPackage:
    # Capture is process-cached and single-flight upstream, so only the first
    # session on this worker pays the archive cost; every call still runs under
    # the shared setup deadline because a cold capture can block on file I/O.
    try:
        return await _within_setup_budget(get_content_package(script_root), setup_deadline)
    except ContentPackagingError:
        raise SessionActivationError("Sandbox content package could not be captured.") from None


def _build_create_request(
    runtime: SessionRuntimeBinding,
    *,
    source: SandboxCreateSource | None,
    labels: SandboxProvisioningLabels,
    setup_deadline: SetupDeadline,
) -> SandboxCreateRequest:
    if runtime.create_profile is not None:
        return runtime.create_profile.build_request(
            labels=labels,
            remaining_setup_budget_seconds=_remaining_setup_seconds(setup_deadline),
            auto_suspend_seconds=runtime.auto_suspend_seconds,
        )
    if source is None:
        raise SessionCreationUnavailableError(
            "No runtime bootstrap source is available for new sandbox sessions."
        )
    return SandboxCreateRequest.create(
        source=source,
        labels=labels,
        remaining_setup_budget_seconds=_remaining_setup_seconds(setup_deadline),
        auto_suspend_seconds=runtime.auto_suspend_seconds,
        auto_suspend_mode="Disk",
    )


async def _verify_optional_harness_artifacts(
    handle: SandboxSessionHandle,
    session: DurableSessionRecord,
    *,
    require_protocol: bool,
) -> None:
    """Validate mandatory protocol capabilities and an optional checkpoint pointer."""
    try:
        protocol_payload = await handle.read_file(HARNESS_PROTOCOL_PATH)
    except SandboxFileNotFoundError:
        if require_protocol:
            raise SessionReadinessArtifactError("protocol_version_mismatch") from None
    else:
        try:
            protocol = _HarnessProtocolArtifact.model_validate(
                decode_json_object(protocol_payload),
                strict=True,
            )
        except (
            DuplicateJsonKeyError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            ValidationError,
        ) as exc:
            raise SessionReadinessArtifactError("protocol_version_mismatch") from exc
        if protocol.protocol_version != session.protocol:
            raise SessionReadinessArtifactError("protocol_version_mismatch")
        if protocol.capabilities != dict(REQUIRED_HARNESS_CAPABILITIES):
            raise SessionReadinessArtifactError("capability_mismatch")

    pointer_payload = await _read_optional_file(handle, ATOMIC_CHECKPOINT_POINTER_PATH)
    if pointer_payload is not None:
        try:
            pointer = pointer_payload.decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise SessionReadinessArtifactError("checkpoint_corrupt") from exc
        try:
            validate_checkpoint_name(pointer)
        except ValueError:
            raise SessionReadinessArtifactError("checkpoint_corrupt") from None


async def _read_optional_file(
    handle: SandboxSessionHandle,
    path: str,
) -> bytes | None:
    try:
        return await handle.read_file(path)
    except SandboxFileNotFoundError:
        return None


def _remaining_setup_seconds(setup_deadline: SetupDeadline) -> float:
    try:
        return setup_deadline.remaining_setup_seconds()
    except TimeoutError:
        raise SessionActivationSetupTimeoutError(
            "Sandbox setup did not complete before the setup deadline."
        ) from None


def _verify_owner_binding(owner: OwnerContext, session: DurableSessionRecord) -> None:
    app_hash_version = session.owner_partition.app_hash.partition("-")[0]
    if (
        verify_owner_hash(
            owner,
            session.owner_partition.owner_hash,
            session.owner_partition.owner_hash_version,
        )
        and verify_app_hash(owner.app_identity, session.owner_partition.app_hash, app_hash_version)
    ):
        return
    _record_security_event("owner_binding_mismatch", frozenset({"owner_hash", "app_hash"}))
    raise SessionActivationNotFoundError("Session was not found for this owner.")


def _verify_state_store_binding(session: DurableSessionRecord, fingerprint: str) -> None:
    if session.state_store_fingerprint == fingerprint:
        return
    _record_security_event(
        "state_store_fingerprint_mismatch",
        frozenset({"state_store_fingerprint"}),
    )
    raise SessionActivationError("Session state store binding no longer matches this controller.")


async def _drain_changed_epoch(
    store: SessionStateStore,
    session: DurableSessionRecord,
    etag: str,
) -> None:
    if session.active_run_id is not None:
        return
    await store.tombstone_session(
        previous=session,
        etag=etag,
        tombstone_reason="deployment_epoch_changed",
        updated_at=datetime.now(UTC),
    )


def _session_with_sandbox(
    session: DurableSessionRecord,
    *,
    sandbox_id: str,
    updated_at: datetime,
) -> DurableSessionRecord:
    return DurableSessionRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        sandbox_id=sandbox_id,
        generation=session.generation,
        digest_kind=session.digest_kind,
        digest=session.digest,
        protocol=session.protocol,
        status="creating",
        last_activity_at=session.last_activity_at,
        expires_at=session.expires_at,
        idle_policy_armed=session.idle_policy_armed,
        active_run_id=None,
        snapshot_ids=session.snapshot_ids,
        region=session.region,
        state_store_fingerprint=session.state_store_fingerprint,
        quarantine_reason=None,
        tombstone_reason=None,
        created_at=session.created_at,
        updated_at=updated_at,
        active_operation_id=session.active_operation_id,
        operation_sequence=session.operation_sequence,
    )


def _ready_session(session: DurableSessionRecord, *, updated_at: datetime) -> DurableSessionRecord:
    return DurableSessionRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        sandbox_id=session.sandbox_id,
        generation=session.generation,
        digest_kind=session.digest_kind,
        digest=session.digest,
        protocol=session.protocol,
        status="ready",
        last_activity_at=updated_at,
        expires_at=session.expires_at,
        idle_policy_armed=True,
        active_run_id=None,
        snapshot_ids=session.snapshot_ids,
        region=session.region,
        state_store_fingerprint=session.state_store_fingerprint,
        quarantine_reason=None,
        tombstone_reason=None,
        created_at=session.created_at,
        updated_at=updated_at,
        active_operation_id=session.active_operation_id,
        operation_sequence=session.operation_sequence,
    )


def _quarantined_session(
    session: DurableSessionRecord,
    *,
    reason: str,
    updated_at: datetime,
) -> DurableSessionRecord:
    reason = _validate_quarantine_reason(reason)
    return DurableSessionRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        sandbox_id=session.sandbox_id,
        generation=session.generation,
        digest_kind=session.digest_kind,
        digest=session.digest,
        protocol=session.protocol,
        status="quarantined",
        last_activity_at=session.last_activity_at,
        expires_at=session.expires_at,
        idle_policy_armed=session.idle_policy_armed,
        active_run_id=None,
        snapshot_ids=session.snapshot_ids,
        region=session.region,
        state_store_fingerprint=session.state_store_fingerprint,
        quarantine_reason=reason,
        tombstone_reason=session.tombstone_reason,
        created_at=session.created_at,
        updated_at=updated_at,
        active_operation_id=session.active_operation_id,
        operation_sequence=session.operation_sequence,
    )


async def _quarantine_session(
    store: SessionStateStore,
    session: DurableSessionRecord,
    etag: str,
    *,
    reason: str,
) -> None:
    quarantined = _quarantined_session(session, reason=reason, updated_at=datetime.now(UTC))
    await store.update_session(previous=session, updated=quarantined, etag=etag)


async def _quarantine_detected_binding(
    store: SessionStateStore,
    session: DurableSessionRecord,
    etag: str,
    *,
    reason: str,
) -> None:
    reason = _validate_quarantine_reason(reason)
    if session.active_run_id is None:
        await _quarantine_session(store, session, etag, reason=reason)
        return
    active_run = await store.get_run(
        session.owner_partition,
        session.session_id,
        session.active_run_id,
    )
    failed = _terminal_run(active_run.record, reason=reason)
    await store.adopt_terminal_run(failed)
    released = await store.get_session(session.owner_partition, session.session_id)
    await _quarantine_session(store, released.record, released.etag, reason=reason)


def _terminal_run(
    run: DurableRunRecord,
    *,
    reason: str | None,
) -> DurableRunRecord:
    return terminal_run(
        run,
        status="failed",
        result_available=False,
        reason=reason,
        updated_at=datetime.now(UTC),
    )


def _routing_differences(
    expected: DurableSessionRecord,
    observed: DurableSessionRecord,
) -> frozenset[str]:
    fields: set[str] = set()
    if expected.sandbox_id != observed.sandbox_id:
        fields.add("sandbox_id")
    if expected.generation != observed.generation:
        fields.add("generation")
    if _digest_pair(expected) != _digest_pair(observed):
        fields.update({"digest_kind", "digest"})
    if expected.region != observed.region:
        fields.add("region")
    if expected.state_store_fingerprint != observed.state_store_fingerprint:
        fields.add("state_store_fingerprint")
    return frozenset(fields)


def _digest_pair(
    value: DurableSessionRecord | CapturedContentPackage,
) -> tuple[str, str]:
    return value.digest_kind, value.digest


def _validate_quarantine_reason(reason: str) -> str:
    if reason not in QUARANTINE_REASONS:
        raise ValueError(f"Unsupported sandbox quarantine reason: {reason}")
    return reason


def _record_security_event(reason: str, fields: frozenset[str]) -> None:
    logger.warning("Sandbox session routing rejected: reason=%s fields=%s", reason, sorted(fields))
    current_span().add_event(
        "af.session.routing_rejected",
        {
            "af.session.reason": reason,
            "af.session.fields": ",".join(sorted(fields)),
        },
    )
