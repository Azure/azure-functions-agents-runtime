"""Activation and routing checks for one persistent sandbox session."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from .._logger import logger
from .._observability import current_span
from ..session_state import (
    AppIdentity,
    DurableRunRecord,
    DurableRunStatus,
    DurableSessionRecord,
    OwnerContext,
    OwnerPartition,
    SessionStateStore,
    owner_partition,
    verify_app_hash,
    verify_owner_hash,
)
from ..session_state.errors import SessionRowNotFoundError
from ..transport.manifest import ExpectedSandboxManifestBinding, SandboxManifestMismatchError
from ..transport.ports import SandboxSessionHandle, SandboxSessionProvider
from ..transport.transport_models import (
    PersistedSandboxBinding,
    SandboxCreateRequest,
    SandboxCreateSource,
    SandboxGroupBinding,
    SandboxProvisioningLabels,
)
from .package import (
    CapturedContentPackage,
    ContentBindingMismatchError,
    ContentPackagingError,
    LiveManifestNotReadyError,
    build_expected_manifest_binding,
    deliver_content_package,
    get_content_package,
    read_live_manifest_binding,
)

DEFAULT_AUTO_SUSPEND_SECONDS = 300
DEFAULT_RECLAIM_IDLE_SECONDS = 86_400
DEFAULT_PROTOCOL_VERSION = "1"
_MANIFEST_RETRY_INTERVAL_SECONDS = 0.25

type _SessionLockKey = tuple[str, str]


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
    auto_suspend_seconds: int
    reclaim_idle_seconds: int
    protocol_version: str
    _provider: _AsyncSingleton[SandboxSessionProvider] = field(repr=False, compare=False)
    _state_store: _AsyncSingleton[StateStoreBinding] = field(repr=False, compare=False)
    _session_locks: _SessionLockRegistry = field(repr=False, compare=False)

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
        auto_suspend_seconds: int = DEFAULT_AUTO_SUSPEND_SECONDS,
        reclaim_idle_seconds: int = DEFAULT_RECLAIM_IDLE_SECONDS,
        protocol_version: str = DEFAULT_PROTOCOL_VERSION,
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
            auto_suspend_seconds=auto_suspend_seconds,
            reclaim_idle_seconds=reclaim_idle_seconds,
            protocol_version=protocol_version,
            _provider=_AsyncSingleton(provider_factory),
            _state_store=_AsyncSingleton(state_store_factory),
            _session_locks=_SessionLockRegistry(),
        )

    async def get_provider(self) -> SandboxSessionProvider:
        """Return the one lazily opened provider for this app's Sandbox Group."""
        return await self._provider.get()

    async def get_state_store(self) -> StateStoreBinding:
        """Return the one lazily resolved state-store binding for this app."""
        return await self._state_store.get()

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
        idle_policy_armed=session.idle_policy_armed,
        active_run_id=run_id,
        snapshot_ids=session.snapshot_ids,
        region=session.region,
        state_store_fingerprint=session.state_store_fingerprint,
        quarantine_reason=session.quarantine_reason,
        tombstone_reason=session.tombstone_reason,
        created_at=session.created_at,
        updated_at=updated_at,
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
    )


async def _create_and_activate_session(
    runtime: SessionRuntimeBinding,
    owner: OwnerContext,
    partition: OwnerPartition,
    session_id: str,
    state_binding: StateStoreBinding,
    setup_deadline: SetupDeadline,
) -> ActivatedSession:
    if runtime.creation_source is None:
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
    )
    etag = await _within_setup_budget(
        state_binding.store.create_session(initial_session),
        setup_deadline,
    )
    group = SandboxGroupBinding.create(
        runtime.sandbox_group_resource_id,
        provider.group.region,
    )
    create_request = SandboxCreateRequest.create(
        source=runtime.creation_source,
        labels=SandboxProvisioningLabels.create(
            owner_hash_version=partition.owner_hash_version,
            owner_hash=partition.owner_hash,
            app_hash=partition.app_hash,
            session_id=session_id,
        ),
        remaining_setup_budget_seconds=_remaining_setup_seconds(setup_deadline),
        auto_suspend_seconds=runtime.auto_suspend_seconds,
        auto_suspend_mode="Disk",
    )
    handle: SandboxSessionHandle | None = None
    persisted_session = initial_session
    succeeded = False
    try:
        handle = await _within_setup_budget(
            provider.create(create_request, persisted_group=group),
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
            deliver_content_package(handle, package, expected, handle.identity),
            setup_deadline,
        )
        await _wait_for_created_manifest(
            handle,
            expected=expected,
            setup_deadline=setup_deadline,
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
            delay = min(
                _MANIFEST_RETRY_INTERVAL_SECONDS,
                _remaining_setup_seconds(setup_deadline),
            )
            await _within_setup_budget(asyncio.sleep(delay), setup_deadline)


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
    )


def _quarantined_session(
    session: DurableSessionRecord,
    *,
    reason: str,
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


def _record_security_event(reason: str, fields: frozenset[str]) -> None:
    logger.warning("Sandbox session routing rejected: reason=%s fields=%s", reason, sorted(fields))
    current_span().add_event(
        "af.session.routing_rejected",
        {
            "af.session.reason": reason,
            "af.session.fields": ",".join(sorted(fields)),
        },
    )
