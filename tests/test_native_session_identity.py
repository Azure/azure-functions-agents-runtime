from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from azure.core.exceptions import ResourceNotFoundError

from azure_functions_agents import _harness
from azure_functions_agents._native_session_identity import (
    IncompatibleSessionError,
    PersistenceUnavailableError,
    StorageMode,
    guard_opposite_history,
    opaque_key,
    resolve_route,
    state_name,
)


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_DIR", str(tmp_path / "sessions"))
    for key in (
        "WEBSITE_INSTANCE_ID", "AzureWebJobsStorage", "AzureWebJobsStorage__blobServiceUri",
        "AZURE_FUNCTIONS_AGENTS_COPILOT_SESSION_STORAGE",
    ):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


@pytest.mark.parametrize("value", ["local", "LOCAL ", " BlOb", "blob"])
def test_storage_mode_parsing(configured, monkeypatch, value):
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_COPILOT_SESSION_STORAGE", value)
    if value.strip().lower() == "blob":
        monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")
    assert resolve_route(configured).mode.value == value.strip().lower()


@pytest.mark.parametrize("value", ["", " ", "auto", "file"])
def test_invalid_storage_mode_is_not_logged(configured, monkeypatch, value):
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_COPILOT_SESSION_STORAGE", value)
    with pytest.raises(ValueError, match="must be local or blob") as error:
        resolve_route(configured)
    if value.strip():
        assert value not in str(error.value)


def test_deployment_defaults_and_identity_requirements(configured, monkeypatch):
    assert resolve_route(configured).mode is StorageMode.LOCAL
    monkeypatch.setenv("WEBSITE_INSTANCE_ID", "worker-1")
    with pytest.raises(ValueError, match="WEBSITE_SITE_NAME"):
        resolve_route(configured)
    monkeypatch.setenv("WEBSITE_SITE_NAME", "app")
    monkeypatch.setenv("AzureWebJobsStorage__blobServiceUri", "https://account.blob.core.windows.net")
    deployed = resolve_route(configured)
    assert deployed.mode is StorageMode.BLOB
    monkeypatch.setenv("WEBSITE_INSTANCE_ID", "worker-2")
    assert resolve_route(configured).app_key == deployed.app_key
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_COPILOT_SESSION_STORAGE", "local")
    with pytest.raises(ValueError, match="deployed"):
        resolve_route(configured)


def test_context_freezes_storage_and_separates_client_identity(configured, monkeypatch):
    monkeypatch.setenv(_harness.FLAG, "true")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "openai")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_MODEL", "model")
    monkeypatch.setattr(_harness, "_HARNESSES", {})
    old = _harness.get_harness(configured, new_app=True)
    monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_COPILOT_SESSION_STORAGE", "BLOB")
    new = _harness.get_harness(configured, new_app=True)
    assert old.session_storage.mode is StorageMode.LOCAL
    assert new.session_storage.mode is StorageMode.BLOB
    assert old.session_storage.identity_key != new.session_storage.identity_key
    assert "UseDevelopmentStorage" not in repr(new)


@pytest.mark.parametrize("session_id", [".", "..", "../outside", "/etc/passwd", ""])
def test_invalid_logical_identity_never_becomes_path(configured, session_id):
    with pytest.raises(ValueError):
        state_name(resolve_route(configured), "agent", session_id)


def test_state_paths_contain_only_hashed_segments(configured):
    route = resolve_route(configured)
    name = state_name(route, "agent", "one.test")
    assert name.startswith("copilot-native/v1/a_")
    assert "/g_" in name and "/s_" in name and name.endswith("/state.json")
    assert "agent" not in name and "one.test" not in name
    assert opaque_key("s", "one.test") != opaque_key("g", "one.test")


@pytest.mark.asyncio
async def test_bidirectional_metadata_only_guard_preserves_history(configured):
    route = resolve_route(configured)
    maf = route.local_dir / "agent" / "session.jsonl"
    maf.parent.mkdir(parents=True)
    maf.write_bytes(b"opaque maf bytes\x00")
    with pytest.raises(IncompatibleSessionError, match="MAF history"):
        await guard_opposite_history(route, "agent", "session", native=True)
    assert maf.read_bytes() == b"opaque maf bytes\x00"
    maf.unlink()
    native = route.local_dir / state_name(route, "agent", "session")
    native.parent.mkdir(parents=True)
    native.write_bytes(b"opaque native bytes\x00")
    with pytest.raises(IncompatibleSessionError, match="native state"):
        await guard_opposite_history(route, "agent", "session", native=False)
    assert native.read_bytes() == b"opaque native bytes\x00"
    maf.write_bytes(b"opaque maf bytes\x00")
    await guard_opposite_history(route, "agent", "session", native=False)
    await guard_opposite_history(route, "agent", "session", native=True)


@pytest.mark.asyncio
async def test_indeterminate_metadata_probe_fails_closed(configured, monkeypatch):
    from azure_functions_agents import _native_session_identity as identity

    route = replace(resolve_route(configured), mode=StorageMode.BLOB, connection_string="fake")

    async def broken(_route, _name):
        raise OSError("secret credentials should never be exposed")

    monkeypatch.setattr(identity, "_blob_metadata_exists", broken)
    with pytest.raises(PersistenceUnavailableError):
        await guard_opposite_history(route, "agent", "session", native=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected"),
    [(None, True), (ResourceNotFoundError("missing"), False), (RuntimeError("failed"), None)],
)
async def test_blob_metadata_probe_closes_owned_service_and_credential(
    configured, monkeypatch, result, expected
):
    from azure.storage.blob import aio

    from azure_functions_agents import _credential
    from azure_functions_agents import _native_session_identity as identity

    closed = []
    properties = AsyncMock(side_effect=result if isinstance(result, BaseException) else None)
    credential = SimpleNamespace(close=AsyncMock(side_effect=lambda: closed.append("credential")))
    service = SimpleNamespace(
        close=AsyncMock(side_effect=lambda: closed.append("service")),
        get_blob_client=lambda **_kwargs: SimpleNamespace(get_blob_properties=properties),
    )
    monkeypatch.setattr(
        _credential, "build_async_credential_with_client_id", lambda _client_id: credential
    )
    monkeypatch.setattr(aio, "BlobServiceClient", lambda **_kwargs: service)
    route = replace(
        resolve_route(configured),
        mode=StorageMode.BLOB,
        connection_string=None,
        blob_uri="https://example.blob.core.windows.net",
    )

    if expected is None:
        with pytest.raises(PersistenceUnavailableError):
            await identity._blob_metadata_exists(route, "state")
    else:
        assert await identity._blob_metadata_exists(route, "state") is expected

    assert closed == ["service", "credential"]
