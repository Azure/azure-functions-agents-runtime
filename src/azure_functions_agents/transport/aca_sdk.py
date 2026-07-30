"""The sole production adapter for the optional ACA Sandbox preview SDK.

Every preview-SDK symbol is deliberately confined to this module. Runtime code
outside this adapter sees only ``transport.models`` projections and the narrow
file/process Protocols.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from importlib import import_module
from typing import Any, cast

import aiohttp

from azure_functions_agents._credential import build_async_credential

from .manifest import (
    SESSION_MANIFEST_PATH,
    ExpectedSandboxManifestBinding,
    parse_sandbox_manifest_binding,
    verify_sandbox_manifest,
)
from .models import (
    AcaSandboxDependencyError,
    PersistedSandboxBinding,
    ProvisionedSandboxIdentity,
    SandboxCreateRequest,
    SandboxEgressPolicy,
    SandboxExecResult,
    SandboxFileEntry,
    SandboxFileStat,
    SandboxGroupBinding,
    SandboxGroupBindingError,
    SandboxGroupIdentity,
    SandboxProvisioningError,
    parse_sandbox_group_resource_id,
    source_to_provider_kwargs,
)
from .ports import SandboxFileTransport, SandboxProcessTransport

_ARM_SCOPE = "https://management.azure.com/.default"
_ARM_API_VERSION = "2026-02-01-preview"


@dataclass(frozen=True, slots=True)
class SdkFactories:
    """SDK constructors injected only at this adapter boundary for tests."""

    endpoint_for_region: Callable[[str], str]
    sandbox_group_client: Callable[..., Any]
    sandbox_client: Callable[..., Any]
    egress_policy: Callable[..., Any]


def _load_sdk_factories() -> SdkFactories:
    """Load the optional preview SDK only when ACA transport is constructed."""

    try:
        sdk_module = import_module("azure.containerapps.sandbox")
        async_sdk_module = import_module("azure.containerapps.sandbox.aio")
    except ImportError:
        raise AcaSandboxDependencyError(
            "ACA Sandbox support requires the aca_sandbox optional dependency."
        ) from None
    sdk = cast(Any, sdk_module)
    async_sdk = cast(Any, async_sdk_module)
    return SdkFactories(
        endpoint_for_region=sdk.endpoint_for_region,
        sandbox_group_client=async_sdk.SandboxGroupClient,
        sandbox_client=async_sdk.SandboxClient,
        egress_policy=sdk.EgressPolicy,
    )


_SDK_FACTORIES: Callable[[], SdkFactories] = _load_sdk_factories
_CREDENTIAL_FACTORY: Callable[[], Any] = build_async_credential


async def _read_arm_group(credential: Any, resource_id: str) -> Mapping[str, object]:
    """Resolve the customer-owned group identity and region under controller identity."""

    token = await credential.get_token(_ARM_SCOPE)
    access_token = getattr(token, "token", None)
    if not isinstance(access_token, str) or not access_token:
        raise SandboxGroupBindingError("Controller credential returned no ARM access token.")

    async with aiohttp.ClientSession() as session, session.get(
        f"https://management.azure.com{resource_id}",
        params={"api-version": _ARM_API_VERSION},
        headers={"Authorization": f"Bearer {access_token}"},
    ) as response:
        if response.status != 200:
            raise SandboxGroupBindingError(
                "Configured Sandbox Group could not be resolved under the controller credential."
            )
        payload = await response.json(content_type=None)
    if not isinstance(payload, dict):
        raise SandboxGroupBindingError("Configured Sandbox Group returned an invalid ARM response.")
    return cast(Mapping[str, object], payload)


_ARM_GROUP_READER: Callable[[Any, str], Awaitable[Mapping[str, object]]] = _read_arm_group


class AcaSandboxAdapter:
    """Binds one controller instance to one pre-provisioned customer Sandbox Group."""

    def __init__(
        self,
        *,
        group: SandboxGroupIdentity,
        credential: Any,
        group_client: Any,
        factories: SdkFactories,
    ) -> None:
        self._group = group
        self._credential = credential
        self._group_client = group_client
        self._factories = factories
        self._closed = False

    @property
    def group(self) -> SandboxGroupIdentity:
        """Return the ARM-validated, customer-owned group identity."""

        return self._group

    @classmethod
    async def open(
        cls,
        configured_group_resource_id: str,
        *,
        persisted_group: SandboxGroupBinding | None = None,
    ) -> AcaSandboxAdapter:
        """Resolve and bind exactly one existing Sandbox Group without mutating it."""

        configured = parse_sandbox_group_resource_id(configured_group_resource_id)
        factories = _SDK_FACTORIES()
        credential = _CREDENTIAL_FACTORY()
        group_client: Any | None = None
        succeeded = False
        try:
            arm_group = await _ARM_GROUP_READER(credential, configured.resource_id)
            resolved = _resolve_group_identity(configured.resource_id, arm_group)
            if persisted_group is not None:
                _verify_group_binding(persisted_group, resolved)

            group_client = factories.sandbox_group_client(
                factories.endpoint_for_region(resolved.region),
                credential,
                subscription_id=resolved.subscription_id,
                resource_group=resolved.resource_group,
                sandbox_group=resolved.group_name,
            )
            adapter = cls(
                group=resolved,
                credential=credential,
                group_client=group_client,
                factories=factories,
            )
            succeeded = True
            return adapter
        finally:
            if not succeeded:
                if group_client is not None:
                    await _close_resource(group_client)
                await _close_resource(credential)

    async def create(
        self,
        request: SandboxCreateRequest,
        *,
        persisted_group: SandboxGroupBinding,
    ) -> AcaSandboxHandle:
        """Create exactly one session sandbox under the bound customer group."""

        self._ensure_open()
        _verify_group_binding(persisted_group, self._group)
        egress = _compile_egress_policy(self._factories, request.egress_policy)
        poller = await self._group_client.begin_create_sandbox(
            **source_to_provider_kwargs(request.source),
            cpu=request.cpu,
            memory=request.memory,
            auto_suspend_seconds=request.auto_suspend_seconds,
            auto_suspend_mode=request.auto_suspend_mode,
            labels=request.labels.to_provider_labels(),
            environment=dict(request.environment),
            egress_policy=egress,
            ports=[],
            entrypoint=list(request.entrypoint),
            cmd=list(request.cmd),
            skip_egress_proxy=False,
            polling_timeout=request.provisioning_timeout_seconds,
            polling_interval=request.polling_interval_seconds,
        )
        sdk_client = await poller.result()
        return await self._make_handle(sdk_client)

    async def attach(
        self,
        persisted: PersistedSandboxBinding,
        expected: ExpectedSandboxManifestBinding,
    ) -> AcaSandboxHandle:
        """Attach by persisted ID, then prove readiness through a direct manifest read."""

        handle = await self._attach_handle(persisted, expected)
        await self._verify_manifest_handshake(handle, expected)
        return handle

    async def resume(
        self,
        persisted: PersistedSandboxBinding,
        expected: ExpectedSandboxManifestBinding,
    ) -> AcaSandboxHandle:
        """Resume by persisted ID and require the same data-plane manifest handshake."""

        handle = await self._attach_handle(persisted, expected)
        await handle.resume()
        await self._verify_manifest_handshake(handle, expected)
        return handle

    async def close(self) -> None:
        """Close controller-side clients and credentials without touching customer IaC."""

        if self._closed:
            return
        self._closed = True
        try:
            await _close_resource(self._group_client)
        finally:
            await _close_resource(self._credential)

    async def _attach_handle(
        self,
        persisted: PersistedSandboxBinding,
        expected: ExpectedSandboxManifestBinding,
    ) -> AcaSandboxHandle:
        self._ensure_open()
        _verify_group_binding(persisted.group, self._group)
        if expected.sandbox_group_resource_id != persisted.group.resource_id:
            raise SandboxGroupBindingError(
                "Persisted Sandbox Group does not match the expected manifest binding."
            )
        if expected.sandbox_id != persisted.sandbox_id:
            raise SandboxGroupBindingError(
                "Persisted sandbox ID does not match the expected manifest binding."
            )

        sdk_client = self._factories.sandbox_client(
            self._factories.endpoint_for_region(self._group.region),
            self._credential,
            subscription_id=self._group.subscription_id,
            resource_group=self._group.resource_group,
            sandbox_group=self._group.group_name,
            sandbox_id=persisted.sandbox_id,
        )
        return await self._make_handle(sdk_client, expected_sandbox_id=persisted.sandbox_id)

    async def _make_handle(
        self, sdk_client: Any, *, expected_sandbox_id: str | None = None
    ) -> AcaSandboxHandle:
        sandbox_id = getattr(sdk_client, "sandbox_id", None)
        if not isinstance(sandbox_id, str) or not sandbox_id:
            await _close_resource(sdk_client)
            raise SandboxProvisioningError("Live Sandbox handle did not provide a sandbox ID.")
        if expected_sandbox_id is not None and sandbox_id != expected_sandbox_id:
            await _close_resource(sdk_client)
            raise SandboxGroupBindingError(
                "Live Sandbox handle ID does not match the persisted sandbox binding."
            )
        return AcaSandboxHandle(
            sdk_client=sdk_client,
            identity=ProvisionedSandboxIdentity(
                sandbox_id=sandbox_id,
                group_resource_id=self._group.resource_id,
                region=self._group.region,
            ),
        )

    async def _verify_manifest_handshake(
        self, handle: AcaSandboxHandle, expected: ExpectedSandboxManifestBinding
    ) -> None:
        verified = False
        try:
            manifest_bytes = await handle.read_file(SESSION_MANIFEST_PATH)
            observed = parse_sandbox_manifest_binding(manifest_bytes)
            verify_sandbox_manifest(expected, observed, handle.identity)
            verified = True
        finally:
            if not verified:
                await handle.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise SandboxProvisioningError("ACA Sandbox adapter is closed.")


class AcaSandboxHandle(SandboxFileTransport, SandboxProcessTransport):
    """A live individual sandbox with direct file and separate process operations."""

    def __init__(self, *, sdk_client: Any, identity: ProvisionedSandboxIdentity) -> None:
        self._sdk_client = sdk_client
        self._identity = identity
        self._closed = False

    @property
    def identity(self) -> ProvisionedSandboxIdentity:
        """Return the provider handle identity without exposing provider types."""

        return self._identity

    async def list_files(self, path: str) -> tuple[SandboxFileEntry, ...]:
        self._ensure_open()
        listing = await self._sdk_client.list_files(path)
        entries = getattr(listing, "entries", None)
        if not isinstance(entries, list):
            raise SandboxProvisioningError("Sandbox file listing response was invalid.")
        return tuple(_project_file_entry(entry) for entry in entries)

    async def stat_file(self, path: str) -> SandboxFileStat:
        self._ensure_open()
        entry = await self._sdk_client.stat_file(path)
        return _project_file_stat(entry)

    async def read_file(self, path: str) -> bytes:
        self._ensure_open()
        content = await self._sdk_client.read_file(path)
        if not isinstance(content, bytes):
            raise SandboxProvisioningError("Sandbox file read response was not bytes.")
        return content

    async def write_file(self, path: str, content: bytes, *, create_dirs: bool = False) -> None:
        self._ensure_open()
        if not isinstance(content, bytes):
            raise TypeError("Sandbox file content must be bytes.")
        await self._sdk_client.write_file(path, content, create_dirs=create_dirs)

    async def delete_file(self, path: str) -> None:
        self._ensure_open()
        await self._sdk_client.delete_file(path, recursive=False)

    async def mkdir(self, path: str) -> None:
        self._ensure_open()
        await self._sdk_client.mkdir(path)

    async def exec(
        self, command: str, *, timeout_seconds: float | None = None
    ) -> SandboxExecResult:
        self._ensure_open()
        if not isinstance(command, str) or not command:
            raise ValueError("Sandbox process command must be a non-empty string.")
        if timeout_seconds is not None:
            if timeout_seconds <= 0:
                raise ValueError("Sandbox process timeout_seconds must be positive.")
            try:
                async with asyncio.timeout(timeout_seconds):
                    result = await self._sdk_client.exec(command)
            except TimeoutError:
                raise SandboxProvisioningError("Sandbox process execution timed out.") from None
        else:
            result = await self._sdk_client.exec(command)
        return _project_exec_result(result)

    async def stop(self) -> None:
        """Stop this individual sandbox; the group remains customer-owned."""

        self._ensure_open()
        await self._sdk_client.stop()

    async def resume(self) -> None:
        """Resume this individual sandbox without trusting advisory state reads."""

        self._ensure_open()
        await self._sdk_client.resume()

    async def delete(self) -> None:
        """Delete only this individual session sandbox."""

        self._ensure_open()
        await self._sdk_client.delete()

    async def close(self) -> None:
        """Close the live data-plane handle."""

        if self._closed:
            return
        self._closed = True
        await _close_resource(self._sdk_client)

    def _ensure_open(self) -> None:
        if self._closed:
            raise SandboxProvisioningError("ACA Sandbox handle is closed.")


def _resolve_group_identity(
    configured_resource_id: str, arm_group: Mapping[str, object]
) -> SandboxGroupIdentity:
    arm_resource_id = arm_group.get("id")
    arm_location = arm_group.get("location")
    if not isinstance(arm_resource_id, str) or not isinstance(arm_location, str):
        raise SandboxGroupBindingError("Configured Sandbox Group ARM response was incomplete.")
    configured = parse_sandbox_group_resource_id(configured_resource_id)
    resolved = parse_sandbox_group_resource_id(arm_resource_id)
    if configured.resource_id != resolved.resource_id:
        raise SandboxGroupBindingError(
            "Configured Sandbox Group does not match the ARM-resolved resource identity."
        )
    region = arm_location.strip().casefold()
    if not region:
        raise SandboxGroupBindingError("Configured Sandbox Group ARM response had no region.")
    return SandboxGroupIdentity(
        resource_id=resolved.resource_id,
        subscription_id=resolved.subscription_id,
        resource_group=resolved.resource_group,
        group_name=resolved.group_name,
        region=region,
    )


def _verify_group_binding(
    persisted: SandboxGroupBinding, resolved: SandboxGroupIdentity
) -> None:
    if persisted.resource_id != resolved.resource_id:
        raise SandboxGroupBindingError(
            "Persisted Sandbox Group does not match the configured ARM resource identity."
        )
    if persisted.region != resolved.region:
        raise SandboxGroupBindingError(
            "Persisted Sandbox Group region does not match the ARM-resolved region."
        )


def _compile_egress_policy(factories: SdkFactories, policy: SandboxEgressPolicy) -> Any:
    """Create only an explicit Deny + inspected SDK policy."""

    if policy.default_action != "Deny" or policy.traffic_inspection not in {"Full", "Partial"}:
        raise SandboxProvisioningError("Sandbox egress policy is not fail-closed.")
    return factories.egress_policy(
        default_action="Deny",
        traffic_inspection=policy.traffic_inspection,
    )


async def _close_resource(resource: object) -> None:
    close = getattr(resource, "close", None)
    if callable(close):
        result = close()
        if inspect.isawaitable(result):
            await cast(Awaitable[object], result)


def _project_file_entry(value: object) -> SandboxFileEntry:
    name = _required_string_attribute(value, "name", "Sandbox file entry")
    path = _required_string_attribute(value, "path", "Sandbox file entry")
    size = _optional_size_attribute(value, "size", "Sandbox file entry")
    is_directory = _required_bool_attribute(value, "is_directory", "Sandbox file entry")
    modified_at = _optional_string_attribute(value, "modified_at", "Sandbox file entry")
    mode = _optional_string_attribute(value, "mode", "Sandbox file entry")
    return SandboxFileEntry(
        name=name,
        path=path,
        size=size,
        is_directory=is_directory,
        modified_at=modified_at,
        mode=mode,
    )


def _project_file_stat(value: object) -> SandboxFileStat:
    path = _required_string_attribute(value, "path", "Sandbox file stat")
    size = _optional_size_attribute(value, "size", "Sandbox file stat")
    is_directory = _required_bool_attribute(value, "is_directory", "Sandbox file stat")
    modified_at = _optional_string_attribute(value, "modified_at", "Sandbox file stat")
    mode = _optional_string_attribute(value, "mode", "Sandbox file stat")
    return SandboxFileStat(
        path=path,
        size=size,
        is_directory=is_directory,
        modified_at=modified_at,
        mode=mode,
    )


def _project_exec_result(value: object) -> SandboxExecResult:
    exit_code = getattr(value, "exit_code", None)
    stdout = getattr(value, "stdout", None)
    stderr = getattr(value, "stderr", None)
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise SandboxProvisioningError("Sandbox process response had an invalid exit code.")
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        raise SandboxProvisioningError("Sandbox process response had invalid output.")
    return SandboxExecResult(exit_code=exit_code, stdout=stdout, stderr=stderr)


def _required_string_attribute(value: object, attribute: str, response_name: str) -> str:
    result = getattr(value, attribute, None)
    if not isinstance(result, str) or not result:
        raise SandboxProvisioningError(f"{response_name} was invalid.")
    return result


def _optional_string_attribute(value: object, attribute: str, response_name: str) -> str | None:
    result = getattr(value, attribute, None)
    if result is not None and not isinstance(result, str):
        raise SandboxProvisioningError(f"{response_name} was invalid.")
    return result


def _optional_size_attribute(value: object, attribute: str, response_name: str) -> int | None:
    result = getattr(value, attribute, None)
    if result is not None and (not isinstance(result, int) or isinstance(result, bool) or result < 0):
        raise SandboxProvisioningError(f"{response_name} was invalid.")
    return result


def _required_bool_attribute(value: object, attribute: str, response_name: str) -> bool:
    result = getattr(value, attribute, None)
    if not isinstance(result, bool):
        raise SandboxProvisioningError(f"{response_name} was invalid.")
    return result
