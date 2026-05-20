"""Tests for :mod:`azure_functions_agents._blob_history`.

Azure SDK clients are stubbed out — we replace the
:func:`_build_service_client` factory with an in-memory fake so the tests
exercise the provider's full state machine (download → save → race on
create) without any network or Azurite dependency.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, ClassVar

import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from azure_functions_agents import _blob_history
from azure_functions_agents._blob_history import (
    DEFAULT_BLOB_PREFIX,
    DEFAULT_CONTAINER_NAME,
    BlobHistoryProvider,
    _normalize_prefix,
    _service_client_cache_key,
    build_blob_provider_from_environment,
    reset_caches_for_testing,
)

# ---------------------------------------------------------------------------
# In-memory blob fakes
# ---------------------------------------------------------------------------


class _FakeDownloader:
    def __init__(self, content: bytes) -> None:
        self._content = content

    async def readall(self) -> bytes:
        return self._content


class _FakeBlobClient:
    def __init__(self, account: _FakeAccount, container: str, blob: str) -> None:
        self._account = account
        self._container = container
        self._blob = blob

    @property
    def _key(self) -> tuple[str, str]:
        return (self._container, self._blob)

    async def download_blob(self, *, encoding: str | None = None) -> _FakeDownloader:
        if self._key not in self._account.blobs:
            raise ResourceNotFoundError("blob not found")
        return _FakeDownloader(self._account.blobs[self._key])

    async def create_append_blob(self) -> None:
        if self._key in self._account.blobs:
            raise ResourceExistsError("blob exists")
        self._account.blobs[self._key] = b""
        self._account.create_calls.append(self._key)

    async def append_block(self, data: bytes) -> None:
        self._account.append_calls.append((self._key, data))
        if self._key not in self._account.blobs:
            raise ResourceNotFoundError("blob not found")
        self._account.blobs[self._key] = self._account.blobs[self._key] + data


class _FakeContainerClient:
    def __init__(self, account: _FakeAccount, container: str) -> None:
        self._account = account
        self._container = container

    async def create_container(self) -> None:
        if self._container in self._account.containers:
            raise ResourceExistsError("container exists")
        self._account.containers.add(self._container)
        self._account.container_create_calls.append(self._container)


class _FakeServiceClient:
    def __init__(self, account: _FakeAccount) -> None:
        self._account = account

    def get_container_client(self, container: str) -> _FakeContainerClient:
        return _FakeContainerClient(self._account, container)

    def get_blob_client(self, *, container: str, blob: str) -> _FakeBlobClient:
        return _FakeBlobClient(self._account, container, blob)


class _FakeAccount:
    """In-memory storage account fake shared across all clients in one test."""

    def __init__(self, *, existing_containers: set[str] | None = None) -> None:
        self.containers: set[str] = set(existing_containers or ())
        self.blobs: dict[tuple[str, str], bytes] = {}
        self.append_calls: list[tuple[tuple[str, str], bytes]] = []
        self.create_calls: list[tuple[str, str]] = []
        self.container_create_calls: list[str] = []


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    reset_caches_for_testing()


@pytest.fixture
def fake_account(monkeypatch: pytest.MonkeyPatch) -> _FakeAccount:
    account = _FakeAccount()

    def _build(*, connection_string=None, blob_service_url=None, credential=None) -> Any:
        return _FakeServiceClient(account)

    monkeypatch.setattr(_blob_history, "_build_service_client", _build)
    return account


def _make_message(text: str, role: str = "user") -> Any:
    from agent_framework import Message

    return Message(role=role, contents=[text])


# ---------------------------------------------------------------------------
# Pure-function helpers
# ---------------------------------------------------------------------------


def test_normalize_prefix_strips_leading_slash_and_adds_trailing() -> None:
    assert _normalize_prefix("/foo") == "foo/"
    assert _normalize_prefix("foo/") == "foo/"
    assert _normalize_prefix("foo") == "foo/"
    assert _normalize_prefix("") == ""
    assert _normalize_prefix("/") == ""


def test_service_client_cache_key_uses_url_directly() -> None:
    key = _service_client_cache_key(
        connection_string=None, blob_service_url="https://acct.blob.core.windows.net/"
    )
    assert key == "url::https://acct.blob.core.windows.net/"


def test_service_client_cache_key_hashes_connection_string() -> None:
    secret = "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=topsecret;"
    key = _service_client_cache_key(connection_string=secret, blob_service_url=None)
    assert key.startswith("conn::")
    # The raw secret must not appear in the cache key.
    assert "topsecret" not in key
    assert "AccountKey" not in key
    # Stable for the same input.
    assert key == _service_client_cache_key(connection_string=secret, blob_service_url=None)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_requires_connection_or_url() -> None:
    with pytest.raises(ValueError, match="connection_string"):
        BlobHistoryProvider()


# ---------------------------------------------------------------------------
# get_messages
# ---------------------------------------------------------------------------


def test_get_messages_returns_empty_when_blob_missing(fake_account: _FakeAccount) -> None:
    provider = BlobHistoryProvider(connection_string="UseDevelopmentStorage=true")
    result = asyncio.run(provider.get_messages("sess-1"))
    assert result == []
    # Container must have been ensured.
    assert DEFAULT_CONTAINER_NAME in fake_account.containers


def test_get_messages_parses_jsonl(fake_account: _FakeAccount) -> None:
    from agent_framework import Message

    msgs = [Message(role="user", contents=["hi"]), Message(role="assistant", contents=["hello"])]
    blob_key = (DEFAULT_CONTAINER_NAME, f"{DEFAULT_BLOB_PREFIX}sess-1.jsonl")
    fake_account.blobs[blob_key] = (
        "".join(f"{json.dumps(m.to_dict())}\n" for m in msgs).encode("utf-8")
    )

    provider = BlobHistoryProvider(connection_string="UseDevelopmentStorage=true")
    result = asyncio.run(provider.get_messages("sess-1"))
    assert [m.text for m in result] == ["hi", "hello"]
    assert [str(m.role) for m in result] == ["user", "assistant"]


def test_get_messages_raises_on_invalid_json(fake_account: _FakeAccount) -> None:
    blob_key = (DEFAULT_CONTAINER_NAME, f"{DEFAULT_BLOB_PREFIX}sess-1.jsonl")
    fake_account.blobs[blob_key] = b"{not json}\n"
    provider = BlobHistoryProvider(connection_string="UseDevelopmentStorage=true")
    with pytest.raises(ValueError, match="Failed to deserialize"):
        asyncio.run(provider.get_messages("sess-1"))


def test_get_messages_skips_blank_lines(fake_account: _FakeAccount) -> None:
    msg = _make_message("hi")
    blob_key = (DEFAULT_CONTAINER_NAME, f"{DEFAULT_BLOB_PREFIX}sess-1.jsonl")
    fake_account.blobs[blob_key] = f"\n{json.dumps(msg.to_dict())}\n\n".encode()
    provider = BlobHistoryProvider(connection_string="UseDevelopmentStorage=true")
    result = asyncio.run(provider.get_messages("sess-1"))
    assert len(result) == 1
    assert result[0].text == "hi"


# ---------------------------------------------------------------------------
# save_messages
# ---------------------------------------------------------------------------


def test_save_messages_creates_then_appends_on_first_write(
    fake_account: _FakeAccount,
) -> None:
    provider = BlobHistoryProvider(connection_string="UseDevelopmentStorage=true")
    msgs = [_make_message("hi"), _make_message("there", role="assistant")]
    asyncio.run(provider.save_messages("sess-1", msgs))

    blob_key = (DEFAULT_CONTAINER_NAME, f"{DEFAULT_BLOB_PREFIX}sess-1.jsonl")
    assert blob_key in fake_account.blobs
    assert fake_account.create_calls == [blob_key]
    # Two append calls: one failed (before create) + one succeeded (after create).
    assert len(fake_account.append_calls) == 2

    stored = fake_account.blobs[blob_key].decode("utf-8").strip().splitlines()
    assert [json.loads(line)["contents"][0]["text"] for line in stored] == ["hi", "there"]


def test_save_messages_appends_without_recreating_when_blob_exists(
    fake_account: _FakeAccount,
) -> None:
    blob_key = (DEFAULT_CONTAINER_NAME, f"{DEFAULT_BLOB_PREFIX}sess-1.jsonl")
    fake_account.blobs[blob_key] = b""  # blob already exists
    provider = BlobHistoryProvider(connection_string="UseDevelopmentStorage=true")
    asyncio.run(provider.save_messages("sess-1", [_make_message("hi")]))
    # Single successful append; no create call.
    assert fake_account.create_calls == []
    assert len(fake_account.append_calls) == 1


def test_save_messages_handles_concurrent_create_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _FakeAccount()
    # Pre-create the blob to simulate another instance winning the race
    # between our first failed append and our create_append_blob call.
    blob_key = (DEFAULT_CONTAINER_NAME, f"{DEFAULT_BLOB_PREFIX}sess-1.jsonl")

    original_blob_cls = _FakeBlobClient
    original_append = original_blob_cls.append_block

    seen_first_append: dict[str, bool] = {}

    async def append_with_race(self: _FakeBlobClient, data: bytes) -> None:
        # On the very first append the blob doesn't exist yet — raise
        # ResourceNotFoundError. Before the test's create_append_blob is
        # called, mutate the account to simulate another instance winning.
        if not seen_first_append.get("done"):
            seen_first_append["done"] = True
            account.blobs[blob_key] = b""  # competitor created it first
            raise ResourceNotFoundError("blob not found")
        return await original_append(self, data)

    monkeypatch.setattr(_FakeBlobClient, "append_block", append_with_race)
    monkeypatch.setattr(
        _blob_history,
        "_build_service_client",
        lambda **_: _FakeServiceClient(account),
    )

    provider = BlobHistoryProvider(connection_string="UseDevelopmentStorage=true")
    # Should NOT raise even though create_append_blob raises ResourceExistsError.
    asyncio.run(provider.save_messages("sess-1", [_make_message("hi")]))
    assert blob_key in account.blobs
    # The stored payload reflects our second (successful) append.
    assert b"hi" in account.blobs[blob_key]


def test_save_messages_noop_for_empty_input(fake_account: _FakeAccount) -> None:
    provider = BlobHistoryProvider(connection_string="UseDevelopmentStorage=true")
    asyncio.run(provider.save_messages("sess-1", []))
    assert fake_account.append_calls == []
    assert fake_account.create_calls == []


# ---------------------------------------------------------------------------
# Round-trip: save then load
# ---------------------------------------------------------------------------


def test_round_trip_save_then_get(fake_account: _FakeAccount) -> None:
    provider = BlobHistoryProvider(connection_string="UseDevelopmentStorage=true")
    msgs = [_make_message("first"), _make_message("second", role="assistant")]
    asyncio.run(provider.save_messages("sess-1", msgs))

    # Second turn: another save appends to the existing blob.
    asyncio.run(provider.save_messages("sess-1", [_make_message("third")]))

    loaded = asyncio.run(provider.get_messages("sess-1"))
    assert [m.text for m in loaded] == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# Blob naming
# ---------------------------------------------------------------------------


def test_blob_name_uses_default_stem_for_none_session() -> None:
    provider = BlobHistoryProvider(connection_string="UseDevelopmentStorage=true")
    assert provider._blob_name(None) == f"{DEFAULT_BLOB_PREFIX}default.jsonl"


def test_blob_name_uses_custom_prefix() -> None:
    provider = BlobHistoryProvider(
        connection_string="UseDevelopmentStorage=true",
        blob_prefix="custom",
    )
    assert provider._blob_name("sess-1") == "custom/sess-1.jsonl"


# ---------------------------------------------------------------------------
# Environment factory
# ---------------------------------------------------------------------------


def test_build_from_environment_returns_none_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AzureWebJobsStorage", raising=False)
    monkeypatch.delenv("AzureWebJobsStorage__blobServiceUri", raising=False)
    assert build_blob_provider_from_environment() is None


def test_build_from_environment_prefers_connection_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")
    monkeypatch.setenv(
        "AzureWebJobsStorage__blobServiceUri", "https://acct.blob.core.windows.net/"
    )
    provider = build_blob_provider_from_environment()
    assert provider is not None
    assert provider._connection_string == "UseDevelopmentStorage=true"
    assert provider._blob_service_url is None


def test_build_from_environment_uses_blob_service_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AzureWebJobsStorage", raising=False)
    monkeypatch.setenv(
        "AzureWebJobsStorage__blobServiceUri", "https://acct.blob.core.windows.net/"
    )
    provider = build_blob_provider_from_environment()
    assert provider is not None
    assert provider._blob_service_url == "https://acct.blob.core.windows.net/"
    assert provider._connection_string is None


def test_build_from_environment_honors_container_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER", "my-container")
    provider = build_blob_provider_from_environment()
    assert provider is not None
    assert provider._container_name == "my-container"


def test_build_from_environment_empty_string_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AzureWebJobsStorage", "   ")
    monkeypatch.setenv("AzureWebJobsStorage__blobServiceUri", "")
    assert build_blob_provider_from_environment() is None


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------


def test_service_client_cached_across_calls(
    monkeypatch: pytest.MonkeyPatch, fake_account: _FakeAccount
) -> None:
    build_calls: list[None] = []
    original_build = _blob_history._build_service_client

    def counting_build(**kwargs: Any) -> Any:
        build_calls.append(None)
        return original_build(**kwargs)

    monkeypatch.setattr(_blob_history, "_build_service_client", counting_build)
    provider = BlobHistoryProvider(connection_string="UseDevelopmentStorage=true")
    asyncio.run(provider.get_messages("sess-1"))
    asyncio.run(provider.get_messages("sess-2"))
    assert len(build_calls) == 1


def test_container_create_called_once(fake_account: _FakeAccount) -> None:
    provider = BlobHistoryProvider(connection_string="UseDevelopmentStorage=true")
    asyncio.run(provider.get_messages("sess-1"))
    asyncio.run(provider.save_messages("sess-1", [_make_message("a")]))
    asyncio.run(provider.get_messages("sess-2"))
    # Across two sessions, container.create_container() runs once.
    assert fake_account.container_create_calls == [DEFAULT_CONTAINER_NAME]


# ---------------------------------------------------------------------------
# Identity-based credential precedence
# ---------------------------------------------------------------------------


class _CredentialSpy:
    instances: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, **kwargs: Any) -> None:
        type(self).instances.append(kwargs)


class _BlobServiceClientSpy:
    last_kwargs: ClassVar[dict[str, Any] | None] = None

    def __init__(self, **kwargs: Any) -> None:
        type(self).last_kwargs = kwargs


@pytest.fixture
def _credential_spies(monkeypatch: pytest.MonkeyPatch) -> type[_CredentialSpy]:
    import azure.identity.aio
    import azure.storage.blob.aio

    _CredentialSpy.instances = []
    _BlobServiceClientSpy.last_kwargs = None
    monkeypatch.setattr(azure.identity.aio, "DefaultAzureCredential", _CredentialSpy)
    monkeypatch.setattr(azure.storage.blob.aio, "BlobServiceClient", _BlobServiceClientSpy)
    return _CredentialSpy


def test_build_service_client_prefers_storage_specific_client_id(
    monkeypatch: pytest.MonkeyPatch, _credential_spies: type[_CredentialSpy]
) -> None:
    monkeypatch.setenv("AzureWebJobsStorage__clientId", "storage-uaid")
    monkeypatch.setenv("AZURE_CLIENT_ID", "app-uaid")
    _blob_history._build_service_client(
        connection_string=None,
        blob_service_url="https://example.blob.core.windows.net",
        credential=None,
    )
    assert _credential_spies.instances == [{"managed_identity_client_id": "storage-uaid"}]


def test_build_service_client_falls_back_to_azure_client_id(
    monkeypatch: pytest.MonkeyPatch, _credential_spies: type[_CredentialSpy]
) -> None:
    monkeypatch.delenv("AzureWebJobsStorage__clientId", raising=False)
    monkeypatch.setenv("AZURE_CLIENT_ID", "app-uaid")
    _blob_history._build_service_client(
        connection_string=None,
        blob_service_url="https://example.blob.core.windows.net",
        credential=None,
    )
    assert _credential_spies.instances == [{"managed_identity_client_id": "app-uaid"}]


def test_build_service_client_bare_credential_when_no_env(
    monkeypatch: pytest.MonkeyPatch, _credential_spies: type[_CredentialSpy]
) -> None:
    monkeypatch.delenv("AzureWebJobsStorage__clientId", raising=False)
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    _blob_history._build_service_client(
        connection_string=None,
        blob_service_url="https://example.blob.core.windows.net",
        credential=None,
    )
    assert _credential_spies.instances == [{}]
