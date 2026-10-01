"""Opt-in tests against a disposable, pre-existing Blob container (including Azurite)."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
import pytest_asyncio
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.storage.blob import StorageErrorCode
from azure.storage.blob.aio import BlobLeaseClient, BlobServiceClient

from azure_functions_agents import _copilot_session_fs as fs
from azure_functions_agents._copilot_providers import OpenAIProvider
from azure_functions_agents._harness import AppHarness, HarnessKind
from azure_functions_agents._native_session_identity import (
    LeaseLostError,
    SessionConflictError,
    StorageMode,
    StorageRoute,
    _open_owned_blob_service,
    opaque_key,
    state_name,
)
from tests._blob_sdk_errors import ScriptedBlobTransport

CONNECTION = "AZURE_FUNCTIONS_AGENTS_TEST_BLOB_CONNECTION_STRING"
SERVICE_URI = "AZURE_FUNCTIONS_AGENTS_TEST_BLOB_SERVICE_URI"
CONTAINER = "AZURE_FUNCTIONS_AGENTS_TEST_BLOB_CONTAINER"
OPT_IN = "AZURE_FUNCTIONS_AGENTS_TEST_DISPOSABLE_BLOB"
NATIVE_ID = "integration-native-id"


def integration_route(environ: Mapping[str, str], local_dir: Path) -> StorageRoute | None:
    """Return a fresh test-owned route, or ``None`` when real Blob tests are not opted in.

    Exactly one target is required: a connection string (Azurite/shared key) or an
    ``https`` Blob service URI authenticated with ``DefaultAzureCredential``.
    Raised messages never include the configured values.
    """
    container = environ.get(CONTAINER, "").strip()
    connection = environ.get(CONNECTION, "").strip()
    uri = environ.get(SERVICE_URI, "").strip()
    if environ.get(OPT_IN) != "1" or not container or not (connection or uri):
        return None
    if connection and uri:
        raise ValueError(f"Set exactly one of {CONNECTION} or {SERVICE_URI}.")
    if uri:
        parts = urlsplit(uri)
        if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment:
            raise ValueError(
                f"{SERVICE_URI} must be an https Blob service URI without a query (no SAS)."
            )
    return StorageRoute(
        mode=StorageMode.BLOB,
        app_key=opaque_key("a", "integration-" + uuid.uuid4().hex),
        local_dir=local_dir,
        container=container,
        identity_key=uuid.uuid4().hex,
        connection_string=connection or None,
        blob_uri=uri or None,
    )


def owned_blob(service, route: StorageRoute, name: str):
    """Return a client for one exact blob under this test run's random app prefix."""
    if not name.startswith(f"copilot-native/v1/{route.app_key}/"):
        raise AssertionError("Refusing to touch a blob outside this test run's prefix.")
    return service.get_blob_client(route.container, name)


async def break_owned_lease(service, route: StorageRoute, name: str) -> None:
    await BlobLeaseClient(owned_blob(service, route, name)).break_lease(lease_break_period=0)


async def delete_owned_blobs(service, route: StorageRoute, names) -> None:
    """Delete exactly ``names``; break only a still-held test lease, and surface other errors."""
    blobs = [(name, owned_blob(service, route, name)) for name in sorted(names)]
    errors: list[Exception] = []
    for name, blob in blobs:
        try:
            try:
                await blob.delete_blob(delete_snapshots="include")
            except ResourceNotFoundError:
                continue
            except HttpResponseError as exc:
                if exc.error_code != StorageErrorCode.LEASE_ID_MISSING:
                    raise
                # Failed owners intentionally keep their lease; break only this exact blob's.
                await break_owned_lease(service, route, name)
                await blob.delete_blob(delete_snapshots="include")
        except ResourceNotFoundError:
            continue
        except Exception as exc:
            exc.add_note(f"Leftover test blob: {name}")
            errors.append(exc)
    if errors:
        raise ExceptionGroup("Test-owned Blob cleanup failed.", errors)


@pytest_asyncio.fixture
async def blob_case(tmp_path):
    try:
        route = integration_route(os.environ, tmp_path / "unused-local")
    except ValueError as exc:
        pytest.fail(str(exc))
    if route is None:
        pytest.skip(
            f"Real Blob tests require {OPT_IN}=1, {CONTAINER} (a pre-existing disposable "
            f"container) and exactly one of {CONNECTION} (Azurite is supported) or "
            f"{SERVICE_URI} (Entra ID via DefaultAzureCredential)."
        )
    # Reuse the product's owned client so any Entra credential is closed with the service.
    owned = await _open_owned_blob_service(route)
    service = owned.service
    names: set[str] = set()
    try:
        # Never let NativeSession.open create a container in an unintended account.
        await service.get_container_client(route.container).get_container_properties()
        def harness(session_id: str) -> AppHarness:
            names.add(state_name(route, "agent", session_id))
            return AppHarness(
                HarnessKind.COPILOT, tmp_path, tmp_path / "preview", "model",
                OpenAIProvider("not-a-credential"), session_storage=route,
            )

        yield harness, service, route
    finally:
        try:
            await delete_owned_blobs(service, route, names)
        finally:
            await owned.close()


async def open_owner(harness, session_id="conversation", *, new=True, timeout=5):
    owner = await fs.NativeSession.open(
        harness(session_id), "agent", session_id, NATIVE_ID,
        asyncio.get_running_loop().time() + timeout,
    )
    if new:
        await owner.prepare(new_session=True)
    return owner


@pytest.mark.asyncio
async def test_real_blob_two_clients_exclude_same_owner(blob_case):
    harness, _, _ = blob_case
    owner = await open_owner(harness)
    try:
        with pytest.raises(SessionConflictError, match="busy"):
            await open_owner(harness, new=False, timeout=.3)
        independent = await open_owner(harness, session_id="independent")
        await independent.close()
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_real_blob_lease_break_blocks_stale_writer(blob_case):
    harness, service, route = blob_case
    owner = await open_owner(harness)
    try:
        await fs.NativeSessionFs(owner).write_file("/journal", "committed")
        await owner.transition(state=fs.SessionState.ACTIVE)
        await owner.complete()
    finally:
        await owner.close()

    stale = await open_owner(harness, new=False)
    try:
        await stale.prepare(new_session=False)
        await break_owned_lease(service, route, state_name(route, "agent", "conversation"))
        with pytest.raises(LeaseLostError):
            await fs.NativeSessionFs(stale).write_file("/journal", "stale")
        with pytest.raises(LeaseLostError):
            await stale.complete()
    finally:
        await stale.close()

    restored = await open_owner(harness, new=False)
    try:
        await restored.recover_preparing()
        assert restored.envelope.completed.files["/journal"].content == "committed"
    finally:
        await restored.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["write", "rename"])
async def test_real_blob_changed_etag_rejects_interrupted_mutation(blob_case, operation):
    harness, service, route = blob_case
    owner = await open_owner(harness)
    try:
        provider = fs.NativeSessionFs(owner)
        await provider.write_file("/old/deep/file", "before")
        baseline = fs.serialize(owner.envelope)
        revision = owner.envelope.revision
        name = state_name(route, "agent", "conversation")
        # A distinct client advances the ETag under the same lease, without changing content.
        # The original owner must not acknowledge its subsequent stale conditional Put.
        blob = owned_blob(service, route, name)
        await blob.upload_blob(baseline, overwrite=True, lease=owner.store.lease.id)
        with pytest.raises(LeaseLostError):
            if operation == "write":
                await provider.write_file("/old/deep/file", "after")
            else:
                await provider.rename("/old", "/new")
        assert owner.envelope.revision == revision
        with pytest.raises(LeaseLostError):
            await owner.check()
    finally:
        await owner.close()

    # Failed owners intentionally never release their lease; break this test-owned lease.
    await break_owned_lease(service, route, name)
    reopened = await open_owner(harness, new=False)
    try:
        assert reopened.envelope.owner_epoch > owner.envelope.owner_epoch
        assert reopened.envelope.state is fs.SessionState.PREPARING
        assert reopened.envelope.working.files["/old/deep/file"].content == "before"
        assert "/new/deep/file" not in reopened.envelope.working.files
        await reopened.recover_preparing()
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_real_blob_completed_tree_restores_from_new_client(blob_case):
    harness, _, _ = blob_case
    first = await open_owner(harness)
    try:
        await fs.NativeSessionFs(first).write_file("/sdk/journal.jsonl", "turn-1")
        await first.transition(state=fs.SessionState.ACTIVE)
        await first.complete()
        completed_revision = first.envelope.revision
    finally:
        await first.close()

    second = await open_owner(harness, new=False)
    try:
        assert not (second.route.local_dir / state_name(
            second.route, "agent", "conversation"
        )).exists()
        assert second.envelope.revision > completed_revision
        assert second.envelope.state is fs.SessionState.READY
        assert second.envelope.completed.files["/sdk/journal.jsonl"].content == "turn-1"
        await second.prepare(new_session=False)
        assert await fs.NativeSessionFs(second).read_file("/sdk/journal.jsonl") == "turn-1"
        await second.rollback()
    finally:
        await second.close()


def test_integration_route_requires_explicit_opt_in_and_target(tmp_path):
    base = {OPT_IN: "1", CONTAINER: "disposable", SERVICE_URI: "https://acct.blob.core.windows.net"}
    assert integration_route({}, tmp_path) is None
    assert integration_route({**base, OPT_IN: "true"}, tmp_path) is None
    assert integration_route({k: v for k, v in base.items() if k != CONTAINER}, tmp_path) is None
    assert integration_route({OPT_IN: "1", CONTAINER: "disposable"}, tmp_path) is None


def test_integration_route_selects_service_uri_with_default_credential(tmp_path):
    route = integration_route({
        OPT_IN: "1", CONTAINER: " disposable ",
        SERVICE_URI: " https://acct.blob.core.windows.net/ ",
    }, tmp_path)
    assert route is not None
    assert route.mode is StorageMode.BLOB
    assert route.container == "disposable"
    assert route.blob_uri == "https://acct.blob.core.windows.net/"
    assert route.connection_string is None
    assert route.client_id is None
    assert "acct" not in repr(route)
    other = integration_route({
        OPT_IN: "1", CONTAINER: "disposable",
        SERVICE_URI: "https://acct.blob.core.windows.net/",
    }, tmp_path)
    assert other.app_key != route.app_key


def test_integration_route_keeps_connection_string_support(tmp_path):
    route = integration_route({
        OPT_IN: "1", CONTAINER: "disposable", CONNECTION: "UseDevelopmentStorage=true",
    }, tmp_path)
    assert route is not None
    assert route.connection_string == "UseDevelopmentStorage=true"
    assert route.blob_uri is None


@pytest.mark.parametrize("uri", [
    "http://acct.blob.core.windows.net",
    "https://acct.blob.core.windows.net/?sv=secret-sas",
    "acct.blob.core.windows.net",
])
def test_integration_route_rejects_unsafe_service_uri_without_echoing_it(tmp_path, uri):
    with pytest.raises(ValueError, match=SERVICE_URI) as exc:
        integration_route({OPT_IN: "1", CONTAINER: "disposable", SERVICE_URI: uri}, tmp_path)
    assert "secret" not in str(exc.value) and "acct" not in str(exc.value)


def test_integration_route_rejects_ambiguous_target(tmp_path):
    with pytest.raises(ValueError, match="exactly one"):
        integration_route({
            OPT_IN: "1", CONTAINER: "disposable", CONNECTION: "AccountKey=secret",
            SERVICE_URI: "https://acct.blob.core.windows.net",
        }, tmp_path)


def offline_cleanup_case(tmp_path, *responses):
    route = integration_route({
        OPT_IN: "1", CONTAINER: "disposable", SERVICE_URI: "https://acct.blob.core.windows.net",
    }, tmp_path)
    transport = ScriptedBlobTransport(*responses)
    service = BlobServiceClient(
        "https://acct.blob.core.windows.net", transport=transport, retry_total=0
    )
    return route, transport, service


def request_summary(route, request):
    parts = urlsplit(request.url)
    name = unquote(parts.path).removeprefix(f"/{route.container}/")
    query = parse_qs(parts.query)
    return request.method, name, query.get("comp", [None])[0], {
        key: request.headers.get(key)
        for key in ("x-ms-lease-action", "x-ms-lease-break-period", "x-ms-delete-snapshots")
        if request.headers.get(key) is not None
    }


@pytest.mark.asyncio
async def test_cleanup_breaks_held_test_lease_then_deletes_exact_blob(tmp_path):
    route, transport, service = offline_cleanup_case(
        tmp_path, (412, "LeaseIdMissing"), (202, None), (202, None),
    )
    name = state_name(route, "agent", "conversation")
    async with service:
        await delete_owned_blobs(service, route, {name})

    assert [request_summary(route, request) for request in transport.requests] == [
        ("DELETE", name, None, {"x-ms-delete-snapshots": "include"}),
        ("PUT", name, "lease", {"x-ms-lease-action": "break", "x-ms-lease-break-period": "0"}),
        ("DELETE", name, None, {"x-ms-delete-snapshots": "include"}),
    ]


@pytest.mark.asyncio
async def test_cleanup_surfaces_non_lease_errors_without_breaking_and_continues(tmp_path):
    route, transport, service = offline_cleanup_case(
        tmp_path, (409, "SnapshotOperationRateExceeded"), (404, "BlobNotFound"),
    )
    names = sorted(state_name(route, "agent", session) for session in ("a", "b"))
    async with service:
        with pytest.raises(ExceptionGroup) as caught:
            await delete_owned_blobs(service, route, set(names))

    [error] = caught.value.exceptions
    assert isinstance(error, HttpResponseError) and error.status_code == 409
    assert f"Leftover test blob: {names[0]}" in error.__notes__
    assert [request_summary(route, r)[:2] for r in transport.requests] == [
        ("DELETE", names[0]), ("DELETE", names[1]),
    ]


@pytest.mark.asyncio
async def test_cleanup_refuses_any_blob_outside_the_run_prefix_before_requests(tmp_path):
    route, transport, service = offline_cleanup_case(tmp_path)
    own = state_name(route, "agent", "conversation")
    async with service:
        with pytest.raises(AssertionError, match="outside"):
            await delete_owned_blobs(
                service, route, {own, "copilot-native/v1/other-app/agent/state.json"}
            )
        with pytest.raises(AssertionError, match="outside"):
            await break_owned_lease(service, route, "copilot-native/v1/other-app/x")

    assert transport.requests == []
