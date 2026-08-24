"""The sole production adapter for the optional ACA Sandbox preview SDK.

Every preview-SDK symbol is deliberately confined to this module. Runtime code
outside this adapter sees only ``transport.transport_models`` projections and
the narrow file/process Protocols.
"""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Any, Protocol, cast

import aiohttp
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import (
    AzureError,
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
)
from azure.core.polling import AsyncLROPoller

from azure_functions_agents._credential import build_async_credential
from azure_functions_agents._logger import logger

from .manifest import (
    SESSION_MANIFEST_PATH,
    ExpectedSandboxManifestBinding,
    parse_sandbox_manifest_binding,
    verify_sandbox_manifest,
)
from .ports import SandboxFileTransport, SandboxProcessTransport
from .transport_models import (
    AcaSandboxDependencyError,
    PersistedSandboxBinding,
    ProvisionedSandboxIdentity,
    SandboxCapacityError,
    SandboxCreateOutcomeUnknownError,
    SandboxCreateRequest,
    SandboxEgressHeader,
    SandboxEgressHostRule,
    SandboxEgressPolicy,
    SandboxEgressRule,
    SandboxEgressRuleAction,
    SandboxEgressRuleMatch,
    SandboxEgressSecretRef,
    SandboxExecResult,
    SandboxFileEntry,
    SandboxFileNotFoundError,
    SandboxFileOperationError,
    SandboxFileStat,
    SandboxGroupAuthorizationError,
    SandboxGroupBinding,
    SandboxGroupBindingError,
    SandboxGroupIdentity,
    SandboxGroupTransientError,
    SandboxLifecyclePolicy,
    SandboxProvisioningError,
    SandboxSnapshot,
    SandboxSummary,
    parse_sandbox_group_resource_id,
    source_to_provider_kwargs,
)

if TYPE_CHECKING:
    # The optional preview SDK is imported for typing only. Every runtime use
    # goes through ``_load_sdk_factories()``'s lazy ``import_module()`` below,
    # so the default in-language-worker runtime never depends on this import.
    from azure.containerapps.sandbox import (
        AutoDeletePolicy,
        AutoSuspendPolicy,
        EgressHeader,
        EgressHeaderValueRef,
        EgressHostRule,
        EgressPolicy,
        EgressRule,
        EgressRuleAction,
        EgressRuleMatch,
        EgressSecretRef,
        ExecResult,
        FileInfo,
        LifecyclePolicy,
    )
    from azure.containerapps.sandbox.aio import SandboxClient, SandboxGroupClient

_ARM_HOST = "https://management.azure.com"
_ARM_SCOPE = f"{_ARM_HOST}/.default"
_ARM_API_VERSION = "2026-02-01-preview"
_ARM_REQUEST_TIMEOUT_SECONDS = 30
_ARM_AUTHORIZATION_STATUS_CODES = frozenset({401, 403})
_ARM_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_PROVISIONING_ATTEMPT_LABEL = "provisioning_attempt_id"
_OPERATION_LABEL = "operation_label"
_CONTROL_OPERATION_TIMEOUT_SECONDS = 30
_CONTROL_OPERATION_POLL_INTERVAL_SECONDS = 3
_FAILED_CREATE_LOOKUP_ATTEMPTS = 3
_FAILED_CREATE_LOOKUP_DELAY_SECONDS = 1.0
_ARM_GROUP_RETRY_ATTEMPTS = 3
_ARM_GROUP_RETRY_DELAY_SECONDS = 0.5
_MANIFEST_RETRY_INTERVAL_SECONDS = 0.5
_RETRYABLE_MANIFEST_STATUS_CODES = frozenset({409, 423, 425, 429, 500, 502, 503, 504})
_RECONCILIATION_ERRORS = (AzureError, TimeoutError, RuntimeError, ValueError)

# A module-level indirection so tests can patch just this adapter's retry
# delays instead of monkeypatching the process-wide ``asyncio`` module.
_sleep = asyncio.sleep


@dataclass(frozen=True, slots=True)
class SdkFactories:
    """SDK constructors injected only at this adapter boundary for tests."""

    endpoint_for_region: Callable[[str], str]
    sandbox_group_client: Callable[..., SandboxGroupClient]
    sandbox_client: Callable[..., SandboxClient]
    egress_policy: Callable[..., EgressPolicy]
    egress_host_rule: Callable[..., EgressHostRule]
    egress_rule: Callable[..., EgressRule]
    egress_rule_match: Callable[..., EgressRuleMatch]
    egress_rule_action: Callable[..., EgressRuleAction]
    egress_header: Callable[..., EgressHeader]
    egress_header_value_ref: Callable[..., EgressHeaderValueRef]
    egress_secret_ref: Callable[..., EgressSecretRef]
    lifecycle_policy: Callable[..., LifecyclePolicy]
    auto_suspend_policy: Callable[..., AutoSuspendPolicy]
    auto_delete_policy: Callable[..., AutoDeletePolicy]


def _load_sdk_factories() -> SdkFactories:
    """Load the optional preview SDK only when ACA transport is constructed."""

    try:
        sdk_module = import_module("azure.containerapps.sandbox")
        async_sdk_module = import_module("azure.containerapps.sandbox.aio")
    except ImportError:
        raise AcaSandboxDependencyError(
            "ACA Sandbox support requires the aca_sandbox optional dependency."
        ) from None
    # The optional SDK is loaded dynamically by name so the default runtime
    # carries no import-time dependency on it. This is the one necessary
    # boundary cast from an opaque ``ModuleType`` into this adapter's typed
    # factory bundle; every symbol pulled out of it below is a real SDK type.
    sdk = cast(Any, sdk_module)
    async_sdk = cast(Any, async_sdk_module)
    return SdkFactories(
        endpoint_for_region=sdk.endpoint_for_region,
        sandbox_group_client=async_sdk.SandboxGroupClient,
        sandbox_client=async_sdk.SandboxClient,
        egress_policy=sdk.EgressPolicy,
        egress_host_rule=sdk.EgressHostRule,
        egress_rule=sdk.EgressRule,
        egress_rule_match=sdk.EgressRuleMatch,
        egress_rule_action=sdk.EgressRuleAction,
        egress_header=sdk.EgressHeader,
        egress_header_value_ref=sdk.EgressHeaderValueRef,
        egress_secret_ref=sdk.EgressSecretRef,
        lifecycle_policy=sdk.LifecyclePolicy,
        auto_suspend_policy=sdk.AutoSuspendPolicy,
        auto_delete_policy=sdk.AutoDeletePolicy,
    )


def validate_aca_sandbox_dependency() -> None:
    """Validate the optional SDK import without constructing credentials or clients."""
    _SDK_FACTORIES()


_SDK_FACTORIES: Callable[[], SdkFactories] = _load_sdk_factories
_CREDENTIAL_FACTORY: Callable[[], AsyncTokenCredential] = build_async_credential


def _raise_for_arm_status(status: int) -> None:
    """Classify an ARM response status and raise the appropriate typed error."""
    if status in _ARM_AUTHORIZATION_STATUS_CODES:
        raise SandboxGroupAuthorizationError()
    if status in _ARM_RETRYABLE_STATUS_CODES:
        raise SandboxGroupTransientError(
            f"Sandbox Group ARM lookup received retryable status {status}."
        )
    if status != 200:
        raise SandboxGroupBindingError(
            f"Sandbox Group ARM lookup failed with status {status}."
        )


async def _read_arm_group(
    credential: AsyncTokenCredential, resource_id: str
) -> Mapping[str, object]:
    """Resolve the customer-owned group identity and region under controller identity."""

    token = await credential.get_token(_ARM_SCOPE)
    if not token.token:
        raise SandboxGroupBindingError("Controller credential returned no ARM access token.")

    timeout = aiohttp.ClientTimeout(total=_ARM_REQUEST_TIMEOUT_SECONDS)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(
                f"{_ARM_HOST}{resource_id}",
                params={"api-version": _ARM_API_VERSION},
                headers={"Authorization": f"Bearer {token.token}"},
            ) as response,
        ):
            _raise_for_arm_status(response.status)
            payload = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise SandboxGroupTransientError(
            "Sandbox Group ARM lookup failed due to a transient transport error."
        ) from exc
    except ValueError as exc:
        raise SandboxGroupBindingError(
            "Sandbox Group ARM lookup failed due to a decode error."
        ) from exc

    if not isinstance(payload, dict):
        raise SandboxGroupBindingError("Configured Sandbox Group returned an invalid ARM response.")
    return cast(Mapping[str, object], payload)


_ARM_GROUP_READER: Callable[[AsyncTokenCredential, str], Awaitable[Mapping[str, object]]] = (
    _read_arm_group
)


async def _read_arm_group_with_retry(
    credential: AsyncTokenCredential, resource_id: str
) -> Mapping[str, object]:
    """Absorb single-instance transient ARM blips with bounded backoff."""
    for attempt in range(_ARM_GROUP_RETRY_ATTEMPTS):
        try:
            return await _ARM_GROUP_READER(credential, resource_id)
        except SandboxGroupTransientError:
            if attempt + 1 >= _ARM_GROUP_RETRY_ATTEMPTS:
                raise
            logger.warning(
                "Transient ARM group resolution failure (attempt %d/%d), retrying.",
                attempt + 1,
                _ARM_GROUP_RETRY_ATTEMPTS,
            )
            await _sleep(_ARM_GROUP_RETRY_DELAY_SECONDS)
    raise SandboxGroupTransientError("Sandbox Group ARM lookup exhausted its retry budget.")


class AcaSandboxAdapter:
    """Binds one controller instance to one pre-provisioned customer Sandbox Group."""

    def __init__(
        self,
        *,
        group: SandboxGroupIdentity,
        credential: AsyncTokenCredential,
        group_client: SandboxGroupClient,
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
        group_client: SandboxGroupClient | None = None
        succeeded = False
        try:
            arm_group = await _read_arm_group_with_retry(credential, configured.resource_id)
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
        self, request: SandboxCreateRequest, *, persisted_group: SandboxGroupBinding
    ) -> AcaSandboxHandle:
        """Create exactly one session sandbox under the bound customer group."""

        self._ensure_open()
        _verify_group_binding(persisted_group, self._group)
        egress = _compile_egress_policy(self._factories, request.egress_policy)
        provisioning_attempt_id = request.labels.operation_label or uuid.uuid4().hex
        stable_attempt = request.labels.operation_label is not None
        if request.reconcile_only and not stable_attempt:
            raise SandboxProvisioningError(
                "Reconciliation-only provisioning requires a stable operation label."
            )
        if stable_attempt:
            existing = await self._find_failed_create_sandboxes(
                provisioning_attempt_id,
                label_key=_OPERATION_LABEL,
                expected_labels=request.labels.to_provider_labels(),
            )
            if len(existing) == 1:
                return await self._handle_for_sandbox_id(existing[0])
            if len(existing) > 1:
                raise SandboxProvisioningError(
                    "A durable provisioning operation matches multiple sandboxes."
                )
            if request.reconcile_only:
                raise SandboxCreateOutcomeUnknownError(
                    "Accepted sandbox creation could not be reconciled yet."
                )
        labels = {
            **request.labels.to_provider_labels(),
            (
                _OPERATION_LABEL if stable_attempt else _PROVISIONING_ATTEMPT_LABEL
            ): provisioning_attempt_id,
        }
        create_accepted = False
        try:
            poller = await self._begin_create_sandbox(
                request,
                labels=labels,
                egress=egress,
                provisioning_attempt_id=provisioning_attempt_id,
                cleanup_on_failure=not stable_attempt,
            )
            create_accepted = True
            return await self._await_create_result(
                poller,
                provisioning_attempt_id,
                cleanup_on_failure=not stable_attempt,
            )
        except SandboxGroupAuthorizationError:
            if stable_attempt and create_accepted:
                return await self._recover_stable_accepted_create(
                    provisioning_attempt_id,
                    request.labels.to_provider_labels(),
                )
            raise
        except (AzureError, TimeoutError, RuntimeError, SandboxProvisioningError):
            if stable_attempt and create_accepted:
                return await self._recover_stable_accepted_create(
                    provisioning_attempt_id,
                    request.labels.to_provider_labels(),
                )
            if stable_attempt:
                existing = await self._find_failed_create_sandboxes(
                    provisioning_attempt_id,
                    label_key=_OPERATION_LABEL,
                    expected_labels=request.labels.to_provider_labels(),
                )
                if len(existing) == 1:
                    return await self._handle_for_sandbox_id(existing[0])
            raise

    async def _recover_stable_accepted_create(
        self,
        provisioning_attempt_id: str,
        expected_labels: Mapping[str, str],
    ) -> AcaSandboxHandle:
        """Recover a labeled create whose accepted poll cannot be observed."""
        try:
            existing = await self._find_failed_create_sandboxes(
                provisioning_attempt_id,
                label_key=_OPERATION_LABEL,
                expected_labels=expected_labels,
            )
        except SandboxGroupAuthorizationError:
            raise SandboxCreateOutcomeUnknownError(
                "Accepted sandbox creation could not be reconciled yet."
            ) from None
        if len(existing) == 1:
            return await self._handle_for_sandbox_id(existing[0])
        if len(existing) > 1:
            raise SandboxProvisioningError(
                "A durable provisioning operation matches multiple sandboxes."
            )
        raise SandboxCreateOutcomeUnknownError(
            "Accepted sandbox creation could not be reconciled yet."
        )

    async def _begin_create_sandbox(
        self,
        request: SandboxCreateRequest,
        *,
        labels: dict[str, str],
        egress: EgressPolicy,
        provisioning_attempt_id: str,
        cleanup_on_failure: bool,
    ) -> AsyncLROPoller[SandboxClient]:
        """Start the create call, reconciling any partial create on failure."""

        # source_to_provider_kwargs() always yields exactly one of
        # disk/disk_id/preset (never e.g. connections/volumes); typing this
        # merge as dict[str, Any] keeps that single-key projection from being
        # checked against every unrelated str-typed keyword on the signature.
        source_kwargs: dict[str, Any] = source_to_provider_kwargs(request.source)
        try:
            poller: AsyncLROPoller[SandboxClient] = await self._group_client.begin_create_sandbox(
                **source_kwargs,
                cpu=request.cpu,
                memory=request.memory,
                auto_suspend_seconds=request.auto_suspend_seconds,
                auto_suspend_mode=request.auto_suspend_mode,
                labels=labels,
                environment=dict(request.environment),
                egress_policy=egress,
                ports=[],
                entrypoint=list(request.entrypoint),
                cmd=list(request.cmd),
                skip_egress_proxy=False,
                # The SDK's ``polling_timeout: int`` annotation is narrower than
                # its implementation, which only ever adds this value to a
                # monotonic clock reading (see ``_polling.py``); round our
                # fractional budget up so we never under-deliver it.
                polling_timeout=math.ceil(request.provisioning_timeout_seconds),
                polling_interval=request.polling_interval_seconds,
            )
        except HttpResponseError as exc:
            if _is_authorization_rejection(exc):
                raise SandboxGroupAuthorizationError() from None
            if _is_capacity_rejection(exc):
                if cleanup_on_failure:
                    await self._cleanup_failed_create(provisioning_attempt_id)
                raise SandboxCapacityError(
                    "Sandbox Group capacity is currently unavailable."
                ) from exc
            if _is_definitive_client_rejection(exc):
                raise
            if cleanup_on_failure:
                await self._cleanup_failed_create(provisioning_attempt_id)
            raise
        except asyncio.CancelledError:
            if cleanup_on_failure:
                await self._cleanup_after_cancelled_create(provisioning_attempt_id)
            raise
        except (AzureError, TimeoutError, RuntimeError, SandboxProvisioningError):
            if cleanup_on_failure:
                await self._cleanup_failed_create(provisioning_attempt_id)
            raise
        return poller

    async def _await_create_result(
        self,
        poller: AsyncLROPoller[SandboxClient],
        provisioning_attempt_id: str,
        *,
        cleanup_on_failure: bool,
    ) -> AcaSandboxHandle:
        """Await the poller, reconciling any partial create on failure."""

        try:
            sdk_client: SandboxClient = await poller.result()
        except asyncio.CancelledError:
            if cleanup_on_failure:
                await self._cleanup_after_cancelled_create(provisioning_attempt_id)
            raise
        except HttpResponseError as exc:
            if _is_authorization_rejection(exc):
                raise SandboxGroupAuthorizationError() from None
            if cleanup_on_failure:
                await self._cleanup_failed_create(provisioning_attempt_id)
            if _is_capacity_rejection(exc):
                raise SandboxCapacityError(
                    "Sandbox Group capacity is currently unavailable."
                ) from exc
            raise
        except (AzureError, TimeoutError, RuntimeError, SandboxProvisioningError):
            if cleanup_on_failure:
                await self._cleanup_failed_create(provisioning_attempt_id)
            raise
        return await self._make_handle(sdk_client)

    async def _cleanup_after_cancelled_create(self, provisioning_attempt_id: str) -> None:
        """Best-effort reconciliation for a create cancelled mid-flight.

        Shielded and log-only so a cleanup failure never masks the caller's
        original ``CancelledError``.
        """

        try:
            await asyncio.shield(self._cleanup_failed_create(provisioning_attempt_id))
        except (AzureError, TimeoutError, RuntimeError, SandboxProvisioningError):
            logger.exception(
                "ACA sandbox create was cancelled before cleanup could be confirmed; "
                "provisioning attempt %s requires reconciliation.",
                provisioning_attempt_id,
            )

    async def attach(
        self,
        persisted: PersistedSandboxBinding,
        expected: ExpectedSandboxManifestBinding,
        *,
        readiness_timeout_seconds: float,
    ) -> AcaSandboxHandle:
        """Attach by persisted ID, then prove readiness through a direct manifest read."""

        _validate_positive_finite_seconds(
            readiness_timeout_seconds,
            "readiness_timeout_seconds",
        )
        handle = await self._attach_handle(persisted, expected)
        try:
            await self._verify_manifest_handshake(
                handle,
                expected,
                readiness_timeout_seconds=readiness_timeout_seconds,
            )
        except SandboxFileOperationError as exc:
            if exc.status_code in _AUTHORIZATION_STATUS_CODES:
                raise SandboxGroupAuthorizationError() from None
            raise
        return handle

    async def resume(
        self,
        persisted: PersistedSandboxBinding,
        expected: ExpectedSandboxManifestBinding,
        *,
        readiness_timeout_seconds: float,
    ) -> AcaSandboxHandle:
        """Resume by persisted ID and require the same data-plane manifest handshake."""

        _validate_positive_finite_seconds(
            readiness_timeout_seconds,
            "readiness_timeout_seconds",
        )
        handle = await self._attach_handle(persisted, expected)
        resumed = False
        try:
            await handle.resume()
            resumed = True
        except HttpResponseError as exc:
            if _is_authorization_rejection(exc):
                raise SandboxGroupAuthorizationError() from None
            raise
        finally:
            if not resumed:
                await handle.close()
        try:
            await self._verify_manifest_handshake(
                handle,
                expected,
                readiness_timeout_seconds=readiness_timeout_seconds,
            )
        except SandboxFileOperationError as exc:
            if exc.status_code in _AUTHORIZATION_STATUS_CODES:
                raise SandboxGroupAuthorizationError() from None
            raise
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

    async def list_sandboxes(self, *, labels: dict[str, str]) -> tuple[SandboxSummary, ...]:
        """Project label-filtered platform inventory without leaking SDK summaries."""
        self._ensure_open()
        summaries: list[SandboxSummary] = []
        try:
            async for sandbox in self._group_client.list_sandboxes(labels=labels):
                summaries.append(
                    SandboxSummary.create(
                        sandbox_id=sandbox.id,
                        labels=dict(sandbox.labels),
                        state=sandbox.state,
                        created_at=_sdk_timestamp(sandbox.created_at),
                        modified_at=None,
                    )
                )
        except HttpResponseError as exc:
            if _is_authorization_rejection(exc):
                raise SandboxGroupAuthorizationError() from None
            raise
        return tuple(summaries)

    async def delete_sandbox(self, sandbox_id: str) -> None:
        """Delete one sandbox through the bound customer-owned group."""
        self._ensure_open()
        if not sandbox_id:
            raise SandboxProvisioningError("Sandbox ID must be non-empty.")
        try:
            poller = await self._group_client.begin_delete_sandbox(
                sandbox_id,
                polling_timeout=_CONTROL_OPERATION_TIMEOUT_SECONDS,
                polling_interval=_CONTROL_OPERATION_POLL_INTERVAL_SECONDS,
            )
            await poller.result()
        except ResourceNotFoundError as exc:
            raise SandboxProvisioningError("Sandbox delete found no target.") from exc
        except HttpResponseError as exc:
            if exc.status_code == 404:
                raise SandboxProvisioningError("Sandbox delete found no target.") from exc
            raise SandboxProvisioningError("Sandbox delete failed.") from exc
        except AzureError as exc:
            raise SandboxProvisioningError("Sandbox delete failed.") from exc

    async def list_snapshots(self) -> tuple[SandboxSnapshot, ...]:
        """Project snapshots so the reconciler can prune provider-retained storage."""
        self._ensure_open()
        snapshots: list[SandboxSnapshot] = []
        async for snapshot in self._group_client.list_snapshots():
            snapshots.append(
                SandboxSnapshot.create(
                    snapshot_id=snapshot.id,
                    sandbox_id=snapshot.sandbox_id,
                    created_at=_sdk_timestamp(snapshot.created_at_utc),
                )
            )
        return tuple(snapshots)

    async def delete_snapshot(self, snapshot_id: str) -> None:
        """Delete one unreferenced snapshot through the bound Sandbox Group."""
        self._ensure_open()
        if not snapshot_id:
            raise SandboxProvisioningError("Snapshot ID must be non-empty.")
        try:
            poller = await self._group_client.begin_delete_snapshot(
                snapshot_id,
                polling_timeout=_CONTROL_OPERATION_TIMEOUT_SECONDS,
                polling_interval=_CONTROL_OPERATION_POLL_INTERVAL_SECONDS,
            )
            await poller.result()
        except ResourceNotFoundError as exc:
            raise SandboxProvisioningError("Snapshot delete found no target.") from exc
        except HttpResponseError as exc:
            if exc.status_code == 404:
                raise SandboxProvisioningError("Snapshot delete found no target.") from exc
            raise SandboxProvisioningError("Snapshot delete failed.") from exc
        except AzureError as exc:
            raise SandboxProvisioningError("Snapshot delete failed.") from exc

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

    async def _handle_for_sandbox_id(self, sandbox_id: str) -> AcaSandboxHandle:
        self._ensure_open()
        sdk_client = self._factories.sandbox_client(
            self._factories.endpoint_for_region(self._group.region),
            self._credential,
            subscription_id=self._group.subscription_id,
            resource_group=self._group.resource_group,
            sandbox_group=self._group.group_name,
            sandbox_id=sandbox_id,
        )
        return await self._make_handle(sdk_client, expected_sandbox_id=sandbox_id)

    async def _make_handle(
        self, sdk_client: SandboxClient, *, expected_sandbox_id: str | None = None
    ) -> AcaSandboxHandle:
        sandbox_id = sdk_client.sandbox_id
        if expected_sandbox_id is not None and sandbox_id != expected_sandbox_id:
            await _close_resource(sdk_client)
            raise SandboxGroupBindingError(
                "Live Sandbox handle ID does not match the persisted sandbox binding."
            )
        return AcaSandboxHandle(
            sdk_client=sdk_client,
            identity=ProvisionedSandboxIdentity.create(
                sandbox_id=sandbox_id,
                group_resource_id=self._group.resource_id,
                region=self._group.region,
            ),
            factories=self._factories,
        )

    async def _verify_manifest_handshake(
        self,
        handle: AcaSandboxHandle,
        expected: ExpectedSandboxManifestBinding,
        *,
        readiness_timeout_seconds: float,
    ) -> None:
        verified = False
        try:
            manifest_bytes = await _read_manifest_when_ready(
                handle,
                readiness_timeout_seconds=readiness_timeout_seconds,
            )
            observed = parse_sandbox_manifest_binding(manifest_bytes)
            verify_sandbox_manifest(expected, observed, handle.identity)
            verified = True
        finally:
            if not verified:
                await handle.close()

    async def _cleanup_failed_create(self, provisioning_attempt_id: str) -> None:
        """Delete any sandbox created by a failed create attempt, by its private label.

        A successful list that finds no matches confirms nothing was created,
        so this returns normally and lets the caller's original failure
        propagate unmasked. Only a list/delete call that itself fails raises,
        because then reconciliation could not be confirmed either way.
        """

        try:
            sandbox_ids = await self._find_failed_create_sandboxes(provisioning_attempt_id)
        except _RECONCILIATION_ERRORS as exc:
            raise SandboxProvisioningError(
                "Failed sandbox creation could not be reconciled for cleanup."
            ) from exc

        if not sandbox_ids:
            logger.info(
                "No sandbox found for cancelled/failed provisioning attempt %s; "
                "nothing to clean up.",
                provisioning_attempt_id,
            )
            return

        try:
            await self._delete_reconciled_sandboxes(sandbox_ids)
        except _RECONCILIATION_ERRORS as exc:
            raise SandboxProvisioningError(
                "Failed sandbox creation could not be reconciled for cleanup."
            ) from exc

    async def _find_failed_create_sandboxes(
        self,
        provisioning_attempt_id: str,
        *,
        label_key: str = _PROVISIONING_ATTEMPT_LABEL,
        expected_labels: Mapping[str, str] | None = None,
    ) -> list[str]:
        """List, with bounded retries, the sandbox(es) tagged by one create attempt."""

        labels = {label_key: provisioning_attempt_id}
        for attempt in range(_FAILED_CREATE_LOOKUP_ATTEMPTS):
            try:
                summaries = [
                    sandbox async for sandbox in self._group_client.list_sandboxes(labels=labels)
                ]
            except HttpResponseError as exc:
                if _is_authorization_rejection(exc):
                    raise SandboxGroupAuthorizationError() from None
                raise
            if summaries:
                if expected_labels is not None and any(
                    dict(summary.labels) != dict(expected_labels)
                    for summary in summaries
                ):
                    raise SandboxProvisioningError(
                        "Provisioning label collision cannot be safely reused."
                    )
                return [summary.id for summary in summaries]
            if attempt + 1 < _FAILED_CREATE_LOOKUP_ATTEMPTS:
                await _sleep(_FAILED_CREATE_LOOKUP_DELAY_SECONDS)
        return []

    async def _delete_reconciled_sandboxes(self, sandbox_ids: list[str]) -> None:
        for sandbox_id in sandbox_ids:
            poller = await self._group_client.begin_delete_sandbox(
                sandbox_id,
                polling_timeout=_CONTROL_OPERATION_TIMEOUT_SECONDS,
                polling_interval=_CONTROL_OPERATION_POLL_INTERVAL_SECONDS,
            )
            await poller.result()

    def _ensure_open(self) -> None:
        if self._closed:
            raise SandboxProvisioningError("ACA Sandbox adapter is closed.")


async def _translate_file_errors[T](operation: Awaitable[T]) -> T:
    """Translate preview-SDK file-operation exceptions to runtime-owned types.

    Keeps every preview-SDK exception type confined to this module: code
    outside ``aca_sdk.py`` only ever sees :class:`SandboxFileNotFoundError` or
    :class:`SandboxFileOperationError` from a ``SandboxFileTransport`` call.
    """

    try:
        return await operation
    except ResourceNotFoundError:
        raise SandboxFileNotFoundError(
            "Sandbox file operation found no entry at the requested path."
        ) from None
    except HttpResponseError as exc:
        raise SandboxFileOperationError(
            "Sandbox file operation failed.", status_code=exc.status_code
        ) from None
    except (ServiceRequestError, AzureError):
        raise SandboxFileOperationError("Sandbox file operation failed.") from None


class AcaSandboxHandle(SandboxFileTransport, SandboxProcessTransport):
    """A live individual sandbox with direct file and separate process operations."""

    def __init__(
        self,
        *,
        sdk_client: SandboxClient,
        identity: ProvisionedSandboxIdentity,
        factories: SdkFactories | None = None,
    ) -> None:
        self._sdk_client = sdk_client
        self._identity = identity
        self._factories = factories
        self._closed = False

    @property
    def identity(self) -> ProvisionedSandboxIdentity:
        """Return the provider handle identity without exposing provider types."""

        return self._identity

    async def list_files(self, path: str) -> tuple[SandboxFileEntry, ...]:
        self._ensure_open()
        listing = await _translate_file_errors(self._sdk_client.list_files(path))
        return tuple(_project_file_entry(entry) for entry in listing.entries)

    async def stat_file(self, path: str) -> SandboxFileStat:
        self._ensure_open()
        entry = await _translate_file_errors(self._sdk_client.stat_file(path))
        return _project_file_stat(entry)

    async def read_file(self, path: str) -> bytes:
        self._ensure_open()
        content: bytes = await _translate_file_errors(self._sdk_client.read_file(path))
        return content

    async def write_file(self, path: str, content: bytes, *, create_dirs: bool = False) -> None:
        self._ensure_open()
        await _translate_file_errors(
            self._sdk_client.write_file(path, content, create_dirs=create_dirs)
        )

    async def delete_file(self, path: str) -> None:
        self._ensure_open()
        await _translate_file_errors(self._sdk_client.delete_file(path, recursive=False))

    async def mkdir(self, path: str) -> None:
        self._ensure_open()
        await _translate_file_errors(self._sdk_client.mkdir(path))

    async def exec(
        self, command: str, *, timeout_seconds: float | None = None
    ) -> SandboxExecResult:
        self._ensure_open()
        if not command:
            raise ValueError("Sandbox process command must be a non-empty string.")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("Sandbox process timeout_seconds must be positive.")
        result = await self._exec_with_timeout(command, timeout_seconds)
        return _project_exec_result(result)

    async def _exec_with_timeout(self, command: str, timeout_seconds: float | None) -> ExecResult:
        if timeout_seconds is None:
            return await self._sdk_client.exec(command)
        try:
            async with asyncio.timeout(timeout_seconds):
                return await self._sdk_client.exec(command)
        except TimeoutError:
            raise SandboxProvisioningError("Sandbox process execution timed out.") from None

    async def stop(self) -> None:
        """Stop this individual sandbox; the group remains customer-owned."""

        self._ensure_open()
        poller = await self._sdk_client.begin_stop(
            polling_timeout=_CONTROL_OPERATION_TIMEOUT_SECONDS,
            polling_interval=_CONTROL_OPERATION_POLL_INTERVAL_SECONDS,
        )
        await poller.result()

    async def resume(self) -> None:
        """Resume this individual sandbox without trusting advisory state reads."""

        self._ensure_open()
        await self._sdk_client.resume()

    async def delete(self) -> None:
        """Delete only this individual session sandbox."""

        self._ensure_open()
        poller = await self._sdk_client.begin_delete(
            polling_timeout=_CONTROL_OPERATION_TIMEOUT_SECONDS,
            polling_interval=_CONTROL_OPERATION_POLL_INTERVAL_SECONDS,
        )
        await poller.result()

    async def get_lifecycle_policy(self) -> SandboxLifecyclePolicy:
        """Read the complete lifecycle projection from the individual sandbox."""
        self._ensure_open()
        sandbox = await self._sdk_client.get()
        if sandbox.lifecycle is None:
            raise SandboxProvisioningError("Sandbox lifecycle policy is unavailable.")
        return _project_lifecycle_policy(sandbox.lifecycle)

    async def set_lifecycle_policy(self, policy: SandboxLifecyclePolicy) -> None:
        """Set both auto-suspend and auto-delete together; never inherit group policy."""
        self._ensure_open()
        if self._factories is None:
            raise SandboxProvisioningError("ACA lifecycle factories are unavailable.")
        auto_suspend = (
            None
            if policy.auto_suspend_seconds is None
            else self._factories.auto_suspend_policy(
                enabled=True,
                interval=policy.auto_suspend_seconds,
                mode=policy.auto_suspend_mode,
            )
        )
        lifecycle = self._factories.lifecycle_policy(
            auto_suspend=auto_suspend,
            auto_delete=self._factories.auto_delete_policy(
                enabled=True,
                delete_interval_seconds=policy.auto_delete_seconds
            ),
        )
        await self._sdk_client.set_lifecycle_policy(lifecycle)

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
    # ``arm_group`` is a raw ARM REST JSON response, not an SDK type — these
    # isinstance checks parse genuinely untrusted wire data, the same
    # category as the sandbox manifest handshake (see ``manifest.py``).
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


def _verify_group_binding(persisted: SandboxGroupBinding, resolved: SandboxGroupIdentity) -> None:
    if persisted.resource_id != resolved.resource_id:
        raise SandboxGroupBindingError(
            "Persisted Sandbox Group does not match the configured ARM resource identity."
        )
    if persisted.region != resolved.region:
        raise SandboxGroupBindingError(
            "Persisted Sandbox Group region does not match the ARM-resolved region."
        )


def _compile_egress_policy(factories: SdkFactories, policy: SandboxEgressPolicy) -> EgressPolicy:
    """Translate the runtime-owned egress IR at the sole preview-SDK boundary."""

    if policy.default_action != "Deny" or policy.traffic_inspection != "Full":
        raise SandboxProvisioningError("Sandbox egress policy is not fail-closed.")
    return factories.egress_policy(
        default_action="Deny",
        traffic_inspection="Full",
        host_rules=[
            _compile_egress_host_rule(factories, rule)
            for rule in policy.host_rules
        ],
        rules=[_compile_egress_rule(factories, rule) for rule in policy.rules],
    )


def _compile_egress_host_rule(
    factories: SdkFactories, rule: SandboxEgressHostRule
) -> EgressHostRule:
    return factories.egress_host_rule(pattern=rule.host, action=rule.action)


def _compile_egress_rule(factories: SdkFactories, rule: SandboxEgressRule) -> EgressRule:
    return factories.egress_rule(
        name=rule.name,
        match=_compile_egress_rule_match(factories, rule.match),
        action=_compile_egress_rule_action(factories, rule.action),
    )


def _compile_egress_rule_match(
    factories: SdkFactories, match: SandboxEgressRuleMatch
) -> EgressRuleMatch:
    kwargs: dict[str, object] = {"host": match.host}
    if match.path is not None:
        kwargs["path"] = match.path
    if match.methods:
        kwargs["methods"] = list(match.methods)
    return factories.egress_rule_match(**kwargs)


def _compile_egress_rule_action(
    factories: SdkFactories, action: SandboxEgressRuleAction
) -> EgressRuleAction:
    kwargs: dict[str, object] = {"type": action.type}
    if action.host is not None:
        kwargs["host"] = action.host
    if action.path is not None:
        kwargs["path"] = action.path
    if action.scheme is not None:
        kwargs["scheme"] = action.scheme
    if action.headers:
        kwargs["headers"] = [
            _compile_egress_header(factories, header) for header in action.headers
        ]
    return factories.egress_rule_action(**kwargs)


def _compile_egress_header(
    factories: SdkFactories, header: SandboxEgressHeader
) -> EgressHeader:
    kwargs: dict[str, object] = {
        "operation": header.operation,
        "name": header.name,
    }
    if header.value is not None:
        kwargs["value"] = header.value
    elif header.secret_ref is not None:
        kwargs["value_ref"] = _compile_egress_secret_ref(factories, header.secret_ref)
    return factories.egress_header(**kwargs)


def _compile_egress_secret_ref(
    factories: SdkFactories, secret_ref: SandboxEgressSecretRef
) -> EgressHeaderValueRef:
    provider_secret_ref = factories.egress_secret_ref(
        secret_id=secret_ref.secret_id,
        secret_key=secret_ref.secret_key,
        format=secret_ref.format,
    )
    return factories.egress_header_value_ref(secret_ref=provider_secret_ref)


def _project_lifecycle_policy(policy: LifecyclePolicy) -> SandboxLifecyclePolicy:
    """Project the SDK lifecycle response while keeping its shape adapter-local."""
    auto_delete = policy.auto_delete
    if (
        auto_delete is None
        or not auto_delete.enabled
        or auto_delete.delete_interval_seconds is None
    ):
        raise SandboxProvisioningError("Sandbox lifecycle policy is incomplete.")
    if policy.auto_suspend is None or not policy.auto_suspend.enabled:
        return SandboxLifecyclePolicy.create(
            auto_suspend_seconds=None,
            auto_delete_seconds=auto_delete.delete_interval_seconds,
        )
    if policy.auto_suspend.interval is None:
        raise SandboxProvisioningError("Sandbox lifecycle policy is incomplete.")
    if policy.auto_suspend.mode is None:
        raise SandboxProvisioningError("Sandbox lifecycle policy is incomplete.")
    return SandboxLifecyclePolicy.create(
        auto_suspend_seconds=policy.auto_suspend.interval,
        auto_suspend_mode=policy.auto_suspend.mode,
        auto_delete_seconds=auto_delete.delete_interval_seconds,
    )


def _sdk_timestamp(timestamp: str | None) -> str | None:
    return timestamp


def _is_definitive_client_rejection(exc: HttpResponseError) -> bool:
    """A 4xx create rejection is definitive: the request never created a sandbox."""

    status_code = exc.status_code
    return status_code is not None and 400 <= status_code < 500


_AUTHORIZATION_STATUS_CODES = frozenset({401, 403})


def _is_authorization_rejection(exc: HttpResponseError) -> bool:
    return exc.status_code in _AUTHORIZATION_STATUS_CODES


def _is_capacity_rejection(exc: HttpResponseError) -> bool:
    return exc.status_code in {409, 429, 503}


async def _read_manifest_when_ready(
    handle: AcaSandboxHandle,
    *,
    readiness_timeout_seconds: float,
) -> bytes:
    deadline = time.monotonic() + readiness_timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SandboxProvisioningError("Sandbox manifest readiness timed out.")
        manifest_bytes = await _try_read_manifest(handle, remaining)
        if manifest_bytes is not None:
            return manifest_bytes

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SandboxProvisioningError("Sandbox manifest readiness timed out.")
        await _sleep(min(_MANIFEST_RETRY_INTERVAL_SECONDS, remaining))


async def _try_read_manifest(handle: AcaSandboxHandle, timeout_seconds: float) -> bytes | None:
    """Attempt one direct manifest read; return ``None`` only for retryable failures."""

    try:
        async with asyncio.timeout(timeout_seconds):
            return await handle.read_file(SESSION_MANIFEST_PATH)
    except SandboxFileNotFoundError:
        return None
    except SandboxFileOperationError as exc:
        if exc.status_code is not None and exc.status_code not in _RETRYABLE_MANIFEST_STATUS_CODES:
            raise
        return None
    except TimeoutError:
        raise SandboxProvisioningError("Sandbox manifest readiness timed out.") from None


def _validate_positive_finite_seconds(value: float, field_name: str) -> None:
    if not math.isfinite(value) or value <= 0:
        raise SandboxProvisioningError(f"Sandbox {field_name} must be positive and finite.")


class _AsyncCloseable(Protocol):
    """The minimal shape shared by the credential and every SDK client we hold."""

    async def close(self) -> None: ...


async def _close_resource(resource: _AsyncCloseable) -> None:
    await resource.close()


def _project_file_entry(entry: FileInfo) -> SandboxFileEntry:
    return SandboxFileEntry(
        name=entry.name,
        path=entry.path,
        size=entry.size,
        is_directory=entry.is_directory,
        modified_at=entry.modified_at,
        mode=entry.mode,
    )


def _project_file_stat(entry: FileInfo) -> SandboxFileStat:
    return SandboxFileStat(
        path=entry.path,
        size=entry.size,
        is_directory=entry.is_directory,
        modified_at=entry.modified_at,
        mode=entry.mode,
    )


def _project_exec_result(result: ExecResult) -> SandboxExecResult:
    return SandboxExecResult(exit_code=result.exit_code, stdout=result.stdout, stderr=result.stderr)
