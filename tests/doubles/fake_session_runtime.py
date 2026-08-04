"""In-memory session-runtime doubles for controller and backend tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime

from azure_functions_agents.controller.package import CONTENT_MANIFEST_SEED_PATH
from azure_functions_agents.session_state import (
    AdmissionOutcome,
    AdmissionRecords,
    AdoptionOutcome,
    DurableRunRecord,
    DurableSessionRecord,
    OwnerPartition,
    RunRead,
    SessionRead,
)
from azure_functions_agents.transport.manifest import SESSION_MANIFEST_PATH
from azure_functions_agents.transport.transport_models import (
    ProvisionedSandboxIdentity,
    SandboxCreateRequest,
    SandboxGroupBinding,
    SandboxGroupIdentity,
)

from .fake_sandbox_transport import FakeSandboxTransport

DEFAULT_GROUP_RESOURCE_ID = (
    "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.App/sandboxGroups/group"
)


class FakeSandboxSessionHandle(FakeSandboxTransport):
    """A closeable file/process handle that can publish a live manifest seed."""

    def __init__(
        self,
        sandbox_id: str = "sandbox-1",
        *,
        group_resource_id: str = DEFAULT_GROUP_RESOURCE_ID,
    ) -> None:
        super().__init__()
        self.identity = ProvisionedSandboxIdentity.create(
            sandbox_id=sandbox_id,
            group_resource_id=group_resource_id,
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


class FakeSandboxSessionProvider:
    """A configurable provider double for one fixed Sandbox Group."""

    def __init__(
        self,
        handle: FakeSandboxSessionHandle,
        *,
        group_resource_id: str = DEFAULT_GROUP_RESOURCE_ID,
    ) -> None:
        self.group = SandboxGroupIdentity(
            resource_id=group_resource_id,
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
    ) -> FakeSandboxSessionHandle:
        assert persisted_group.region == self.group.region
        self.create_calls.append(request)
        return self.handle

    async def attach(self, *args: object, **kwargs: object) -> FakeSandboxSessionHandle:
        del args, kwargs
        self.attach_calls += 1
        if self.attach_delay:
            await asyncio.sleep(self.attach_delay)
        if self.attach_error is not None:
            raise self.attach_error
        return self.handle

    async def resume(self, *args: object, **kwargs: object) -> FakeSandboxSessionHandle:
        del args, kwargs
        self.resume_calls += 1
        if self.attach_error is not None:
            raise self.attach_error
        return self.handle

    async def close(self) -> None:
        self.closed = True


class FakeSessionStateStore:
    """A minimal in-memory state-store double with observable write ordering."""

    def __init__(self, session: DurableSessionRecord | None = None) -> None:
        self.session = session
        self.etag = "etag-1"
        self.adopted: list[DurableRunRecord] = []
        self.operations: list[str] = []
        self.runs: dict[str, DurableRunRecord] = {}
        self.admission_expected_session_etags: list[str | None] = []

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

    async def admit_run(
        self,
        records: AdmissionRecords,
        *,
        expected_session_etag: str | None = None,
    ) -> AdmissionOutcome:
        assert self.session is not None
        if expected_session_etag is not None:
            assert self.etag == expected_session_etag
        self.admission_expected_session_etags.append(expected_session_etag)
        assert self.session.active_run_id is None
        self.session = records.session
        self.runs[records.run.run_id] = records.run
        self.operations.append("admit")
        self.etag = "etag-admitted"
        return AdmissionOutcome(
            run=records.run,
            run_etag="run-etag",
            session_etag=self.etag,
            replayed=False,
        )

    async def get_run(
        self,
        partition: OwnerPartition,
        session_id: str,
        run_id: str,
    ) -> RunRead:
        del partition, session_id
        return RunRead(record=self.runs[run_id], etag="run-etag")

    async def adopt_terminal_run(self, terminal_run: DurableRunRecord) -> AdoptionOutcome:
        assert self.session is not None
        self.adopted.append(terminal_run)
        self.operations.append("adopt")
        self.runs[terminal_run.run_id] = terminal_run
        self.session = replace(
            self.session,
            status="ready",
            active_run_id=None,
            updated_at=terminal_run.updated_at,
        )
        self.etag = "etag-released"
        return AdoptionOutcome(run=terminal_run, run_etag="run-etag", slot_released=True)
