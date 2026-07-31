"""Pure unit tests for AzureWebJobsStorage Table connection resolution + fingerprint.

Constructing a ``TableServiceClient`` (sync or async) from a connection string
or an endpoint/credential pair performs no network I/O -- only local parsing --
so these tests exercise the real ``azure-data-tables`` SDK without needing
Azurite running. Real Table I/O (create/read/CAS/EGT) is covered separately by
the Azurite-backed tests under ``tests/endtoend/``.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from azure_functions_agents.session_state import (
    StateStoreConfigurationError,
    TableConnectionSettings,
    compute_state_store_fingerprint,
    get_table_service_client,
    reset_connection_caches_for_testing,
    resolve_table_connection_settings,
)
from azure_functions_agents.session_state._label_encoding import LABEL_SAFE_PAYLOAD_PATTERN

_DEV_CONNECTION_STRING = (
    "DefaultEndpointsProtocol=http;"
    "AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/"
    "K1SZFPTOtr/KBHBeksoGMGw==;"
    "TableEndpoint=http://127.0.0.1:10002/devstoreaccount1;"
)
_DEV_CONNECTION_STRING_ROTATED_KEY = (
    "DefaultEndpointsProtocol=http;"
    "AccountName=devstoreaccount1;"
    "AccountKey=00000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000000000000000000000==;"
    "TableEndpoint=http://127.0.0.1:10002/devstoreaccount1;"
)
_OTHER_ACCOUNT_CONNECTION_STRING = (
    "DefaultEndpointsProtocol=http;"
    "AccountName=otheraccount;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/"
    "K1SZFPTOtr/KBHBeksoGMGw==;"
    "TableEndpoint=http://127.0.0.1:10002/otheraccount;"
)
_OTHER_ENDPOINT_CONNECTION_STRING = (
    "DefaultEndpointsProtocol=http;"
    "AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/"
    "K1SZFPTOtr/KBHBeksoGMGw==;"
    "TableEndpoint=http://127.0.0.1:19999/devstoreaccount1;"
)


def _env(values: Mapping[str, str]) -> object:
    return values.get


# ---------------------------------------------------------------------------
# resolve_table_connection_settings
# ---------------------------------------------------------------------------


def test_connection_string_wins_when_both_forms_are_configured() -> None:
    settings = resolve_table_connection_settings(
        _env(
            {
                "AzureWebJobsStorage": _DEV_CONNECTION_STRING,
                "AzureWebJobsStorage__tableServiceUri": "https://example.table.core.windows.net",
            }
        )
    )

    assert settings == TableConnectionSettings(
        connection_string=_DEV_CONNECTION_STRING,
        table_service_uri=None,
        client_id=None,
    )


def test_identity_based_uri_used_when_no_connection_string() -> None:
    settings = resolve_table_connection_settings(
        _env({"AzureWebJobsStorage__tableServiceUri": "https://example.table.core.windows.net"})
    )

    assert settings.connection_string is None
    assert settings.table_service_uri == "https://example.table.core.windows.net"
    assert settings.client_id is None


def test_client_id_precedence_storage_specific_then_azure_client_id_then_none() -> None:
    storage_specific = resolve_table_connection_settings(
        _env(
            {
                "AzureWebJobsStorage__tableServiceUri": "https://example.table.core.windows.net",
                "AzureWebJobsStorage__clientId": "storage-client-id",
                "AZURE_CLIENT_ID": "app-client-id",
            }
        )
    )
    assert storage_specific.client_id == "storage-client-id"

    app_wide = resolve_table_connection_settings(
        _env(
            {
                "AzureWebJobsStorage__tableServiceUri": "https://example.table.core.windows.net",
                "AZURE_CLIENT_ID": "app-client-id",
            }
        )
    )
    assert app_wide.client_id == "app-client-id"

    neither = resolve_table_connection_settings(
        _env({"AzureWebJobsStorage__tableServiceUri": "https://example.table.core.windows.net"})
    )
    assert neither.client_id is None


def test_fails_closed_when_neither_form_is_configured() -> None:
    with pytest.raises(StateStoreConfigurationError, match="AzureWebJobsStorage"):
        resolve_table_connection_settings(_env({}))

    with pytest.raises(StateStoreConfigurationError):
        resolve_table_connection_settings(_env({"AzureWebJobsStorage": "   "}))


def test_error_message_documents_no_dedicated_account_or_fallback() -> None:
    with pytest.raises(StateStoreConfigurationError) as excinfo:
        resolve_table_connection_settings(_env({}))
    assert "dedicated" in str(excinfo.value)
    assert "fallback" in str(excinfo.value)


# ---------------------------------------------------------------------------
# compute_state_store_fingerprint
# ---------------------------------------------------------------------------


async def _service_client(connection_string: str) -> object:
    from azure.data.tables.aio import TableServiceClient

    return TableServiceClient.from_connection_string(connection_string)


@pytest.mark.asyncio
async def test_fingerprint_matches_label_safe_shape() -> None:
    client = await _service_client(_DEV_CONNECTION_STRING)
    try:
        fingerprint = compute_state_store_fingerprint(client)
    finally:
        await client.close()

    assert fingerprint.startswith("s1-")
    assert LABEL_SAFE_PAYLOAD_PATTERN.fullmatch(fingerprint.removeprefix("s1-"))
    assert len(fingerprint) == 55


@pytest.mark.asyncio
async def test_same_account_rotated_key_yields_the_same_fingerprint() -> None:
    original = await _service_client(_DEV_CONNECTION_STRING)
    rotated = await _service_client(_DEV_CONNECTION_STRING_ROTATED_KEY)
    try:
        assert compute_state_store_fingerprint(original) == compute_state_store_fingerprint(
            rotated
        )
    finally:
        await original.close()
        await rotated.close()


@pytest.mark.asyncio
async def test_different_account_yields_a_different_fingerprint() -> None:
    devstore = await _service_client(_DEV_CONNECTION_STRING)
    other = await _service_client(_OTHER_ACCOUNT_CONNECTION_STRING)
    try:
        assert compute_state_store_fingerprint(devstore) != compute_state_store_fingerprint(other)
    finally:
        await devstore.close()
        await other.close()


@pytest.mark.asyncio
async def test_different_endpoint_yields_a_different_fingerprint() -> None:
    devstore = await _service_client(_DEV_CONNECTION_STRING)
    other_port = await _service_client(_OTHER_ENDPOINT_CONNECTION_STRING)
    try:
        assert compute_state_store_fingerprint(devstore) != compute_state_store_fingerprint(
            other_port
        )
    finally:
        await devstore.close()
        await other_port.close()


@pytest.mark.asyncio
async def test_use_development_storage_alias_matches_its_explicit_equivalent() -> None:
    alias_client = await _service_client("UseDevelopmentStorage=true")
    explicit_client = await _service_client(_DEV_CONNECTION_STRING)
    try:
        assert compute_state_store_fingerprint(alias_client) == compute_state_store_fingerprint(
            explicit_client
        )
    finally:
        await alias_client.close()
        await explicit_client.close()


@pytest.mark.asyncio
async def test_fingerprint_never_contains_the_account_key_or_sas() -> None:
    client = await _service_client(_DEV_CONNECTION_STRING)
    try:
        fingerprint = compute_state_store_fingerprint(client)
    finally:
        await client.close()

    assert "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq" not in fingerprint
    assert "AccountKey" not in fingerprint


@pytest.mark.asyncio
async def test_fingerprint_rejects_a_url_with_embedded_userinfo() -> None:
    class _FakeServiceClient:
        # Duck-typed stand-in: only .url/.account_name are read by
        # compute_state_store_fingerprint, so a minimal fake proves the
        # userinfo guard without needing a real credential that embeds one.
        url = "http://user:pass@127.0.0.1:10002/devstoreaccount1"
        account_name = "devstoreaccount1"

    with pytest.raises(StateStoreConfigurationError, match="credentials"):
        compute_state_store_fingerprint(_FakeServiceClient())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# get_table_service_client caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_table_service_client_caches_by_fingerprint_not_connection_string() -> None:
    reset_connection_caches_for_testing()
    try:
        first_client, first_fingerprint = await get_table_service_client(
            TableConnectionSettings(
                connection_string=_DEV_CONNECTION_STRING,
                table_service_uri=None,
                client_id=None,
            )
        )
        # A different connection string (rotated key) that normalizes to the
        # SAME fingerprint must reuse the cached client instance.
        second_client, second_fingerprint = await get_table_service_client(
            TableConnectionSettings(
                connection_string=_DEV_CONNECTION_STRING_ROTATED_KEY,
                table_service_uri=None,
                client_id=None,
            )
        )
        assert first_fingerprint == second_fingerprint
        assert first_client is second_client

        other_client, other_fingerprint = await get_table_service_client(
            TableConnectionSettings(
                connection_string=_OTHER_ACCOUNT_CONNECTION_STRING,
                table_service_uri=None,
                client_id=None,
            )
        )
        assert other_fingerprint != first_fingerprint
        assert other_client is not first_client
    finally:
        reset_connection_caches_for_testing()
