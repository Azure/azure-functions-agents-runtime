"""Shared ``DefaultAzureCredential`` builders that honor ``AZURE_CLIENT_ID``.

Multi-identity Function Apps need an explicit ``managed_identity_client_id``;
without one, :class:`DefaultAzureCredential` picks a managed identity
non-deterministically when more than one is assigned. These helpers centralize
the ``AZURE_CLIENT_ID`` lookup so every component (the MAF client manager,
the ACA sandbox, the ARM/connector data-plane clients) selects the same
identity in the same way.

``BlobHistoryProvider`` deliberately does **not** use these helpers because it
follows a storage-specific precedence (``AzureWebJobsStorage__clientId`` wins,
then falls back to ``AZURE_CLIENT_ID``); see
:mod:`azure_functions_agents._blob_history`.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from azure.identity import DefaultAzureCredential as SyncDefaultAzureCredential
    from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential


_AZURE_CLIENT_ID_ENV = "AZURE_CLIENT_ID"


def build_credential() -> SyncDefaultAzureCredential:
    """Return a sync ``DefaultAzureCredential`` honoring ``AZURE_CLIENT_ID``."""
    from azure.identity import DefaultAzureCredential

    client_id = os.environ.get(_AZURE_CLIENT_ID_ENV)
    if client_id:
        return DefaultAzureCredential(managed_identity_client_id=client_id)
    return DefaultAzureCredential()


def build_credential_with_client_id(
    client_id: str | None,
) -> SyncDefaultAzureCredential:
    """Return a sync ``DefaultAzureCredential`` for a caller-supplied client id.

    Pass an empty/``None`` value to fall back to a bare
    :class:`DefaultAzureCredential`.
    """
    from azure.identity import DefaultAzureCredential

    if client_id:
        return DefaultAzureCredential(managed_identity_client_id=client_id)
    return DefaultAzureCredential()


def build_async_credential() -> AsyncDefaultAzureCredential:
    """Return an async ``DefaultAzureCredential`` honoring ``AZURE_CLIENT_ID``."""
    from azure.identity.aio import DefaultAzureCredential

    client_id = os.environ.get(_AZURE_CLIENT_ID_ENV)
    if client_id:
        return DefaultAzureCredential(managed_identity_client_id=client_id)
    return DefaultAzureCredential()


def build_async_credential_with_client_id(
    client_id: str | None,
) -> AsyncDefaultAzureCredential:
    """Return an async ``DefaultAzureCredential`` for a caller-supplied client id.

    Use this when the calling module has its own precedence rules for which
    environment variable identifies the managed identity (e.g.
    :class:`BlobHistoryProvider` prefers ``AzureWebJobsStorage__clientId`` and
    only falls back to ``AZURE_CLIENT_ID``). Pass an empty/``None`` value to
    fall back to a bare :class:`DefaultAzureCredential`.
    """
    from azure.identity.aio import DefaultAzureCredential

    if client_id:
        return DefaultAzureCredential(managed_identity_client_id=client_id)
    return DefaultAzureCredential()
