from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from azure_functions_agents.controller.package import (
    CONTENT_MANIFEST_SEED_PATH,
    capture_script_root,
)
from azure_functions_agents.controller.readiness import (
    ActivatedSession,
    SessionActivationError,
    SessionActivationGoneError,
    SessionActivationNotFoundError,
    SessionActivationSetupTimeoutError,
    SessionBindingChangedError,
    SessionRuntimeBinding,
    StateStoreBinding,
    activate_session,
    revalidate_before_submit,
    session_with_admitted_run,
)
from azure_functions_agents.execution.setup_budget import SetupBudget
from azure_functions_agents.session_state import (
    AdoptionOutcome,
    AppIdentity,
    DurableRunRecord,
    DurableSessionRecord,
    FunctionAppOwnerContext,
    OwnerPartition,
    SessionRead,
    StateStoreUnavailableError,
    owner_partition,
)
from azure_functions_agents.transport.manifest import (
    SESSION_MANIFEST_PATH,
    SandboxManifestMismatchError,
)
from azure_functions_agents.transport.transport_models import (
    DiskSource,
    ProvisionedSandboxIdentity,
    SandboxCreateRequest,
    SandboxGroupBinding,
    SandboxGroupIdentity,
)
from tests.doubles.fake_sandbox_transport import FakeSandboxTransport

_GROUP_RESOURCE_ID = (
    "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.App/sandboxGroups/group"
)
_FINGERPRINT = "s1-" + ("a" * 52)
_TEST_SOURCE = DiskSource.create("test-harness")


class _FakeHandle(FakeSandboxTransport):
    def __init__(self, sandbox_id: str = "sandbox-1") -> None:
        super().__init__()
        self.identity = ProvisionedSandboxIdentity.create(
            sandbox_id=sandbox_id,
            group_resource_id=_GROUP_RESOURCE_ID,
            region="westus2",
        )
        self.closed = False
        self.stop_calls = 0
        self.resume_calls = 0
        self.delete_calls = 0

    async def write_file(self, path: str, content: bytes, *, create_dirs: bool = False) -> None:
        await super().write_file(path, content, create_dirs=create_dirs)
        if path == CONTENT_MANIFEST_SEED_PATH:
            self.seed_file(SESSION_MANIFEST_PATH, content)

    async def stop(self) -> None:
        self.stop_calls += 1

    async def resume(self) -> None:
        self.resume_calls += 1

    async def delete(self) -> None:
        self.delete_calls += 1

    async def close(self) -> None:
        self.closed = True


class _FakeProvider:
    def __init__(self, handle: _FakeHandle) -> None:
        self.group = SandboxGroupIdentity(
            resource_id=_GROUP_RESOURCE_ID,
            subscription_id="sub",
            resource_group="rg",
            group_name="group",
            region="westus2",
        )
        self.handle = handle
        self.attach_calls = 0
        self.resume_calls = 0
        self.create_calls: list[SandboxCreateRequest] = []
        self.attach_error: Exception | None = None
        self.attach_delay = 0.0
        self.closed = False

    async def create(
        self,
        request: SandboxCreateRequest,
        *,
        persisted_group: SandboxGroupBinding,
    ) -> _FakeHandle:
        assert persisted_group.region == self.group.region
        self.create_calls.append(request)
        return self.handle

    async def attach(self, *args: object, **kwargs: object) -> _FakeHandle:
        del args, kwargs
        self.attach_calls += 1
        if self.attach_delay:
            await asyncio.sleep(self.attach_delay)
        if self.attach_error is not None:
            raise self.attach_error
        return self.handle

    async def resume(self, *args: object, **kwargs: object) -> _FakeHandle:
        del args, kwargs
        self.resume_calls += 1
        if self.attach_error is not None:
            raise self.attach_error
        return self.handle

    async def close(self) -> None:
        self.closed = True


class _FakeStore:
    def __init__(self, session: DurableSessionRecord | None = None) -> None:
        self.session = session
        self.etag = "etag-1"
        self.adopted: list[DurableRunRecord] = []
        self.operations: list[str] = []

    async def create_session(self, record: DurableSessionRecord) -> str:
        if self.session is not None:
            raise AssertionError("session row already exists")
        self.session = record
        self.etag = "etag-2"
        self.operations.append("create")
        return self.etag

    async def get_session(
        self, partition: OwnerPartition, session_id: str
    ) -> SessionRead:
        if self.session is None or self.session.session_id != session_id:
            from azure_functions_agents.session_state import SessionRowNotFoundError

            raise SessionRowNotFoundError("missing")
        del partition
        return SessionRead(record=self.session, etag=self.etag)

    async def update_session(
        self,
        *,
        previous: DurableSessionRecord,
        updated: DurableSessionRecord,
        etag: str,
        backing_rebind: bool = False,
    ) -> str:
        del backing_rebind
        assert self.session == previous
        assert self.etag == etag
        self.session = updated
        self.etag = f"etag-{len(self.operations) + 3}"
        self.operations.append(f"update:{updated.status}")
        return self.etag

    async def tombstone_session(
        self,
        *,
        previous: DurableSessionRecord,
        etag: str,
        tombstone_reason: str,
        updated_at: datetime,
    ) -> str:
        assert self.session == previous
        assert self.etag == etag
        self.session = replace(
            previous,
            status="tombstoned",
            active_run_id=None,
            tombstone_reason=tombstone_reason,
            updated_at=updated_at,
        )
        self.operations.append("tombstone")
        self.etag = "etag-tombstone"
        return self.etag

    async def adopt_terminal_run(self, terminal_run: DurableRunRecord) -> AdoptionOutcome:
        assert self.session is not None
        self.adopted.append(terminal_run)
        self.operations.append("adopt")
        self.session = replace(
            self.session,
            status="ready",
            active_run_id=None,
            updated_at=terminal_run.updated_at,
        )
        self.etag = "etag-released"
        return AdoptionOutcome(run=terminal_run, run_etag="run-etag", slot_released=True)


def _owner() -> FunctionAppOwnerContext:
    app_identity = AppIdentity.create(
        subscription_id="11111111-2222-3333-4444-555555555555",
        site_name="agent-app",
    )
    return FunctionAppOwnerContext.create(app_identity, "main")


def _session(
    script_root: Path,
    *,
    owner: FunctionAppOwnerContext | None = None,
    status: str = "ready",
    sandbox_id: str | None = "sandbox-1",
    fingerprint: str = _FINGERPRINT,
) -> DurableSessionRecord:
    resolved_owner = owner or _owner()
    package = capture_script_root(script_root)
    now = datetime.now(UTC)
    return DurableSessionRecord.create(
        owner_partition=owner_partition(resolved_owner),
        session_id="session-1",
        sandbox_id=sandbox_id,
        generation=1,
        digest_kind=package.digest_kind,
        digest=package.digest,
        protocol="1",
        status=status,  # type: ignore[arg-type]
        last_activity_at=now,
        expires_at=now + timedelta(hours=24),
        idle_policy_armed=True,
        active_run_id=None,
        snapshot_ids=(),
        region="westus2",
        state_store_fingerprint=fingerprint,
        quarantine_reason=None,
        tombstone_reason=None,
        created_at=now,
        updated_at=now,
    )


def _run(session: DurableSessionRecord) -> DurableRunRecord:
    now = datetime.now(UTC)
    return DurableRunRecord.create(
        owner_partition=session.owner_partition,
        session_id=session.session_id,
        run_id="run-1",
        generation=session.generation,
        status="accepted",
        result_available=False,
        status_reason=None,
        expires_at=now + timedelta(minutes=15),
        created_at=now,
        updated_at=now,
    )


def _runtime(
    script_root: Path,
    provider: _FakeProvider,
    store: _FakeStore,
    *,
    source: DiskSource | None = _TEST_SOURCE,
    fingerprint: str = _FINGERPRINT,
) -> SessionRuntimeBinding:
    async def provider_factory() -> _FakeProvider:
        return provider

    async def state_store_factory() -> StateStoreBinding:
        return StateStoreBinding.create(
            store=store,
            state_store_fingerprint=fingerprint,
        )

    return SessionRuntimeBinding.create(
        app_identity=_owner().app_identity,
        sandbox_group_resource_id=_GROUP_RESOURCE_ID,
        script_root=script_root,
        provider_factory=provider_factory,
        state_store_factory=state_store_factory,
        creation_source=source,
    )


def _script_root(tmp_path: Path) -> Path:
    (tmp_path / "function_app.py").write_text("app = object()\n", encoding="utf-8")
    return tmp_path


@pytest.mark.asyncio
async def test_attach_uses_the_provider_handshake_without_a_second_manifest_read(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    handle = _FakeHandle()
    provider = _FakeProvider(handle)
    session = _session(script_root)
    store = _FakeStore(session)

    activated = await activate_session(
        _runtime(script_root, provider, store),
        _owner(),
        session.session_id,
        SetupBudget.start(),
        allow_create=False,
    )

    assert provider.attach_calls == 1
    assert provider.resume_calls == 0
    assert [call.operation for call in handle.calls] == []
    await activated.handle.close()


@pytest.mark.asyncio
async def test_resume_uses_the_provider_handshake_without_a_second_manifest_read(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    handle = _FakeHandle()
    provider = _FakeProvider(handle)
    session = _session(script_root, status="suspended")
    store = _FakeStore(session)

    activated = await activate_session(
        _runtime(script_root, provider, store),
        _owner(),
        session.session_id,
        SetupBudget.start(),
        allow_create=False,
    )

    assert provider.attach_calls == 0
    assert provider.resume_calls == 1
    assert [call.operation for call in handle.calls] == []
    await activated.handle.close()


@pytest.mark.asyncio
async def test_manifest_mismatch_quarantines_without_deleting_state(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    provider = _FakeProvider(_FakeHandle())
    provider.attach_error = SandboxManifestMismatchError(frozenset({"sandbox_id"}))
    session = _session(script_root)
    store = _FakeStore(session)

    with pytest.raises(SessionActivationNotFoundError):
        await activate_session(
            _runtime(script_root, provider, store),
            _owner(),
            session.session_id,
            SetupBudget.start(),
            allow_create=False,
        )

    assert store.session is not None
    assert store.session.status == "quarantined"
    assert store.operations == ["update:quarantined"]


@pytest.mark.asyncio
async def test_owner_or_app_hash_mismatch_fails_without_attaching(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    provider = _FakeProvider(_FakeHandle())
    other_app = AppIdentity.create(
        subscription_id="99999999-2222-3333-4444-555555555555",
        site_name="other-app",
    )
    mismatched_owner = FunctionAppOwnerContext.create(other_app, "main")
    session = _session(script_root, owner=mismatched_owner)
    store = _FakeStore(session)

    with pytest.raises(SessionActivationNotFoundError, match="not found"):
        await activate_session(
            _runtime(script_root, provider, store),
            _owner(),
            session.session_id,
            SetupBudget.start(),
            allow_create=False,
        )

    assert provider.attach_calls == 0


@pytest.mark.asyncio
async def test_state_store_fingerprint_mismatch_fails_without_attaching(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    provider = _FakeProvider(_FakeHandle())
    session = _session(script_root, fingerprint="s1-" + ("b" * 52))
    store = _FakeStore(session)

    with pytest.raises(SessionActivationError, match="state store binding"):
        await activate_session(
            _runtime(script_root, provider, store),
            _owner(),
            session.session_id,
            SetupBudget.start(),
            allow_create=False,
        )

    assert provider.attach_calls == 0


@pytest.mark.asyncio
async def test_deployment_epoch_mismatch_tombstones_an_idle_session(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    provider = _FakeProvider(_FakeHandle())
    session = replace(_session(script_root), digest="sha256:" + ("b" * 64))
    store = _FakeStore(session)

    with pytest.raises(SessionActivationGoneError):
        await activate_session(
            _runtime(script_root, provider, store),
            _owner(),
            session.session_id,
            SetupBudget.start(),
            allow_create=False,
        )

    assert store.session is not None
    assert store.session.status == "tombstoned"
    assert store.operations == ["tombstone"]


@pytest.mark.asyncio
async def test_missing_explicit_session_fails_closed_without_creating_a_sandbox(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    provider = _FakeProvider(_FakeHandle())
    store = _FakeStore()

    with pytest.raises(SessionActivationNotFoundError):
        await activate_session(
            _runtime(script_root, provider, store),
            _owner(),
            "client-supplied",
            SetupBudget.start(),
            allow_create=False,
        )

    assert provider.create_calls == []
    assert store.session is None


@pytest.mark.asyncio
async def test_creation_reserves_the_row_then_proves_the_live_manifest(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    handle = _FakeHandle("new-sandbox")
    provider = _FakeProvider(handle)
    store = _FakeStore()

    activated = await activate_session(
        _runtime(script_root, provider, store),
        _owner(),
        "new-session",
        SetupBudget.start(),
        allow_create=True,
    )

    assert provider.create_calls
    assert provider.create_calls[0].remaining_setup_budget_seconds <= 30.0
    assert store.session is not None
    assert store.session.status == "ready"
    assert store.session.sandbox_id == "new-sandbox"
    assert not handle.closed
    await activated.handle.close()


@pytest.mark.asyncio
async def test_setup_timeout_occurs_before_run_launch(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    provider = _FakeProvider(_FakeHandle())
    provider.attach_delay = 0.1
    session = _session(script_root)
    store = _FakeStore(session)

    with pytest.raises(SessionActivationSetupTimeoutError):
        await activate_session(
            _runtime(script_root, provider, store),
            _owner(),
            session.session_id,
            SetupBudget.start(setup_seconds=0.05),
            allow_create=False,
        )


@pytest.mark.asyncio
async def test_state_store_unavailability_fails_closed(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    provider = _FakeProvider(_FakeHandle())

    async def unavailable_store() -> StateStoreBinding:
        raise StateStoreUnavailableError("unavailable")

    runtime = SessionRuntimeBinding.create(
        app_identity=_owner().app_identity,
        sandbox_group_resource_id=_GROUP_RESOURCE_ID,
        script_root=script_root,
        provider_factory=lambda: _provider_factory(provider),
        state_store_factory=unavailable_store,
    )

    with pytest.raises(StateStoreUnavailableError):
        await activate_session(
            runtime,
            _owner(),
            "session-1",
            SetupBudget.start(),
            allow_create=False,
        )


async def _provider_factory(provider: _FakeProvider) -> _FakeProvider:
    return provider


@pytest.mark.asyncio
async def test_pre_submit_change_releases_slot_before_quarantine(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    original = _session(script_root)
    changed = replace(original, sandbox_id="repointed-sandbox")
    admitted_session = session_with_admitted_run(
        changed,
        "run-1",
        updated_at=datetime.now(UTC),
    )
    store = _FakeStore(admitted_session)
    activated = ActivatedSession.create(
        handle=_FakeHandle(),
        session=original,
        etag="etag-1",
        partition=original.owner_partition,
        store=store,
    )

    with pytest.raises(SessionBindingChangedError) as raised:
        await revalidate_before_submit(activated, _run(original))

    assert raised.value.fields == frozenset({"sandbox_id"})
    assert [operation for operation in store.operations if operation in {"adopt", "update:quarantined"}] == [
        "adopt",
        "update:quarantined",
    ]
    assert store.adopted[0].status == "failed"
    assert store.session is not None
    assert store.session.status == "quarantined"
    assert store.session.active_run_id is None
