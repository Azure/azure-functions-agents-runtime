"""Read-only observations for deployed ACA lifecycle evidence."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from azure.core.exceptions import AzureError
from azure.identity.aio import DefaultAzureCredential
from tests.aca_smoke_diagnostics import AcaSmokeEnvironmentError
from tests.live.aca_deployed_agent_support import (
    DeployedAcaSmokeConfig,
    deployed_aca_smoke_config_from_environment,
)

from azure_functions_agents.sandbox_runtime_limits import RECLAIM_SAFETY_GRACE_SECONDS
from azure_functions_agents.session_state import (
    AppIdentity,
    DurableOwnerIdempotencyRecord,
    DurableRunRecord,
    DurableSessionOperation,
    DurableSessionRecord,
    OwnerIdempotencyRowKey,
    SessionStateContractError,
    compute_app_hash,
    hash_idempotency_key,
)
from azure_functions_agents.transport.aca_sdk import AcaSandboxAdapter
from azure_functions_agents.transport.transport_models import (
    SandboxSnapshot,
    SandboxSummary,
    SandboxTransportError,
)

_TABLE_SERVICE_URI_ENV = "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TABLE_SERVICE_URI"
_TABLE_NAME_ENV = "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_TABLE_NAME"
_GROUP_RESOURCE_ID_ENV = "AZURE_FUNCTIONS_AGENTS_ACA_SANDBOX_GROUP_RESOURCE_ID"
_APP_SUBSCRIPTION_ID_ENV = "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_APP_SUBSCRIPTION_ID"
_APP_SITE_NAME_ENV = "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_APP_SITE_NAME"
_APP_SLOT_NAME_ENV = "AZURE_FUNCTIONS_AGENTS_DEPLOYED_ACA_APP_SLOT_NAME"

LIFECYCLE_AUTO_SUSPEND_SECONDS = 60
LIFECYCLE_RECLAIM_IDLE_SECONDS = 120
LIFECYCLE_RECONCILER_CADENCE_SECONDS = 60
LIFECYCLE_RECLAIM_CONTROLLER_WINDOWS = 4
LIFECYCLE_RECLAIM_CONTROLLER_WAIT_SECONDS = (
    LIFECYCLE_RECONCILER_CADENCE_SECONDS * LIFECYCLE_RECLAIM_CONTROLLER_WINDOWS
)
_POLL_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class DeployedAcaLifecycleConfig:
    """Non-secret deployment details needed to observe one owned session."""

    deployed: DeployedAcaSmokeConfig
    table_service_uri: str
    table_name: str
    sandbox_group_resource_id: str
    app_identity: AppIdentity

    @property
    def app_hash(self) -> str:
        return compute_app_hash(self.app_identity)


@dataclass(slots=True)
class DeployedAcaLifecycleResources:
    """Live provider and read-only Table clients bound to the deployed application."""

    adapter: AcaSandboxAdapter
    table_client: Any
    _service_client: Any
    _credential: DefaultAzureCredential

    async def close(self) -> None:
        try:
            await self.adapter.close()
        finally:
            try:
                await self._service_client.close()
            finally:
                await self._credential.close()


def deployed_aca_lifecycle_config_from_environment() -> DeployedAcaLifecycleConfig:
    """Load the read-only observation contract for the manual lifecycle test."""

    deployed = deployed_aca_smoke_config_from_environment()
    table_service_uri = _table_service_uri(_required_value(_TABLE_SERVICE_URI_ENV))
    table_name = _required_value(_TABLE_NAME_ENV)
    sandbox_group_resource_id = _required_value(_GROUP_RESOURCE_ID_ENV)
    try:
        app_identity = AppIdentity.create(
            _required_value(_APP_SUBSCRIPTION_ID_ENV),
            _required_value(_APP_SITE_NAME_ENV),
            os.environ.get(_APP_SLOT_NAME_ENV),
        )
    except SessionStateContractError as exc:
        raise AcaSmokeEnvironmentError(
            "The deployed lifecycle app identity configuration is invalid."
        ) from exc
    return DeployedAcaLifecycleConfig(
        deployed=deployed,
        table_service_uri=table_service_uri,
        table_name=table_name,
        sandbox_group_resource_id=sandbox_group_resource_id,
        app_identity=app_identity,
    )


async def open_deployed_aca_lifecycle_resources(
    config: DeployedAcaLifecycleConfig,
) -> DeployedAcaLifecycleResources:
    """Open real, operator-authorized Table and ACA clients without creating infrastructure."""

    from azure.data.tables.aio import TableServiceClient

    credential = DefaultAzureCredential()
    service_client = TableServiceClient(endpoint=config.table_service_uri, credential=credential)
    try:
        adapter = await AcaSandboxAdapter.open(config.sandbox_group_resource_id)
    except (AzureError, SandboxTransportError) as exc:
        await service_client.close()
        await credential.close()
        raise AcaSmokeEnvironmentError(
            "The lifecycle qualification could not open the configured ACA Sandbox Group."
        ) from exc
    return DeployedAcaLifecycleResources(
        adapter=adapter,
        table_client=service_client.get_table_client(config.table_name),
        _service_client=service_client,
        _credential=credential,
    )


async def read_authoritative_session(
    resources: DeployedAcaLifecycleResources,
    *,
    session_id: str,
    partition_key: str | None = None,
) -> DurableSessionRecord:
    """Read exactly one session row; this helper never writes Table state."""
    entity = await _read_exact_entity(resources, f"session:{session_id}", partition_key)
    try:
        return DurableSessionRecord.from_table_entity(entity)
    except SessionStateContractError as exc:
        raise AssertionError("The authoritative session row violates the durable contract.") from exc


async def read_authoritative_run(
    resources: DeployedAcaLifecycleResources,
    *,
    session_id: str,
    run_id: str,
    partition_key: str | None = None,
) -> DurableRunRecord:
    """Read exactly one durable run row without modifying Table state."""
    entity = await _read_exact_entity(resources, f"run:{session_id}:{run_id}", partition_key)
    try:
        return DurableRunRecord.from_table_entity(entity)
    except SessionStateContractError as exc:
        raise AssertionError("The authoritative run row violates the durable contract.") from exc


async def read_owner_idempotency(
    resources: DeployedAcaLifecycleResources,
    *,
    partition_key: str,
    idempotency_key: str,
) -> DurableOwnerIdempotencyRecord | None:
    """Read one owner-scoped idempotency reservation without modifying Table state."""
    row_key = str(OwnerIdempotencyRowKey.create(hash_idempotency_key(idempotency_key)))
    entity = await _read_optional_exact_entity(resources, row_key, partition_key)
    if entity is None:
        return None
    try:
        return DurableOwnerIdempotencyRecord.from_table_entity(entity)
    except SessionStateContractError as exc:
        raise AssertionError(
            "The authoritative owner idempotency row violates the durable contract."
        ) from exc


async def read_session_operations(
    resources: DeployedAcaLifecycleResources,
    *,
    session_id: str,
    partition_key: str | None = None,
) -> tuple[DurableSessionOperation, ...]:
    """Read the known session's operation rows without modifying Table state."""
    prefix = f"operation:{session_id}:"
    upper_bound = f"{prefix}~"
    try:
        partition_filter = (
            ""
            if partition_key is None
            else f"PartitionKey eq '{_escape_odata_literal(partition_key)}' and "
        )
        entities = [
            entity
            async for entity in resources.table_client.query_entities(
                query_filter=(
                    f"{partition_filter}RowKey ge '{_escape_odata_literal(prefix)}' and "
                    f"RowKey lt '{_escape_odata_literal(upper_bound)}'"
                ),
                results_per_page=128,
            )
        ]
    except AzureError as exc:
        raise AcaSmokeEnvironmentError(
            "The load qualification could not read the configured operation Table rows."
        ) from exc
    try:
        return tuple(DurableSessionOperation.from_table_entity(entity) for entity in entities)
    except SessionStateContractError as exc:
        raise AssertionError("The authoritative operation row violates the durable contract.") from exc


async def _read_exact_entity(
    resources: DeployedAcaLifecycleResources,
    row_key: str,
    partition_key: str | None = None,
) -> dict[str, object]:
    try:
        entities = []
        partition_filter = (
            ""
            if partition_key is None
            else f"PartitionKey eq '{_escape_odata_literal(partition_key)}' and "
        )
        async for entity in resources.table_client.query_entities(
            query_filter=f"{partition_filter}RowKey eq '{_escape_odata_literal(row_key)}'",
            results_per_page=2,
        ):
            entities.append(entity)
            if len(entities) == 2:
                break
    except AzureError as exc:
        raise AcaSmokeEnvironmentError(
            "The lifecycle qualification could not read the configured session Table."
        ) from exc
    if len(entities) != 1:
        raise AssertionError("The authoritative Table must contain exactly one requested row.")
    return entities[0]


async def _read_optional_exact_entity(
    resources: DeployedAcaLifecycleResources,
    row_key: str,
    partition_key: str,
) -> dict[str, object] | None:
    try:
        entities = [
            entity
            async for entity in resources.table_client.query_entities(
                query_filter=(
                    f"PartitionKey eq '{_escape_odata_literal(partition_key)}' and "
                    f"RowKey eq '{_escape_odata_literal(row_key)}'"
                ),
                results_per_page=2,
            )
        ]
    except AzureError as exc:
        raise AcaSmokeEnvironmentError(
            "The lifecycle qualification could not read the configured owner idempotency row."
        ) from exc
    if len(entities) > 1:
        raise AssertionError("The authoritative Table returned duplicate owner idempotency rows.")
    return entities[0] if entities else None


def assert_session_belongs_to_deployment(
    session: DurableSessionRecord,
    config: DeployedAcaLifecycleConfig,
) -> None:
    """Reject an observation from another deployed app before touching its provider resources."""

    if session.owner_partition.app_hash != config.app_hash:
        raise AssertionError("The Table session does not belong to the configured deployed application.")


def session_labels(session: DurableSessionRecord) -> dict[str, str]:
    """Return the complete ownership selector used for exact-label cleanup."""

    partition = session.owner_partition
    return {
        "owner_hash_version": partition.owner_hash_version,
        "owner_kind": partition.owner_kind,
        "owner_hash": partition.owner_hash,
        "app_hash": partition.app_hash,
        "session_id": session.session_id,
    }


async def owned_sandbox(
    resources: DeployedAcaLifecycleResources,
    session: DurableSessionRecord,
) -> SandboxSummary | None:
    """Read one session's provider sandbox by its complete immutable ownership labels."""

    try:
        matches = await resources.adapter.list_sandboxes(labels=session_labels(session))
    except SandboxTransportError as exc:
        raise AcaSmokeEnvironmentError(
            "The lifecycle qualification could not read the ACA Sandbox Group inventory."
        ) from exc
    if len(matches) > 1:
        raise AssertionError("The provider reported multiple sandboxes with one exact session selector.")
    return matches[0] if matches else None


async def owned_snapshots(
    resources: DeployedAcaLifecycleResources,
    session: DurableSessionRecord,
) -> tuple[SandboxSnapshot, ...]:
    """Read the provider snapshots owned by the exact backing sandbox."""

    try:
        snapshots = await resources.adapter.list_snapshots()
    except SandboxTransportError as exc:
        raise AcaSmokeEnvironmentError(
            "The lifecycle qualification could not read ACA snapshot inventory."
        ) from exc
    return tuple(snapshot for snapshot in snapshots if snapshot.sandbox_id == session.sandbox_id)


async def wait_for_idle_session(
    resources: DeployedAcaLifecycleResources,
    *,
    session_id: str,
    timeout_seconds: float,
) -> DurableSessionRecord:
    """Wait for the public turn's own terminal path to arm reusable idle state."""

    async def condition() -> DurableSessionRecord | None:
        session = await read_authoritative_session(resources, session_id=session_id)
        if (
            session.status == "ready"
            and session.active_run_id is None
            and session.active_operation_id is None
            and session.idle_policy_armed
            and session.sandbox_id is not None
        ):
            return session
        return None

    return await _wait_for(condition, timeout_seconds, "The terminal run did not arm idle lifecycle.")


async def wait_for_suspended_sandbox(
    resources: DeployedAcaLifecycleResources,
    session: DurableSessionRecord,
    *,
    timeout_seconds: float,
) -> SandboxSummary:
    """Wait for ACA itself to report the backing sandbox stopped or suspended."""

    async def condition() -> SandboxSummary | None:
        sandbox = await owned_sandbox(resources, session)
        if (
            sandbox is not None
            and sandbox.sandbox_id == session.sandbox_id
            and sandbox.state in {"Stopped", "Suspended"}
        ):
            return sandbox
        return None

    return await _wait_for(
        condition,
        timeout_seconds,
        "ACA did not report the owned sandbox stopped or suspended after idle expiry.",
    )


async def wait_until_reclaim_due(session: DurableSessionRecord) -> None:
    """Wait only for the product's durable reclaim eligibility, not for the timer cadence."""

    due_at = session.expires_at + timedelta(seconds=RECLAIM_SAFETY_GRACE_SECONDS)
    remaining = (due_at - datetime.now(UTC)).total_seconds()
    if remaining > 0:
        await asyncio.sleep(remaining)


async def wait_for_reclaimed_session(
    resources: DeployedAcaLifecycleResources,
    *,
    session_id: str,
    timeout_seconds: float = LIFECYCLE_RECLAIM_CONTROLLER_WAIT_SECONDS,
    partition_key: str | None = None,
) -> DurableSessionRecord:
    """Observe the deployed timer's durable reclaim result over bounded cadence windows."""

    async def condition() -> DurableSessionRecord | None:
        session = await read_authoritative_session(
            resources,
            session_id=session_id,
            partition_key=partition_key,
        )
        if (
            session.status == "tombstoned"
            and session.tombstone_reason == "reclaimed_idle_session"
            and session.active_run_id is None
            and session.active_operation_id is None
        ):
            return session
        return None

    return await _wait_for(
        condition,
        timeout_seconds,
        "The deployed controller timer did not tombstone the idle session within "
        f"{LIFECYCLE_RECLAIM_CONTROLLER_WINDOWS} cadence windows.",
    )


async def cleanup_owned_lifecycle_session(
    resources: DeployedAcaLifecycleResources,
    *,
    session: DurableSessionRecord,
    config: DeployedAcaLifecycleConfig,
    partition_key: str | None = None,
) -> None:
    """Delete only exact-label backing, then require the deployed controller's tombstone."""

    assert_session_belongs_to_deployment(session, config)
    selector = _format_session_selector(session)
    try:
        sandbox = await owned_sandbox(resources, session)
        if sandbox is not None:
            await resources.adapter.delete_sandbox(sandbox.sandbox_id)
        await wait_for_reclaimed_session(
            resources,
            session_id=session.session_id,
            partition_key=partition_key,
        )
    except (AcaSmokeEnvironmentError, AssertionError, SandboxTransportError) as exc:
        raise AcaSmokeEnvironmentError(
            "ACA-SMOKE-ENV cleanup could not confirm the deployed controller tombstone for "
            f"exact session selector {selector}."
        ) from exc


async def _wait_for[T](
    condition: Callable[[], Awaitable[T | None]],
    timeout_seconds: float,
    failure_message: str,
) -> T:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        value = await condition()
        if value is not None:
            return value
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(failure_message)
        await asyncio.sleep(_POLL_SECONDS)


def _required_value(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise AcaSmokeEnvironmentError(f"{name} must be set to a non-blank value.")
    return value.strip()


def _escape_odata_literal(value: str) -> str:
    return value.replace("'", "''")


def _format_session_selector(session: DurableSessionRecord) -> str:
    return ", ".join(f"{name}={value}" for name, value in sorted(session_labels(session).items()))


def _table_service_uri(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path.rstrip("/")
        or parsed.query
        or parsed.fragment
    ):
        raise AcaSmokeEnvironmentError(
            f"{_TABLE_SERVICE_URI_ENV} must be an HTTPS Table service URL without a path or query."
        )
    return urlunsplit(("https", parsed.netloc, "", "", ""))
