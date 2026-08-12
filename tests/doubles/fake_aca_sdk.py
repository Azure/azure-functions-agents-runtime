"""Test-only stand-ins injected into the ACA SDK adapter's factory boundary.

These doubles construct and emit SDK-shaped response objects — matching the
field names, defaults, and untouched ``mode`` passthrough of the real SDK's
``FileInfo``/``DirListing``/``ExecResult``/``Sandbox`` — instead of
round-tripping values through this adapter's own runtime types. That is what
pins the real SDK response contract instead of mirroring this adapter's own
assumptions about it.

These stand-ins deliberately do **not** import the preview SDK package
itself: it is an optional preview extra (see ``pyproject.toml``) that this
repository's default test/CI environment does not install, and
``transport/aca_sdk.py`` is the only module allowed to import it (see
``test_transport_import_graph.py``). Mirroring the SDK's shape here keeps
these tests independent of whether the optional extra happens to be present.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from azure.core.credentials import AccessToken
from azure.core.exceptions import ResourceNotFoundError

from azure_functions_agents.transport.aca_sdk import SdkFactories
from azure_functions_agents.transport.transport_models import (
    SandboxExecResult,
    SandboxFileNotFoundError,
)

from .fake_sandbox_transport import FakeSandboxTransport, RecordedTransportCall


@dataclass(frozen=True, slots=True)
class FakeSdkEgressPolicy:
    """Records the explicit egress values passed to the provider boundary."""

    default_action: Literal["Allow", "Deny"] = "Allow"
    host_rules: list[FakeSdkEgressHostRule] = field(default_factory=list)
    rules: list[FakeSdkEgressRule] = field(default_factory=list)
    traffic_inspection: Literal["Legacy", "Full", "Partial", "None"] | None = None


@dataclass(frozen=True, slots=True)
class FakeSdkEgressHostRule:
    pattern: str = ""
    action: Literal["Allow", "Deny"] = "Allow"


@dataclass(frozen=True, slots=True)
class FakeSdkEgressRuleMatch:
    host: str = ""
    path: str | None = None
    methods: list[str] | None = None


@dataclass(frozen=True, slots=True)
class FakeSdkEgressSecretRef:
    secret_id: str = ""
    secret_key: str | None = None
    format: str | None = None


@dataclass(frozen=True, slots=True)
class FakeSdkEgressManagedIdentityRef:
    identity_type: Literal["SystemAssigned", "UserAssigned"] = "SystemAssigned"
    resource: str = ""
    identity_resource_id: str | None = None
    format: str | None = None


@dataclass(frozen=True, slots=True)
class FakeSdkEgressHeaderValueRef:
    secret_ref: FakeSdkEgressSecretRef | None = None
    managed_identity_ref: FakeSdkEgressManagedIdentityRef | None = None


@dataclass(frozen=True, slots=True)
class FakeSdkEgressHeader:
    operation: Literal["Set", "Insert", "Remove"] = "Set"
    name: str = ""
    value: str | None = None
    value_ref: FakeSdkEgressHeaderValueRef | None = None


@dataclass(frozen=True, slots=True)
class FakeSdkEgressRuleAction:
    type: Literal["Allow", "Deny", "Transform", "Rewrite"] = "Allow"
    host: str | None = None
    path: str | None = None
    scheme: str | None = None
    headers: list[FakeSdkEgressHeader] | None = None


@dataclass(frozen=True, slots=True)
class FakeSdkEgressRule:
    name: str | None = None
    match: FakeSdkEgressRuleMatch | None = None
    action: FakeSdkEgressRuleAction | None = None


@dataclass(frozen=True, slots=True)
class FakeSdkAutoSuspendPolicy:
    enabled: bool = False
    interval: int | None = None
    mode: Literal["Memory", "Disk"] | None = None


@dataclass(frozen=True, slots=True)
class FakeSdkAutoDeletePolicy:
    enabled: bool = False
    delete_interval_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class FakeSdkLifecyclePolicy:
    auto_suspend: FakeSdkAutoSuspendPolicy | None = None
    auto_delete: FakeSdkAutoDeletePolicy | None = None


@dataclass(frozen=True, slots=True)
class FakeSdkPortAuthEntraId:
    enabled: bool = False
    emails: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class FakeSdkPortAuthConfig:
    anonymous: bool | None = None
    entra_id: FakeSdkPortAuthEntraId | None = None


@dataclass(frozen=True, slots=True)
class FakeSdkPortIpAccessControlRule:
    name: str = ""
    action: Literal["Allow", "Deny"] = "Allow"
    priority: int = 0
    source_cidrs: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class FakeSdkPortIpAccessControl:
    default_action: Literal["Allow", "Deny"]
    rules: list[FakeSdkPortIpAccessControlRule] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class FakeSdkAddPortRequest:
    port: int = 0
    auth: FakeSdkPortAuthConfig | None = None
    protocol: Literal["Http", "Http2"] | None = None
    activation_mode: Literal["Manual", "OnDemand"] | None = None
    ip_access_control: FakeSdkPortIpAccessControl | None = None


@dataclass(frozen=True, slots=True)
class FakeSdkSandboxVolume:
    volume_name: str = ""
    mountpoint: str = ""
    read_only: bool | None = None


@dataclass(frozen=True, slots=True)
class FakeSdkFileInfo:
    """Mirrors the preview SDK's ``FileInfo`` response shape.

    ``mode`` mirrors the integer POSIX value sent by the service.
    """

    name: str = ""
    path: str = ""
    size: int | None = None
    is_directory: bool = False
    modified_at: str | None = None
    mode: str | None = None


@dataclass(frozen=True, slots=True)
class FakeSdkDirListing:
    """Mirrors the preview SDK's ``DirListing`` response shape."""

    path: str = ""
    entries: list[FakeSdkFileInfo] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class FakeSdkExecResult:
    """Mirrors the preview SDK's ``ExecResult`` response shape."""

    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True, slots=True)
class FakeSdkSandboxSummary:
    """Mirrors the subset of the preview SDK's ``Sandbox`` response used here."""

    id: str = ""
    state: Literal[
        "Running", "Stopped", "Suspended", "Resuming", "Stopping", "Creating", "Deleting"
    ] | None = None
    labels: dict[str, str] = field(default_factory=dict)
    lifecycle: FakeSdkLifecyclePolicy | None = None
    created_at: str | None = None


@dataclass(frozen=True, slots=True)
class FakeSdkSnapshotGpu:
    sku: str = ""
    quantity: int = 0


@dataclass(frozen=True, slots=True)
class FakeSdkSnapshotResources:
    cpu: str = ""
    memory: str = ""
    disk: str | None = None
    gpu: FakeSdkSnapshotGpu | None = None


@dataclass(frozen=True, slots=True)
class FakeSdkSnapshot:
    id: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    sandbox_id: str | None = None
    status: str | None = None
    vmm_type: str | None = None
    created_at_utc: str | None = None
    resources: FakeSdkSnapshotResources | None = None


class FakeCredential:
    """Controller-only credential double."""

    def __init__(self) -> None:
        self.closed = False
        self.token_scopes: list[str] = []

    async def get_token(self, scope: str) -> AccessToken:
        self.token_scopes.append(scope)
        return AccessToken(token="test-token", expires_on=0)

    async def close(self) -> None:
        self.closed = True


async def _as_sdk_response[T](operation: Awaitable[T]) -> T:
    """Re-raise a fake-transport not-found as the real SDK's wire-level shape."""

    try:
        return await operation
    except SandboxFileNotFoundError:
        raise ResourceNotFoundError(message="Not Found") from None


class FakeSdkSandboxClient:
    """A direct-file SDK-client stand-in with advisory ``get`` intentionally forbidden.

    File operations translate this repository's own
    :class:`SandboxFileNotFoundError` (raised by the underlying
    :class:`FakeSandboxTransport` store) into ``azure.core``'s
    :class:`ResourceNotFoundError`, mirroring the real preview SDK's wire-level
    404 so ``transport/aca_sdk.py``'s own translation layer has a real SDK
    exception shape to translate from, not this repository's own type.
    """

    def __init__(self, sandbox_id: str, *, labels: dict[str, str] | None = None) -> None:
        self.sandbox_id = sandbox_id
        self.labels = dict(labels or {})
        self.transport = FakeSandboxTransport()
        self.calls = self.transport.calls
        self.closed = False
        self.deleted = False
        self.stop_kwargs: dict[str, object] | None = None
        self.delete_kwargs: dict[str, object] | None = None
        self.lifecycle_policy = FakeSdkLifecyclePolicy(
            auto_suspend=FakeSdkAutoSuspendPolicy(enabled=True, interval=300, mode="Disk"),
            auto_delete=FakeSdkAutoDeletePolicy(enabled=True, delete_interval_seconds=90_300),
        )

    async def list_files(
        self,
        path: str = "/",
        *,
        container_name: str | None = None,
        **kwargs: object,
    ) -> FakeSdkDirListing:
        del container_name, kwargs
        entries = await _as_sdk_response(self.transport.list_files(path))
        return FakeSdkDirListing(
            path=path,
            entries=[
                FakeSdkFileInfo(
                    name=entry.name,
                    path=entry.path,
                    size=entry.size,
                    is_directory=entry.is_directory,
                    modified_at=entry.modified_at,
                    mode=entry.mode,
                )
                for entry in entries
            ],
        )

    async def stat_file(
        self, path: str, *, container_name: str | None = None, **kwargs: object
    ) -> FakeSdkFileInfo:
        del container_name, kwargs
        stat = await _as_sdk_response(self.transport.stat_file(path))
        return FakeSdkFileInfo(
            path=stat.path,
            size=stat.size,
            is_directory=stat.is_directory,
            modified_at=stat.modified_at,
            mode=stat.mode,
        )

    async def read_file(
        self, path: str, *, container_name: str | None = None, **kwargs: object
    ) -> bytes:
        del container_name, kwargs
        return await _as_sdk_response(self.transport.read_file(path))

    async def write_file(
        self,
        path: str,
        content: str | bytes,
        *,
        create_dirs: bool = True,
        mode: str | None = None,
        container_name: str | None = None,
        **kwargs: object,
    ) -> None:
        del mode, container_name, kwargs
        if isinstance(content, str):
            content = content.encode()
        await _as_sdk_response(self.transport.write_file(path, content, create_dirs=create_dirs))

    async def delete_file(
        self,
        path: str,
        *,
        recursive: bool = False,
        container_name: str | None = None,
        **kwargs: object,
    ) -> None:
        del container_name, kwargs
        if recursive:
            raise AssertionError("the adapter must not request recursive file deletion")
        await _as_sdk_response(self.transport.delete_file(path))

    async def mkdir(
        self, path: str, *, container_name: str | None = None, **kwargs: object
    ) -> None:
        del container_name, kwargs
        await _as_sdk_response(self.transport.mkdir(path))

    async def exec(
        self,
        command: str,
        *,
        working_directory: str | None = None,
        **kwargs: object,
    ) -> FakeSdkExecResult:
        del working_directory, kwargs
        result = await self.transport.exec(command)
        return FakeSdkExecResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
        )

    async def get(self, **kwargs: object) -> FakeSdkSandboxSummary:
        del kwargs
        return FakeSdkSandboxSummary(lifecycle=self.lifecycle_policy)

    async def begin_stop(
        self,
        *,
        polling_timeout: int = 180,
        polling_interval: int = 3,
        **kwargs: object,
    ) -> FakePoller[None]:
        self.stop_kwargs = {
            "polling_timeout": polling_timeout,
            "polling_interval": polling_interval,
            **kwargs,
        }
        self.calls.append(RecordedTransportCall("stop"))
        return FakePoller(None)

    async def resume(self, **kwargs: object) -> None:
        del kwargs
        self.calls.append(RecordedTransportCall("resume"))

    async def begin_delete(
        self,
        *,
        polling_timeout: int = 300,
        polling_interval: int = 3,
        **kwargs: object,
    ) -> FakePoller[None]:
        self.delete_kwargs = {
            "polling_timeout": polling_timeout,
            "polling_interval": polling_interval,
            **kwargs,
        }
        self.calls.append(RecordedTransportCall("delete"))
        return FakePoller(None, on_result=self._mark_deleted)

    async def set_lifecycle_policy(
        self, policy: FakeSdkLifecyclePolicy, **kwargs: object
    ) -> FakeSdkLifecyclePolicy:
        del kwargs
        self.lifecycle_policy = policy
        return policy

    async def close(self) -> None:
        self.closed = True

    def _mark_deleted(self) -> None:
        self.deleted = True


class FakePoller[T]:
    """The async poller returned by the preview client's create method."""

    def __init__(
        self,
        result: T,
        *,
        error: Exception | None = None,
        on_result: Callable[[], None] | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self._on_result = on_result

    async def result(self) -> T:
        if self._error is not None:
            raise self._error
        if self._on_result is not None:
            self._on_result()
        return self._result


class FakeSdkGroupClient:
    """Test-only group client that can create individual fake sandboxes."""

    def __init__(self, sandboxes: dict[str, FakeSdkSandboxClient], **kwargs: object) -> None:
        self.sandboxes = sandboxes
        self.constructor_kwargs = kwargs
        self.create_calls: list[dict[str, object]] = []
        self.deleted_sandbox_ids: list[str] = []
        self.create_result_error: Exception | None = None
        self.closed = False
        self.add_port_calls = 0
        self.snapshots: dict[str, FakeSdkSnapshot] = {}
        self.deleted_snapshot_ids: list[str] = []

    async def begin_create_sandbox(
        self,
        *,
        disk: str | None = "ubuntu",
        disk_id: str | None = None,
        snapshot_id: str | None = None,
        preset: str | None = None,
        cpu: str = "1000m",
        memory: str = "2048Mi",
        disk_size: str | None = None,
        auto_suspend_seconds: int = 300,
        auto_suspend_mode: Literal["Memory", "Disk"] = "Memory",
        labels: dict[str, str] | None = None,
        environment: dict[str, str] | None = None,
        connections: list[str] | None = None,
        egress_policy: FakeSdkEgressPolicy | None = None,
        volumes: list[FakeSdkSandboxVolume] | None = None,
        ports: list[FakeSdkAddPortRequest | int] | None = None,
        entrypoint: list[str] | None = None,
        cmd: list[str] | None = None,
        skip_egress_proxy: bool | None = None,
        customer_vnet_connection_name: str | None = None,
        vmm_type: str | None = None,
        polling_timeout: int = 300,
        polling_interval: int = 3,
        **kwargs: object,
    ) -> FakePoller[FakeSdkSandboxClient]:
        values = {
            "disk": disk,
            "disk_id": disk_id,
            "snapshot_id": snapshot_id,
            "preset": preset,
            "cpu": cpu,
            "memory": memory,
            "disk_size": disk_size,
            "auto_suspend_seconds": auto_suspend_seconds,
            "auto_suspend_mode": auto_suspend_mode,
            "labels": labels,
            "environment": environment,
            "connections": connections,
            "egress_policy": egress_policy,
            "volumes": volumes,
            "ports": ports,
            "entrypoint": entrypoint,
            "cmd": cmd,
            "skip_egress_proxy": skip_egress_proxy,
            "customer_vnet_connection_name": customer_vnet_connection_name,
            "vmm_type": vmm_type,
            "polling_timeout": polling_timeout,
            "polling_interval": polling_interval,
            **kwargs,
        }
        recorded = {key: value for key, value in values.items() if value is not None}
        if disk == "ubuntu" and (disk_id is not None or preset is not None):
            recorded.pop("disk")
        self.create_calls.append(recorded)
        sandbox = FakeSdkSandboxClient(
            f"created-{len(self.create_calls)}",
            labels=labels,
        )
        self.sandboxes[sandbox.sandbox_id] = sandbox
        return FakePoller(sandbox, error=self.create_result_error)

    def list_sandboxes(
        self,
        *,
        labels: dict[str, str] | None = None,
        **kwargs: object,
    ) -> AsyncIterator[FakeSdkSandboxSummary]:
        del kwargs
        async def iterate() -> AsyncIterator[FakeSdkSandboxSummary]:
            for sandbox in tuple(self.sandboxes.values()):
                if labels and any(sandbox.labels.get(key) != value for key, value in labels.items()):
                    continue
                yield FakeSdkSandboxSummary(
                    id=sandbox.sandbox_id,
                    state="Running",
                    labels=sandbox.labels,
                )

        return iterate()

    async def begin_delete_sandbox(
        self,
        sandbox_id: str,
        *,
        polling_timeout: int = 300,
        polling_interval: int = 3,
        **kwargs: object,
    ) -> FakePoller[None]:
        del polling_timeout, polling_interval, kwargs
        sandbox = self.sandboxes[sandbox_id]

        def delete() -> None:
            sandbox.deleted = True
            self.deleted_sandbox_ids.append(sandbox_id)
            del self.sandboxes[sandbox_id]

        return FakePoller(None, on_result=delete)

    def list_snapshots(self, **kwargs: object) -> AsyncIterator[FakeSdkSnapshot]:
        del kwargs
        async def iterate() -> AsyncIterator[FakeSdkSnapshot]:
            for snapshot in tuple(self.snapshots.values()):
                yield snapshot

        return iterate()

    async def begin_delete_snapshot(
        self,
        snapshot_id: str,
        *,
        polling_timeout: int = 300,
        polling_interval: int = 3,
        **kwargs: object,
    ) -> FakePoller[None]:
        del polling_timeout, polling_interval, kwargs

        def delete() -> None:
            self.snapshots.pop(snapshot_id, None)
            self.deleted_snapshot_ids.append(snapshot_id)

        return FakePoller(None, on_result=delete)

    async def close(self) -> None:
        self.closed = True

    def add_port(self) -> None:
        self.add_port_calls += 1
        raise AssertionError("the adapter must never add an inbound port")


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
            egress_host_rule=FakeSdkEgressHostRule,
            egress_rule=FakeSdkEgressRule,
            egress_rule_match=FakeSdkEgressRuleMatch,
            egress_rule_action=FakeSdkEgressRuleAction,
            egress_header=FakeSdkEgressHeader,
            egress_header_value_ref=FakeSdkEgressHeaderValueRef,
            egress_secret_ref=FakeSdkEgressSecretRef,
            lifecycle_policy=FakeSdkLifecyclePolicy,
            auto_suspend_policy=FakeSdkAutoSuspendPolicy,
            auto_delete_policy=FakeSdkAutoDeletePolicy,
        )

    def endpoint_for_region(self, region: str) -> str:
        self.endpoint_regions.append(region)
        return f"https://sandbox.{region}.invalid"

    def make_group_client(
        self,
        endpoint: str,
        credential: object,
        *,
        subscription_id: str,
        resource_group: str,
        sandbox_group: str,
        **kwargs: object,
    ) -> FakeSdkGroupClient:
        del endpoint, credential
        client = FakeSdkGroupClient(
            self.sandboxes,
            subscription_id=subscription_id,
            resource_group=resource_group,
            sandbox_group=sandbox_group,
            **kwargs,
        )
        self.group_clients.append(client)
        return client

    def make_sandbox_client(
        self,
        endpoint: str,
        credential: object,
        *,
        subscription_id: str,
        resource_group: str,
        sandbox_group: str,
        sandbox_id: str,
        **kwargs: object,
    ) -> FakeSdkSandboxClient:
        del endpoint, credential, subscription_id, resource_group, sandbox_group, kwargs
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
