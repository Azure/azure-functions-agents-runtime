from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

import azure_functions_agents.controller.readiness as readiness_module
from azure_functions_agents.controller.package import LiveManifestNotReadyError
from azure_functions_agents.controller.readiness import (
    ATOMIC_CHECKPOINT_POINTER_PATH,
    HARNESS_PROTOCOL_PATH,
    ActivatedSession,
    SessionActivationError,
    SessionActivationGoneError,
    SessionActivationNotFoundError,
    SessionActivationSetupTimeoutError,
    SessionActivationUnavailableError,
    SessionActivationUntrustedError,
    SessionBindingChangedError,
    SessionRuntimeBinding,
    StateStoreBinding,
    activate_session,
    begin_submit_operation,
    disarm_submit_lifecycle,
    finalize_submit_operation,
    provision_new_session_submit,
    rearm_idle_lifecycle,
    revalidate_before_submit,
    session_with_admitted_run,
    touch_session_activity,
)
from azure_functions_agents.controller.reconciler import SessionReconciler
from azure_functions_agents.execution.setup_budget import SetupBudget
from azure_functions_agents.harness.sandbox_capabilities import REQUIRED_HARNESS_CAPABILITIES
from azure_functions_agents.journal_paths import BOOTSTRAP_ERROR_PATH
from azure_functions_agents.session_state import (
    AdmissionRecords,
    AppIdentity,
    ConcurrencyConflictError,
    DurableRunRecord,
    DurableSessionRecord,
    FunctionAppOwnerContext,
    SessionNotAdmissibleError,
    SessionOperationTarget,
    SessionStateContractError,
    StateStoreUnavailableError,
    owner_partition,
)
from azure_functions_agents.transport.manifest import SandboxManifestMismatchError
from azure_functions_agents.transport.transport_models import DiskSource, SandboxFileOperationError
from tests.doubles.content_package import content_package
from tests.doubles.fake_session_runtime import DEFAULT_GROUP_RESOURCE_ID
from tests.doubles.fake_session_runtime import FakeSandboxSessionHandle as _FakeHandle
from tests.doubles.fake_session_runtime import FakeSandboxSessionProvider as _FakeProvider
from tests.doubles.fake_session_runtime import FakeSessionStateStore as _FakeStore

_GROUP_RESOURCE_ID = DEFAULT_GROUP_RESOURCE_ID
_FINGERPRINT = "s1-" + ("a" * 52)
_TEST_SOURCE = DiskSource.create("test-harness")
pytestmark = pytest.mark.usefixtures("deterministic_content_package")


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
    package = content_package()
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
        active_operation_id=None,
        operation_sequence=0,
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
    post_create_reconciler: Callable[[], Awaitable[None]] | None = None,
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
        post_create_reconciler=post_create_reconciler,
    )


def _script_root(tmp_path: Path) -> Path:
    (tmp_path / "function_app.py").write_text("app = object()\n", encoding="utf-8")
    return tmp_path


class _CountingHandle(_FakeHandle):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        await super().close()


@pytest.mark.asyncio
async def test_profile_readiness_requires_exact_protocol_capabilities(tmp_path: Path) -> None:
    session = _session(_script_root(tmp_path))
    handle = _FakeHandle()
    handle.seed_file(
        HARNESS_PROTOCOL_PATH,
        (
            '{"protocol_version":"1","capabilities":'
            + json.dumps(dict(REQUIRED_HARNESS_CAPABILITIES), sort_keys=True)
            + "}\n"
        ).encode("utf-8"),
    )

    await readiness_module._verify_optional_harness_artifacts(
        handle,
        session,
        require_protocol=True,
    )

    handle.seed_file(
        HARNESS_PROTOCOL_PATH,
        b'{"protocol_version":"1","capabilities":{"bootstrap":"bootstrap_v1"}}',
    )
    with pytest.raises(readiness_module.SessionReadinessArtifactError, match="capability_mismatch"):
        await readiness_module._verify_optional_harness_artifacts(
            handle,
            session,
            require_protocol=True,
        )


@pytest.mark.asyncio
async def test_reserved_provision_keeps_successful_created_handle_open(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    handle = _CountingHandle()
    store = _FakeStore()
    runtime = _runtime(script_root, _FakeProvider(handle), store)

    provisioned = await provision_new_session_submit(
        runtime,
        _owner(),
        session_id="new-session",
        run_id="run-1",
        timeout=None,
        attempt=None,
        setup_deadline=SetupBudget.start(),
    )

    assert provisioned.activated is not None
    assert provisioned.activated.handle is handle
    assert provisioned.activated.session.checkpoint_expectation == "required"
    assert handle.close_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["lifecycle", "content", "manifest", "phase"])
async def test_reserved_provision_closes_created_handle_after_post_create_failure(
    failure_phase: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _PhaseFailureStore(_FakeStore):
        async def advance_operation(self, **kwargs: object):  # type: ignore[no-untyped-def]
            if failure_phase == "phase" and kwargs["phase"] == "provision_lifecycle":
                raise ConcurrencyConflictError("phase write failed")
            return await super().advance_operation(**kwargs)

    class _LifecycleFailureHandle(_CountingHandle):
        async def set_lifecycle_policy(self, policy):  # type: ignore[no-untyped-def]
            if failure_phase == "lifecycle":
                raise SandboxFileOperationError("lifecycle failed")
            await super().set_lifecycle_policy(policy)

    async def fail_content(*_args: object, **_kwargs: object) -> None:
        raise readiness_module.ContentPackagingError("content failed")

    async def fail_manifest(*_args: object, **_kwargs: object) -> None:
        raise readiness_module.LiveManifestNotReadyError("manifest failed")

    if failure_phase == "content":
        monkeypatch.setattr(readiness_module, "deliver_content_and_bootstrap", fail_content)
    if failure_phase == "manifest":
        monkeypatch.setattr(readiness_module, "_wait_for_created_manifest", fail_manifest)

    script_root = _script_root(tmp_path)
    handle = _LifecycleFailureHandle()
    store = _PhaseFailureStore()
    runtime = _runtime(script_root, _FakeProvider(handle), store)

    with pytest.raises(
        (
            SandboxFileOperationError,
            SessionActivationError,
            readiness_module.ContentPackagingError,
            readiness_module.LiveManifestNotReadyError,
            ConcurrencyConflictError,
        )
    ):
        await provision_new_session_submit(
            runtime,
            _owner(),
            session_id="new-session",
            run_id="run-1",
            timeout=None,
            attempt=None,
            setup_deadline=SetupBudget.start(),
        )

    assert handle.close_calls == 1


@pytest.mark.asyncio
async def test_reserved_provision_closes_handle_when_post_create_reconcile_fails(
    tmp_path: Path,
) -> None:
    async def fail_reconcile() -> None:
        raise RuntimeError("post-create reconcile failed")

    script_root = _script_root(tmp_path)
    handle = _CountingHandle()
    store = _FakeStore()
    runtime = _runtime(
        script_root,
        _FakeProvider(handle),
        store,
        post_create_reconciler=fail_reconcile,
    )

    with pytest.raises(RuntimeError, match="post-create reconcile failed"):
        await provision_new_session_submit(
            runtime,
            _owner(),
            session_id="new-session",
            run_id="run-1",
            timeout=None,
            attempt=None,
            setup_deadline=SetupBudget.start(),
        )

    assert handle.close_calls == 1


@pytest.mark.asyncio
async def test_session_locks_do_not_serialize_distinct_owners(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    provider = _FakeProvider(_FakeHandle())
    store = _FakeStore(_session(script_root))
    runtime = _runtime(script_root, provider, store)
    first_owner = _owner()
    second_owner = FunctionAppOwnerContext.create(first_owner.app_identity, "other")
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def hold_first() -> None:
        async with runtime.hold_session(owner_partition(first_owner), "session-1"):
            first_entered.set()
            await release_first.wait()

    async def hold_second() -> None:
        async with runtime.hold_session(owner_partition(second_owner), "session-1"):
            second_entered.set()

    first = asyncio.create_task(hold_first())
    await asyncio.wait_for(first_entered.wait(), timeout=1.0)
    second = asyncio.create_task(hold_second())
    try:
        await asyncio.wait_for(second_entered.wait(), timeout=1.0)
    finally:
        release_first.set()
        await first
        await second


@pytest.mark.asyncio
@pytest.mark.parametrize(
        ("status", "touches"),
        [
            ("ready", True),
            ("running", True),
            ("canceling", True),
            ("suspending", True),
            ("suspended", True),
            ("resuming", True),
            ("failed", False),
            ("quarantined", False),
            ("tombstoned", False),
            ("deleting", False),
            ("deleted", False),
        ],
)
async def test_management_touch_only_renews_live_session_retention(
        status: str,
        touches: bool,
        tmp_path: Path,
) -> None:
        script_root = _script_root(tmp_path)
        base = _session(script_root)
        if status in {"running", "canceling"}:
            record = session_with_admitted_run(base, "run-1", updated_at=base.updated_at)
            record = replace(record, status=status)
        else:
            record = replace(base, status=status)
        store = _FakeStore(record)
        runtime = _runtime(script_root, _FakeProvider(_FakeHandle()), store)
        previous = record

        await touch_session_activity(runtime, _owner(), record.session_id)

        assert store.session is not None
        if touches:
            assert store.session.expires_at > previous.expires_at
            assert store.operations == [f"update:{status}"]
        else:
            assert store.session == previous
            assert store.operations == []


@pytest.mark.asyncio
async def test_rearm_rejects_an_orphan_disarmed_idle_marker(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    session = replace(_session(script_root), idle_policy_armed=False)
    store = _FakeStore(session)
    handle = _FakeHandle()
    runtime = _runtime(script_root, _FakeProvider(handle), store)
    activated = ActivatedSession.create(
        handle=handle,
        session=session,
        etag=store.etag,
        partition=session.owner_partition,
        store=store,
    )

    with pytest.raises(
        SessionStateContractError,
        match="disarmed idle lifecycle requires an active durable operation",
    ):
        await rearm_idle_lifecycle(runtime, activated)

    assert store.session == session


@pytest.mark.asyncio
async def test_management_touch_retries_once_from_a_fresh_session_read(
    tmp_path: Path,
) -> None:
    class _ConflictOnceStore(_FakeStore):
            def __init__(self, session: DurableSessionRecord) -> None:
                super().__init__(session)
                self.update_attempts = 0

            async def update_session(self, **kwargs: object) -> str:  # type: ignore[no-untyped-def]
                self.update_attempts += 1
                if self.update_attempts == 1:
                    assert self.session is not None
                    self.session = replace(
                        self.session,
                        idle_policy_armed=False,
                        snapshot_ids=("newer-snapshot",),
                    )
                    self.etag = "newer-etag"
                    raise ConcurrencyConflictError("session changed")
                return await super().update_session(**kwargs)

    script_root = _script_root(tmp_path)
    store = _ConflictOnceStore(_session(script_root))
    runtime = _runtime(script_root, _FakeProvider(_FakeHandle()), store)

    await touch_session_activity(runtime, _owner(), "session-1")

    assert store.update_attempts == 2
    assert store.session is not None
    assert not store.session.idle_policy_armed
    assert store.session.snapshot_ids == ("newer-snapshot",)
    assert store.operations == ["update:ready"]


@pytest.mark.asyncio
async def test_management_touch_ignores_only_a_second_etag_conflict(
    tmp_path: Path,
) -> None:
    class _AlwaysConflictStore(_FakeStore):
            def __init__(self, session: DurableSessionRecord) -> None:
                super().__init__(session)
                self.update_attempts = 0

            async def update_session(self, **_kwargs: object) -> str:  # type: ignore[no-untyped-def]
                self.update_attempts += 1
                raise ConcurrencyConflictError("session changed")

    script_root = _script_root(tmp_path)
    store = _AlwaysConflictStore(_session(script_root))
    runtime = _runtime(script_root, _FakeProvider(_FakeHandle()), store)

    await touch_session_activity(runtime, _owner(), "session-1")

    assert store.update_attempts == 2
    assert store.operations == []


@pytest.mark.asyncio
async def test_management_touch_propagates_non_concurrency_store_errors(
    tmp_path: Path,
) -> None:
    class _UnavailableStore(_FakeStore):
            async def update_session(self, **_kwargs: object) -> str:  # type: ignore[no-untyped-def]
                raise StateStoreUnavailableError("unavailable")

    script_root = _script_root(tmp_path)
    store = _UnavailableStore(_session(script_root))
    runtime = _runtime(script_root, _FakeProvider(_FakeHandle()), store)

    with pytest.raises(StateStoreUnavailableError, match="unavailable"):
            await touch_session_activity(runtime, _owner(), "session-1")


@pytest.mark.asyncio
async def test_attach_requires_the_provider_handshake_and_protocol_capabilities(
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
    assert [call.path for call in handle.calls] == [
        HARNESS_PROTOCOL_PATH,
        ATOMIC_CHECKPOINT_POINTER_PATH,
    ]
    await activated.handle.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_attach_calls", "expected_resume_calls"),
    [("ready", 1, 0), ("suspended", 0, 1)],
)
async def test_activation_closes_untransferred_handle_after_harness_file_failure(
    tmp_path: Path,
    status: str,
    expected_attach_calls: int,
    expected_resume_calls: int,
) -> None:
    class _ArtifactFailureHandle(_CountingHandle):
        async def read_file(self, path: str) -> bytes:
            if path == ATOMIC_CHECKPOINT_POINTER_PATH:
                raise SandboxFileOperationError("file plane unavailable")
            return await super().read_file(path)

    script_root = _script_root(tmp_path)
    handle = _ArtifactFailureHandle()
    provider = _FakeProvider(handle)
    session = _session(script_root, status=status)
    store = _FakeStore(session)

    with pytest.raises(SessionActivationUnavailableError):
        await activate_session(
            _runtime(script_root, provider, store),
            _owner(),
            session.session_id,
            SetupBudget.start(),
            allow_create=False,
        )

    assert provider.attach_calls == expected_attach_calls
    assert provider.resume_calls == expected_resume_calls
    assert handle.close_calls == 1
    assert store.session == session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_attach_calls", "expected_resume_calls"),
    [("ready", 1, 0), ("suspended", 0, 1)],
)
async def test_activation_closes_untransferred_handle_when_harness_read_is_cancelled(
    tmp_path: Path,
    status: str,
    expected_attach_calls: int,
    expected_resume_calls: int,
) -> None:
    class _CancelledArtifactHandle(_CountingHandle):
        async def read_file(self, path: str) -> bytes:
            if path == ATOMIC_CHECKPOINT_POINTER_PATH:
                raise asyncio.CancelledError
            return await super().read_file(path)

    script_root = _script_root(tmp_path)
    handle = _CancelledArtifactHandle()
    provider = _FakeProvider(handle)
    session = _session(script_root, status=status)

    with pytest.raises(asyncio.CancelledError):
        await activate_session(
            _runtime(script_root, provider, _FakeStore(session)),
            _owner(),
            session.session_id,
            SetupBudget.start(),
            allow_create=False,
        )

    assert provider.attach_calls == expected_attach_calls
    assert provider.resume_calls == expected_resume_calls
    assert handle.close_calls == 1


@pytest.mark.asyncio
async def test_verified_quarantined_and_unbound_sessions_have_narrow_not_found_subtypes(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)

    quarantined = replace(
        _session(script_root),
        status="quarantined",
        quarantine_reason="checkpoint_corrupt",
    )
    runtime = _runtime(script_root, _FakeProvider(_FakeHandle()), _FakeStore(quarantined))
    with pytest.raises(SessionActivationUntrustedError):
        await activate_session(
            runtime,
            _owner(),
            quarantined.session_id,
            SetupBudget.start(),
            allow_create=False,
        )

    unbound = _session(script_root, sandbox_id=None)
    runtime = _runtime(script_root, _FakeProvider(_FakeHandle()), _FakeStore(unbound))
    with pytest.raises(SessionActivationUnavailableError):
        await activate_session(
            runtime,
            _owner(),
            unbound.session_id,
            SetupBudget.start(),
            allow_create=False,
        )


@pytest.mark.asyncio
async def test_resume_requires_the_provider_handshake_and_protocol_capabilities(
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
    assert activated.resumed is True
    assert [call.path for call in handle.calls] == [
        HARNESS_PROTOCOL_PATH,
        ATOMIC_CHECKPOINT_POINTER_PATH,
    ]
    await activated.handle.close()


@pytest.mark.asyncio
async def test_manifest_mismatch_quarantines_without_deleting_state(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    provider = _FakeProvider(_FakeHandle())
    provider.attach_error = SandboxManifestMismatchError(frozenset({"sandbox_id"}))
    session = _session(script_root)
    store = _FakeStore(session)

    with pytest.raises(SessionActivationUntrustedError):
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
async def test_corrupt_optional_harness_protocol_quarantines_before_admission(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    handle = _FakeHandle("new-sandbox")
    handle.seed_file(HARNESS_PROTOCOL_PATH, b"not-json")
    provider = _FakeProvider(handle)
    store = _FakeStore()

    with pytest.raises(SessionActivationNotFoundError):
        await activate_session(
            _runtime(script_root, provider, store),
            _owner(),
            "new-session",
            SetupBudget.start(),
            allow_create=True,
        )

    assert store.session is not None
    assert store.session.status == "quarantined"
    assert store.session.quarantine_reason == "protocol_version_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "protocol_payload",
    [
        b'{"protocol_version":"1","protocol_version":"forged"}',
        b'{"protocol_version":1}',
        b'{"protocol_version":"1","capabilities":{"delegation":1}}',
    ],
)
async def test_optional_harness_protocol_rejects_duplicate_keys_and_coercion(
    tmp_path: Path,
    protocol_payload: bytes,
) -> None:
    script_root = _script_root(tmp_path)
    handle = _FakeHandle("new-sandbox")
    handle.seed_file(HARNESS_PROTOCOL_PATH, protocol_payload)
    provider = _FakeProvider(handle)
    store = _FakeStore()

    with pytest.raises(SessionActivationNotFoundError):
        await activate_session(
            _runtime(script_root, provider, store),
            _owner(),
            "new-session",
            SetupBudget.start(),
            allow_create=True,
        )

    assert store.session is not None
    assert store.session.quarantine_reason == "protocol_version_mismatch"


@pytest.mark.asyncio
async def test_optional_checkpoint_pointer_requires_a_canonical_uuid_name(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    handle = _FakeHandle("new-sandbox")
    handle.seed_file(ATOMIC_CHECKPOINT_POINTER_PATH, b"checkpoint_not_a_uuid\n")
    provider = _FakeProvider(handle)
    store = _FakeStore()

    with pytest.raises(SessionActivationNotFoundError):
        await activate_session(
            _runtime(script_root, provider, store),
            _owner(),
            "new-session",
            SetupBudget.start(),
            allow_create=True,
        )

    assert store.session is not None
    assert store.session.quarantine_reason == "checkpoint_corrupt"


@pytest.mark.asyncio
async def test_optional_checkpoint_pointer_accepts_a_canonical_uuid_name(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    handle = _FakeHandle("new-sandbox")
    checkpoint_name = f"checkpoint_{uuid4().hex}"
    handle.seed_file(
        ATOMIC_CHECKPOINT_POINTER_PATH,
        f"{checkpoint_name}\n".encode("ascii"),
    )
    provider = _FakeProvider(handle)
    store = _FakeStore()

    activated = await activate_session(
        _runtime(script_root, provider, store),
        _owner(),
        "new-session",
        SetupBudget.start(),
        allow_create=True,
    )

    assert activated.session.status == "ready"
    assert activated.checkpoint_name == checkpoint_name
    assert [call.path for call in handle.calls].count(ATOMIC_CHECKPOINT_POINTER_PATH) == 1
    await activated.handle.close()


@pytest.mark.asyncio
async def test_permanent_bootstrap_error_report_quarantines_provisioned_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_live_manifest(*_args: object, **_kwargs: object) -> None:
        raise LiveManifestNotReadyError("not ready")

    script_root = _script_root(tmp_path)
    handle = _FakeHandle("new-sandbox")
    handle.seed_file(
        BOOTSTRAP_ERROR_PATH,
        b'{"code":"content_digest_mismatch","message":"content invalid","permanent":true}',
    )
    store = _FakeStore()
    monkeypatch.setattr(readiness_module, "read_live_manifest_binding", no_live_manifest)

    with pytest.raises(SessionActivationNotFoundError):
        await provision_new_session_submit(
            _runtime(script_root, _FakeProvider(handle), store),
            _owner(),
            session_id="new-session",
            run_id="run-1",
            timeout=None,
            attempt=None,
            setup_deadline=SetupBudget.start(),
        )

    assert store.session is not None
    assert store.session.status == "quarantined"
    assert store.session.quarantine_reason == "bootstrap_failure"
    assert store.adopted[0].status == "failed"
    assert store.adopted[0].status_reason == "bootstrap_failure"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "report",
    [
        None,
        b'{"code":"retrying","message":"try again","permanent":false}',
    ],
)
async def test_missing_or_transient_bootstrap_report_keeps_manifest_polling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report: bytes | None,
) -> None:
    calls = 0

    async def eventual_manifest(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LiveManifestNotReadyError("not ready")

    async def no_sleep(_delay: float) -> None:
        return None

    script_root = _script_root(tmp_path)
    session = _session(script_root)
    handle = _FakeHandle()
    if report is not None:
        handle.seed_file(BOOTSTRAP_ERROR_PATH, report)
    expected = readiness_module.build_expected_manifest_binding(
        session,
        sandbox_group_resource_id=_GROUP_RESOURCE_ID,
        state_store_fingerprint=_FINGERPRINT,
    )
    monkeypatch.setattr(readiness_module, "read_live_manifest_binding", eventual_manifest)
    monkeypatch.setattr(readiness_module.asyncio, "sleep", no_sleep)

    await readiness_module._wait_for_created_manifest(
        handle,
        expected=expected,
        setup_deadline=SetupBudget.start(),
    )

    assert calls == 2


@pytest.mark.asyncio
async def test_malformed_bootstrap_error_report_fails_closed(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    handle = _FakeHandle()
    handle.seed_file(BOOTSTRAP_ERROR_PATH, b"not-json")
    expected = readiness_module.build_expected_manifest_binding(
        session,
        sandbox_group_resource_id=_GROUP_RESOURCE_ID,
        state_store_fingerprint=_FINGERPRINT,
    )

    with pytest.raises(readiness_module.SessionReadinessArtifactError) as error:
        await readiness_module._wait_for_created_manifest(
            handle,
            expected=expected,
            setup_deadline=SetupBudget.start(),
        )

    assert error.value.reason == "bootstrap_failure"


@pytest.mark.asyncio
async def test_manifest_mismatch_releases_an_active_slot_before_quarantine(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    provider = _FakeProvider(_FakeHandle())
    provider.attach_error = SandboxManifestMismatchError(frozenset({"sandbox_id"}))
    base_session = _session(script_root)
    active_session = session_with_admitted_run(
        base_session,
        "run-1",
        updated_at=datetime.now(UTC),
    )
    store = _FakeStore(active_session)
    store.runs["run-1"] = _run(base_session)

    with pytest.raises(SessionActivationNotFoundError):
        await activate_session(
            _runtime(script_root, provider, store),
            _owner(),
            active_session.session_id,
            SetupBudget.start(),
            allow_create=False,
        )

    assert store.operations == ["adopt", "update:quarantined"]
    assert store.session is not None
    assert store.session.status == "quarantined"
    assert store.session.active_run_id is None


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
async def test_suspended_stale_digest_tombstones_before_provider_resume(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    provider = _FakeProvider(_FakeHandle())
    session = replace(
        _session(script_root, status="suspended"),
        digest="sha256:" + ("b" * 64),
    )
    store = _FakeStore(session)

    with pytest.raises(SessionActivationGoneError):
        await activate_session(
            _runtime(script_root, provider, store),
            _owner(),
            session.session_id,
            SetupBudget.start(),
            allow_create=False,
        )

    assert provider.resume_calls == 0
    assert store.session is not None
    assert store.session.status == "tombstoned"


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
    assert store.session.checkpoint_expectation == "none"
    assert not handle.closed
    assert handle.lifecycle_policy.auto_suspend_seconds == 300
    assert handle.lifecycle_policy.auto_delete_seconds == 90_300
    await activated.handle.close()


@pytest.mark.asyncio
async def test_partial_delivery_stays_creating_until_reconciler_reclaims_candidate(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    handle = _FakeHandle("new-sandbox")
    handle.write_errors.append(SandboxFileOperationError("partial delivery"))
    provider = _FakeProvider(handle)
    store = _FakeStore()

    with pytest.raises(SandboxFileOperationError):
        await activate_session(
            _runtime(script_root, provider, store),
            _owner(),
            "new-session",
            SetupBudget.start(),
            allow_create=True,
        )

    assert store.session is not None
    assert store.session.status == "creating"
    store.session = replace(
        store.session,
        created_at=datetime.now(UTC) - timedelta(minutes=10),
        updated_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    report = await SessionReconciler(
        store=store,
        provider=provider,
        app_hash=owner_partition(_owner()).app_hash,
    ).run_once()

    assert report.tombstoned_sessions == 1
    assert store.session.status == "tombstoned"


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


@pytest.mark.asyncio
async def test_terminal_submit_fences_admission_before_lifecycle_write(tmp_path: Path) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = _FakeStore(session)
    admission_blocked = False

    class AdmissionProbeHandle(_FakeHandle):
        async def set_lifecycle_policy(self, policy):  # type: ignore[no-untyped-def]
            nonlocal admission_blocked
            if policy.auto_suspend_seconds is not None:
                assert store.session is not None
                candidate = session_with_admitted_run(
                    store.session,
                    "run-2",
                    updated_at=datetime.now(UTC),
                )
                with pytest.raises(SessionNotAdmissibleError):
                    await store.admit_run(
                        AdmissionRecords.create(
                            candidate,
                            replace(_run(session), run_id="run-2"),
                        )
                    )
                admission_blocked = True
            await super().set_lifecycle_policy(policy)

    handle = AdmissionProbeHandle()
    runtime = _runtime(script_root, _FakeProvider(handle), store)
    activated = ActivatedSession.create(
        handle=handle,
        session=session,
        etag=store.etag,
        partition=session.owner_partition,
        store=store,
    )
    run = _run(session)
    prepared, fence = await begin_submit_operation(activated, run)
    prepared, fence = await disarm_submit_lifecycle(runtime, prepared, fence)
    admitted = session_with_admitted_run(
        prepared.session,
        run.run_id,
        updated_at=run.updated_at,
    )
    await store.admit_operation_run(
        fence=fence,
        records=AdmissionRecords.create(admitted, run),
    )
    await store.adopt_terminal_run(replace(run, status="succeeded"))

    assert await finalize_submit_operation(
        runtime,
        activated,
        expected_run_id=run.run_id,
    )
    assert admission_blocked
    assert store.session is not None
    assert store.session.active_operation_id is None
    assert store.session.idle_policy_armed
    operation = next(iter(store.durable_operations.values()))
    assert operation.kind == "submit_run"
    assert operation.state == "completed"


@pytest.mark.asyncio
async def test_submit_rearm_resumes_a_durable_operation_after_lifecycle_failure(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = _FakeStore(session)

    class FailOnceHandle(_FakeHandle):
        def __init__(self) -> None:
            super().__init__()
            self.fail_once = True

        async def set_lifecycle_policy(self, policy):  # type: ignore[no-untyped-def]
            if self.fail_once and policy.auto_suspend_seconds is not None:
                self.fail_once = False
                raise SandboxFileOperationError("transient lifecycle failure")
            await super().set_lifecycle_policy(policy)

    handle = FailOnceHandle()
    runtime = _runtime(script_root, _FakeProvider(handle), store)
    activated = ActivatedSession.create(
        handle=handle,
        session=session,
        etag=store.etag,
        partition=session.owner_partition,
        store=store,
    )
    run = _run(session)
    prepared, fence = await begin_submit_operation(activated, run)
    prepared, fence = await disarm_submit_lifecycle(runtime, prepared, fence)
    admitted = session_with_admitted_run(
        prepared.session,
        run.run_id,
        updated_at=run.updated_at,
    )
    await store.admit_operation_run(
        fence=fence,
        records=AdmissionRecords.create(admitted, run),
    )
    await store.adopt_terminal_run(replace(run, status="succeeded"))

    with pytest.raises(SandboxFileOperationError):
        await finalize_submit_operation(
            runtime,
            activated,
            expected_run_id=run.run_id,
        )

    assert store.session is not None
    operation_id = store.session.active_operation_id
    assert operation_id is not None
    assert store.durable_operations[operation_id].error_code == "lifecycle_policy_apply_failed"

    assert await finalize_submit_operation(
        runtime,
        activated,
        expected_run_id=run.run_id,
    )
    assert store.session.active_operation_id is None
    assert store.durable_operations[operation_id].state == "completed"


@pytest.mark.asyncio
async def test_finalize_submit_operation_aborts_a_missing_run_without_stranding(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = _FakeStore(session)
    handle = _FakeHandle()
    runtime = _runtime(script_root, _FakeProvider(handle), store)
    activated = ActivatedSession.create(
        handle=handle,
        session=session,
        etag=store.etag,
        partition=session.owner_partition,
        store=store,
    )
    run = _run(session)
    prepared, fence = await begin_submit_operation(activated, run)
    prepared, fence = await disarm_submit_lifecycle(runtime, prepared, fence)
    admitted = session_with_admitted_run(
        prepared.session,
        run.run_id,
        updated_at=run.updated_at,
    )
    await store.admit_operation_run(
        fence=fence,
        records=AdmissionRecords.create(admitted, run),
    )
    store.runs.pop(run.run_id)

    assert await finalize_submit_operation(
        runtime,
        activated,
        expected_run_id=run.run_id,
    )
    assert store.session is not None
    assert store.session.status == "ready"
    assert store.session.active_run_id is None
    assert store.session.active_operation_id is None
    assert next(iter(store.durable_operations.values())).state == "aborted"


@pytest.mark.asyncio
async def test_finalize_submit_operation_preserves_security_quarantine(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = _FakeStore(session)
    handle = _FakeHandle()
    runtime = _runtime(script_root, _FakeProvider(handle), store)
    activated = ActivatedSession.create(
        handle=handle,
        session=session,
        etag=store.etag,
        partition=session.owner_partition,
        store=store,
    )
    run = _run(session)
    prepared, fence = await begin_submit_operation(activated, run)
    prepared, fence = await disarm_submit_lifecycle(runtime, prepared, fence)
    admitted = session_with_admitted_run(
        prepared.session,
        run.run_id,
        updated_at=run.updated_at,
    )
    await store.admit_operation_run(
        fence=fence,
        records=AdmissionRecords.create(admitted, run),
    )
    await store.adopt_terminal_run(replace(run, status="failed"))
    assert store.session is not None
    quarantined = DurableSessionRecord.create(
        owner_partition=store.session.owner_partition,
        session_id=store.session.session_id,
        sandbox_id=store.session.sandbox_id,
        generation=store.session.generation,
        digest_kind=store.session.digest_kind,
        digest=store.session.digest,
        protocol=store.session.protocol,
        status="quarantined",
        last_activity_at=store.session.last_activity_at,
        expires_at=store.session.expires_at,
        idle_policy_armed=False,
        active_run_id=None,
        snapshot_ids=store.session.snapshot_ids,
        region=store.session.region,
        state_store_fingerprint=store.session.state_store_fingerprint,
        quarantine_reason="sandbox_manifest_mismatch",
        tombstone_reason=None,
        created_at=store.session.created_at,
        updated_at=datetime.now(UTC),
        active_operation_id=store.session.active_operation_id,
        operation_sequence=store.session.operation_sequence,
    )
    store.session = quarantined

    assert await finalize_submit_operation(
        runtime,
        activated,
        expected_run_id=run.run_id,
    )
    assert store.session.status == "quarantined"
    assert store.session.quarantine_reason == "sandbox_manifest_mismatch"
    candidate = session_with_admitted_run(
        store.session,
        "run-2",
        updated_at=datetime.now(UTC),
    )
    with pytest.raises(SessionNotAdmissibleError):
        await store.admit_run(
            AdmissionRecords.create(candidate, replace(run, run_id="run-2"))
        )


@pytest.mark.asyncio
async def test_missing_run_finalization_preserves_security_quarantine(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = _FakeStore(session)
    handle = _FakeHandle()
    runtime = _runtime(script_root, _FakeProvider(handle), store)
    activated = ActivatedSession.create(
        handle=handle,
        session=session,
        etag=store.etag,
        partition=session.owner_partition,
        store=store,
    )
    run = _run(session)
    prepared, fence = await begin_submit_operation(activated, run)
    prepared, fence = await disarm_submit_lifecycle(runtime, prepared, fence)
    admitted = session_with_admitted_run(
        prepared.session,
        run.run_id,
        updated_at=run.updated_at,
    )
    await store.admit_operation_run(
        fence=fence,
        records=AdmissionRecords.create(admitted, run),
    )
    assert store.session is not None
    store.session = DurableSessionRecord.create(
        owner_partition=store.session.owner_partition,
        session_id=store.session.session_id,
        sandbox_id=store.session.sandbox_id,
        generation=store.session.generation,
        digest_kind=store.session.digest_kind,
        digest=store.session.digest,
        protocol=store.session.protocol,
        status="quarantined",
        last_activity_at=store.session.last_activity_at,
        expires_at=store.session.expires_at,
        idle_policy_armed=False,
        active_run_id=None,
        snapshot_ids=store.session.snapshot_ids,
        region=store.session.region,
        state_store_fingerprint=store.session.state_store_fingerprint,
        quarantine_reason="sandbox_manifest_mismatch",
        tombstone_reason=None,
        created_at=store.session.created_at,
        updated_at=datetime.now(UTC),
        active_operation_id=store.session.active_operation_id,
        operation_sequence=store.session.operation_sequence,
    )
    store.runs.pop(run.run_id)

    assert await finalize_submit_operation(
        runtime,
        activated,
        expected_run_id=run.run_id,
    )
    assert store.session.status == "quarantined"
    assert store.session.quarantine_reason == "sandbox_manifest_mismatch"
    assert store.session.active_operation_id is None


@pytest.mark.asyncio
async def test_terminal_poll_does_not_take_over_a_newer_submit_operation(
    tmp_path: Path,
) -> None:
    script_root = _script_root(tmp_path)
    session = _session(script_root)
    store = _FakeStore(session)
    handle = _FakeHandle()
    runtime = _runtime(script_root, _FakeProvider(handle), store)
    activated = ActivatedSession.create(
        handle=handle,
        session=session,
        etag=store.etag,
        partition=session.owner_partition,
        store=store,
    )
    old_run = _run(session)
    _prepared, fence = await begin_submit_operation(activated, old_run)
    newer_target = SessionOperationTarget.create(
        session_id=session.session_id,
        sandbox_id=session.sandbox_id,
        generation=session.generation,
        digest_kind=session.digest_kind,
        digest=session.digest,
        run_id="run-2",
    )
    store.durable_operations[fence.operation_id] = replace(
        store.durable_operations[fence.operation_id],
        target=newer_target,
    )

    assert not await finalize_submit_operation(
        runtime,
        activated,
        expected_run_id=old_run.run_id,
    )
    assert store.durable_operations[fence.operation_id].target.run_id == "run-2"
    assert "resume_operation" not in store.operations
    assert len(handle.lifecycle_policy_history) == 1
