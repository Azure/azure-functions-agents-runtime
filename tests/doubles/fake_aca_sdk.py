"""Test-only stand-ins injected into the ACA SDK adapter's factory boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from azure_functions_agents.transport.aca_sdk import SdkFactories
from azure_functions_agents.transport.models import SandboxExecResult

from .fake_sandbox_transport import FakeSandboxTransport, RecordedTransportCall


@dataclass(frozen=True, slots=True)
class FakeSdkEgressPolicy:
    """Records the explicit egress values passed to the provider boundary."""

    default_action: str
    traffic_inspection: str


class FakeCredential:
    """Controller-only credential double."""

    def __init__(self) -> None:
        self.closed = False
        self.token_scopes: list[str] = []

    async def get_token(self, scope: str) -> SimpleNamespace:
        self.token_scopes.append(scope)
        return SimpleNamespace(token="test-token")

    async def close(self) -> None:
        self.closed = True


class FakeSdkSandboxClient:
    """A direct-file SDK-client stand-in with advisory ``get`` intentionally forbidden."""

    def __init__(self, sandbox_id: str, *, labels: dict[str, str] | None = None) -> None:
        self.sandbox_id = sandbox_id
        self.labels = dict(labels or {})
        self.transport = FakeSandboxTransport()
        self.calls = self.transport.calls
        self.closed = False
        self.deleted = False
        self.stop_kwargs: dict[str, Any] | None = None
        self.delete_kwargs: dict[str, Any] | None = None

    async def list_files(self, path: str) -> SimpleNamespace:
        entries = await self.transport.list_files(path)
        return SimpleNamespace(
            entries=[
                SimpleNamespace(
                    name=entry.name,
                    path=entry.path,
                    size=entry.size,
                    is_directory=entry.is_directory,
                    modified_at=entry.modified_at,
                    mode=entry.mode,
                )
                for entry in entries
            ]
        )

    async def stat_file(self, path: str) -> SimpleNamespace:
        stat = await self.transport.stat_file(path)
        return SimpleNamespace(
            path=stat.path,
            size=stat.size,
            is_directory=stat.is_directory,
            modified_at=stat.modified_at,
            mode=stat.mode,
        )

    async def read_file(self, path: str) -> bytes:
        return await self.transport.read_file(path)

    async def write_file(self, path: str, content: bytes, *, create_dirs: bool) -> None:
        await self.transport.write_file(path, content, create_dirs=create_dirs)

    async def delete_file(self, path: str, *, recursive: bool) -> None:
        if recursive:
            raise AssertionError("P4a must not request recursive file deletion")
        await self.transport.delete_file(path)

    async def mkdir(self, path: str) -> None:
        await self.transport.mkdir(path)

    async def exec(self, command: str) -> SimpleNamespace:
        result = await self.transport.exec(command)
        return SimpleNamespace(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    async def get(self) -> None:
        raise AssertionError("P4a readiness must not trust advisory sandbox state")

    async def begin_stop(self, **kwargs: Any) -> FakePoller:
        self.stop_kwargs = kwargs
        self.calls.append(RecordedTransportCall("stop"))
        return FakePoller(None)

    async def resume(self) -> None:
        self.calls.append(RecordedTransportCall("resume"))

    async def begin_delete(self, **kwargs: Any) -> FakePoller:
        self.delete_kwargs = kwargs
        self.calls.append(RecordedTransportCall("delete"))
        return FakePoller(None, on_result=self._mark_deleted)

    async def close(self) -> None:
        self.closed = True

    def _mark_deleted(self) -> None:
        self.deleted = True


class FakePoller:
    """The async poller returned by the preview client's create method."""

    def __init__(
        self,
        result: object,
        *,
        error: Exception | None = None,
        on_result: Callable[[], None] | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self._on_result = on_result

    async def result(self) -> object:
        if self._error is not None:
            raise self._error
        if self._on_result is not None:
            self._on_result()
        return self._result


class FakeSdkGroupClient:
    """Test-only group client that can create individual fake sandboxes."""

    def __init__(self, sandboxes: dict[str, FakeSdkSandboxClient], **kwargs: Any) -> None:
        self.sandboxes = sandboxes
        self.constructor_kwargs = kwargs
        self.create_calls: list[dict[str, Any]] = []
        self.deleted_sandbox_ids: list[str] = []
        self.create_result_error: Exception | None = None
        self.closed = False
        self.add_port_calls = 0

    async def begin_create_sandbox(self, **kwargs: Any) -> FakePoller:
        self.create_calls.append(kwargs)
        sandbox = FakeSdkSandboxClient(
            f"created-{len(self.create_calls)}",
            labels=kwargs.get("labels"),
        )
        self.sandboxes[sandbox.sandbox_id] = sandbox
        return FakePoller(sandbox, error=self.create_result_error)

    def list_sandboxes(
        self,
        *,
        labels: dict[str, str] | None = None,
    ) -> AsyncIterator[SimpleNamespace]:
        async def iterate() -> AsyncIterator[SimpleNamespace]:
            for sandbox in tuple(self.sandboxes.values()):
                if labels and any(sandbox.labels.get(key) != value for key, value in labels.items()):
                    continue
                yield SimpleNamespace(id=sandbox.sandbox_id, labels=sandbox.labels)

        return iterate()

    async def begin_delete_sandbox(self, sandbox_id: str, **kwargs: Any) -> FakePoller:
        del kwargs
        sandbox = self.sandboxes[sandbox_id]

        def delete() -> None:
            sandbox.deleted = True
            self.deleted_sandbox_ids.append(sandbox_id)
            del self.sandboxes[sandbox_id]

        return FakePoller(None, on_result=delete)

    async def close(self) -> None:
        self.closed = True

    def add_port(self) -> None:
        self.add_port_calls += 1
        raise AssertionError("P4a must never add an inbound port")


class FakeSdkEnvironment:
    """Factory bundle and state shared by adapter tests."""

    def __init__(self) -> None:
        self.sandboxes: dict[str, FakeSdkSandboxClient] = {}
        self.group_clients: list[FakeSdkGroupClient] = []
        self.endpoint_regions: list[str] = []
        self.sandbox_client_ids: list[str] = []

    def factories(self) -> SdkFactories:
        return SdkFactories(
            endpoint_for_region=self.endpoint_for_region,
            sandbox_group_client=self.make_group_client,
            sandbox_client=self.make_sandbox_client,
            egress_policy=FakeSdkEgressPolicy,
        )

    def endpoint_for_region(self, region: str) -> str:
        self.endpoint_regions.append(region)
        return f"https://sandbox.{region}.invalid"

    def make_group_client(self, endpoint: str, credential: object, **kwargs: Any) -> FakeSdkGroupClient:
        del endpoint, credential
        client = FakeSdkGroupClient(self.sandboxes, **kwargs)
        self.group_clients.append(client)
        return client

    def make_sandbox_client(
        self, endpoint: str, credential: object, *, sandbox_id: str, **kwargs: Any
    ) -> FakeSdkSandboxClient:
        del endpoint, credential, kwargs
        self.sandbox_client_ids.append(sandbox_id)
        return self.sandboxes[sandbox_id]

    def add_sandbox(self, sandbox_id: str) -> FakeSdkSandboxClient:
        sandbox = FakeSdkSandboxClient(sandbox_id)
        self.sandboxes[sandbox_id] = sandbox
        return sandbox

    @property
    def group_client(self) -> FakeSdkGroupClient:
        assert len(self.group_clients) == 1
        return self.group_clients[0]

    def set_exec_result(self, sandbox_id: str, result: SandboxExecResult) -> None:
        self.sandboxes[sandbox_id].transport.next_exec_result = result
