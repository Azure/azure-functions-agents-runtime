"""Read-only observations and real reconciler support for deployed ACA lifecycle evidence."""

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

from azure_functions_agents.controller.reconciler import (
    ReconcilerConfig,
    ReconcileReport,
    SessionReconciler,
)
from azure_functions_agents.session_state import (
    AppIdentity,
    DurableSessionRecord,
    SessionStateContractError,
    compute_app_hash,
)
from azure_functions_agents.session_state.errors import SessionStateStoreError
from azure_functions_agents.session_state.store import AzureTableSessionStateStore
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
    """Live provider and Table clients bound to the deployed application."""

    adapter: AcaSandboxAdapter
    store: AzureTableSessionStateStore
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
    """Load the separate read-only and reconciler contract for the manual lifecycle test."""

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
        store=AzureTableSessionStateStore(service_client.get_table_client(config.table_name)),
        _service_client=service_client,
        _credential=credential,
    )


async def read_authoritative_session(
    resources: DeployedAcaLifecycleResources,
    *,
    session_id: str,
) -> DurableSessionRecord:
    """Read exactly one session row by row key; this helper never writes Table state."""

    try:
        page = await resources.store.query_entities(
            filter_expression=f"RowKey eq 'session:{session_id}'",
            top=2,
        )
    except SessionStateStoreError as exc:
        raise AcaSmokeEnvironmentError(
            "The lifecycle qualification could not read the configured session Table."
        ) from exc
    if len(page.entities) != 1:
        raise AssertionError("The authoritative Table must contain exactly one session row.")
    try:
        return DurableSessionRecord.from_table_entity(page.entities[0])
    except SessionStateContractError as exc:
        raise AssertionError("The authoritative session row violates the durable contract.") from exc


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

    due_at = session.expires_at + timedelta(seconds=ReconcilerConfig().safety_grace_seconds)
    remaining = (due_at - datetime.now(UTC)).total_seconds()
    if remaining > 0:
        await asyncio.sleep(remaining)


async def reconcile_owned_session(
    resources: DeployedAcaLifecycleResources,
    *,
    session: DurableSessionRecord,
    config: DeployedAcaLifecycleConfig,
) -> ReconcileReport:
    """Run the production reconciler on one known session with real Table and ACA adapters."""

    assert_session_belongs_to_deployment(session, config)
    reconciler = SessionReconciler(
        store=resources.store,
        provider=resources.adapter,
        app_hash=config.app_hash,
        reclaim_idle_seconds=LIFECYCLE_RECLAIM_IDLE_SECONDS,
    )
    try:
        return await reconciler.reconcile_session(session.owner_partition, session.session_id)
    except (SandboxTransportError, SessionStateStoreError) as exc:
        raise AcaSmokeEnvironmentError(
            "The explicit production reconciler could not reconcile the owned lifecycle session."
        ) from exc


async def cleanup_owned_lifecycle_session(
    resources: DeployedAcaLifecycleResources,
    *,
    session: DurableSessionRecord,
    config: DeployedAcaLifecycleConfig,
) -> None:
    """Use only the exact session labels, then reconcile the durable tombstone on test failure."""

    sandbox = await owned_sandbox(resources, session)
    if sandbox is not None:
        try:
            await resources.adapter.delete_sandbox(sandbox.sandbox_id)
        except SandboxTransportError as exc:
            raise AcaSmokeEnvironmentError(
                "The exact-label lifecycle cleanup could not delete the owned sandbox."
            ) from exc
    await reconcile_owned_session(resources, session=session, config=config)


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
