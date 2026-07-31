"""``AzureWebJobsStorage`` Table connection resolution and state-store fingerprint.

Session state always reuses the Function App's own ``AzureWebJobsStorage``
account -- there is no dedicated/alternate state-account setting. This module
resolves a usable Azure Table connection from it and derives
``compute_state_store_fingerprint``'s non-secret ``s1-<52 base32>`` identity
token, following the same precedence and caching shape as
:mod:`azure_functions_agents._blob_history`:

* ``AzureWebJobsStorage`` -- connection string (local dev, Azurite, or a real
  storage account's connection string). Tried first.
* ``AzureWebJobsStorage__tableServiceUri`` (+ optional
  ``AzureWebJobsStorage__clientId``, else ``AZURE_CLIENT_ID``, else a bare
  ``DefaultAzureCredential``) -- identity-based, matching how Azure Functions
  deploys configure managed-identity storage access.

Neither present -> :class:`StateStoreConfigurationError` (fail closed; never
falls back to in-memory/local state).

The Azure Tables SDK is imported lazily (inside functions) so that importing
this module -- and therefore importing :mod:`azure_functions_agents` itself --
never requires the ``[aca_sandbox]`` extra. ``in_lang_worker`` installs are
unaffected.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .errors import StateStoreConfigurationError
from .identity import frame_canonical_components
from .session_models import STATE_STORE_FINGERPRINT_VERSION, validate_state_store_fingerprint

if TYPE_CHECKING:
    from azure.data.tables.aio import TableServiceClient

type EnvironmentReader = Callable[[str], str | None]

_CONN_STRING_ENV = "AzureWebJobsStorage"
_TABLE_SERVICE_URI_ENV = "AzureWebJobsStorage__tableServiceUri"
_CLIENT_ID_ENV = "AzureWebJobsStorage__clientId"
_AZURE_CLIENT_ID_ENV = "AZURE_CLIENT_ID"


@dataclass(frozen=True, slots=True)
class TableConnectionSettings:
    """Resolved description of how to reach Azure Tables.

    ``connection_string`` and ``table_service_uri`` are mutually exclusive;
    exactly one is set (connection string wins when both are configured,
    matching :mod:`azure_functions_agents._blob_history`'s precedence).

    ``connection_string`` embeds the storage account key when present, so it
    is excluded from ``repr()`` (``field(repr=False)``) -- callers must never
    log or interpolate this object directly; cache keys and log/error
    messages use only the non-secret ``s1-`` fingerprint (see
    :func:`compute_state_store_fingerprint`).
    """

    connection_string: str | None = field(repr=False)
    table_service_uri: str | None
    client_id: str | None


def resolve_table_connection_settings(
    get_environment: EnvironmentReader = os.getenv,
) -> TableConnectionSettings:
    """Resolve Table connection settings from ``AzureWebJobsStorage`` only.

    Fails closed with :class:`StateStoreConfigurationError` when neither the
    connection-string nor identity-based form is configured -- there is no
    dedicated state-account setting and no in-memory fallback.
    """
    connection_string = (get_environment(_CONN_STRING_ENV) or "").strip()
    table_service_uri = (get_environment(_TABLE_SERVICE_URI_ENV) or "").strip()
    if connection_string:
        return TableConnectionSettings(
            connection_string=connection_string,
            table_service_uri=None,
            client_id=None,
        )
    if table_service_uri:
        client_id = (
            get_environment(_CLIENT_ID_ENV) or get_environment(_AZURE_CLIENT_ID_ENV) or ""
        ).strip() or None
        return TableConnectionSettings(
            connection_string=None,
            table_service_uri=table_service_uri,
            client_id=client_id,
        )
    raise StateStoreConfigurationError(
        "Session state requires AzureWebJobsStorage (connection string) or "
        f"{_TABLE_SERVICE_URI_ENV} (identity-based) to be configured; neither "
        "was found. Session state has no dedicated-account or in-memory "
        "fallback."
    )


def _build_table_service_client(settings: TableConnectionSettings) -> TableServiceClient:
    from azure.data.tables.aio import TableServiceClient

    if settings.connection_string:
        try:
            return TableServiceClient.from_connection_string(settings.connection_string)
        except ValueError as exc:
            raise StateStoreConfigurationError(
                "AzureWebJobsStorage is not a usable Azure Table connection string."
            ) from exc
    if settings.table_service_uri:
        from .._credential import build_async_credential_with_client_id

        credential = build_async_credential_with_client_id(settings.client_id)
        return TableServiceClient(endpoint=settings.table_service_uri, credential=credential)
    raise StateStoreConfigurationError(
        "TableConnectionSettings has neither a connection string nor a table service URI."
    )


def _normalize_table_endpoint_identity(service_client: TableServiceClient) -> tuple[str, str]:
    """Return ``(normalized_endpoint, normalized_account_name)`` -- no secrets.

    Reads only ``service_client.url`` and ``service_client.account_name``,
    which the SDK already keeps separate from the credential/key material
    (connection-string account keys and SAS tokens never appear in ``.url``).
    Query strings, fragments, and any embedded userinfo are defensively
    stripped/rejected here as well, so a fingerprint can never encode
    credential material regardless of SDK version behavior.
    """
    parsed = urlsplit(service_client.url)
    if parsed.username or parsed.password:
        raise StateStoreConfigurationError(
            "AzureWebJobsStorage Table endpoint must not embed credentials in the URL."
        )
    if not parsed.scheme or not parsed.hostname:
        raise StateStoreConfigurationError(
            "AzureWebJobsStorage Table endpoint could not be resolved to a valid URL."
        )
    host = parsed.hostname.lower()
    endpoint = f"{parsed.scheme.lower()}://{host}"
    if parsed.port is not None:
        endpoint = f"{endpoint}:{parsed.port}"
    account_name = (service_client.account_name or "").strip().lower()
    if not account_name:
        raise StateStoreConfigurationError(
            "AzureWebJobsStorage Table account name could not be resolved."
        )
    return endpoint, account_name


def compute_state_store_fingerprint(service_client: TableServiceClient) -> str:
    """Derive the canonical ``s1-<52 base32>`` non-secret state-store fingerprint.

    Same account with a rotated key/credential yields the same fingerprint
    (the key/credential never enters the hashed bytes); a different account
    or endpoint always differs. Uses the same delimiter-safe canonical framing
    and label-safe base32 encoding as the ``a1``/``o1`` identity hashes, for
    consistency.
    """
    import hashlib

    from ._label_encoding import encode_label_safe_digest

    endpoint, account_name = _normalize_table_endpoint_identity(service_client)
    canonical = frame_canonical_components(
        (STATE_STORE_FINGERPRINT_VERSION, endpoint, account_name)
    )
    digest = hashlib.sha256(canonical).digest()
    fingerprint = f"{STATE_STORE_FINGERPRINT_VERSION}-{encode_label_safe_digest(digest)}"
    return validate_state_store_fingerprint(fingerprint)


# ---------------------------------------------------------------------------
# Process-wide client cache, keyed by the non-secret fingerprint only.
# ---------------------------------------------------------------------------

_TABLE_SERVICE_CLIENTS: dict[str, Any] = {}
_TABLE_SERVICE_CLIENTS_LOCK = asyncio.Lock()


async def get_table_service_client(
    settings: TableConnectionSettings | None = None,
) -> tuple[TableServiceClient, str]:
    """Return a cached ``(TableServiceClient, fingerprint)`` for the settings.

    Resolves settings from the environment when not supplied. Clients are
    cached process-wide by the computed fingerprint (never by connection
    string or credential), matching :mod:`azure_functions_agents._blob_history`.
    Constructing a client is local parsing only (no network I/O), so computing
    the fingerprint before consulting the cache is cheap even on a cache hit.
    """
    resolved_settings = settings or resolve_table_connection_settings()
    candidate = _build_table_service_client(resolved_settings)
    try:
        fingerprint = compute_state_store_fingerprint(candidate)
    except Exception:
        await candidate.close()
        raise

    cached = _TABLE_SERVICE_CLIENTS.get(fingerprint)
    if cached is not None:
        await candidate.close()
        return cached, fingerprint
    async with _TABLE_SERVICE_CLIENTS_LOCK:
        cached = _TABLE_SERVICE_CLIENTS.get(fingerprint)
        if cached is not None:
            await candidate.close()
            return cached, fingerprint
        _TABLE_SERVICE_CLIENTS[fingerprint] = candidate
        return candidate, fingerprint


def reset_caches_for_testing() -> None:
    """Drop the module-level client cache. Test-only helper."""
    _TABLE_SERVICE_CLIENTS.clear()
