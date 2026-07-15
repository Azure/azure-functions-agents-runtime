"""Blob Storage persistence layer for agent project files.

Mirrors the runtime's storage approach (see
``src/azure_functions_agents/_blob_history.py``): agent ``*.agent.md`` files are
persisted in the Function App's storage account. This is the portal's **working
copy** (requirements §5.3).

Backend selection (first match wins):

1. ``PORTAL_STORAGE_CONNECTION``  — a storage connection string.
2. ``PORTAL_STORAGE_ACCOUNT_URL`` — e.g. ``https://<acct>.blob.core.windows.net``;
   uses ``DefaultAzureCredential`` (run ``az login``).
3. Otherwise falls back to Azurite (``UseDevelopmentStorage=true``) for local dev.

Blob layout::

    container: agent-projects
      <project>/<environment>/agents/<name>.agent.md
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import ContainerClient

from .agent_md import parse_frontmatter

_DEV_CONNECTION = "UseDevelopmentStorage=true"


@dataclass
class AgentSummary:
    """Lightweight listing entry for an agent."""

    name: str
    display_name: str
    description: str
    trigger: str
    builtin_endpoints: bool
    last_modified: datetime | None
    size: int


class AgentExistsError(Exception):
    """Raised when creating an agent whose name already exists."""


class AgentNotFoundError(Exception):
    """Raised when an agent blob does not exist."""


def _container_name() -> str:
    return os.environ.get("PORTAL_CONTAINER", "agent-projects")


def _project() -> str:
    return os.environ.get("PORTAL_PROJECT", "default")


def _environment() -> str:
    return os.environ.get("PORTAL_ENVIRONMENT", "dev")


def _prefix() -> str:
    return f"{_project()}/{_environment()}/agents/"


def _blob_name(name: str) -> str:
    return f"{_prefix()}{name}.agent.md"


@lru_cache(maxsize=1)
def _container() -> ContainerClient:
    """Return a cached container client, creating the container if needed."""
    conn = os.environ.get("PORTAL_STORAGE_CONNECTION")
    account_url = os.environ.get("PORTAL_STORAGE_ACCOUNT_URL")

    if conn:
        client = ContainerClient.from_connection_string(conn, _container_name())
    elif account_url:
        from azure.identity import DefaultAzureCredential

        client = ContainerClient(
            account_url, _container_name(), credential=DefaultAzureCredential()
        )
    else:
        client = ContainerClient.from_connection_string(_DEV_CONNECTION, _container_name())

    try:
        client.create_container()
    except ResourceExistsError:
        pass
    return client


def storage_backend() -> str:
    """Return a human-readable description of the active storage backend."""
    if os.environ.get("PORTAL_STORAGE_CONNECTION"):
        return "connection-string"
    if url := os.environ.get("PORTAL_STORAGE_ACCOUNT_URL"):
        return f"identity:{url}"
    return "azurite (UseDevelopmentStorage=true)"


def list_agents() -> list[AgentSummary]:
    """List all agents in the current project/environment."""
    container = _container()
    summaries: list[AgentSummary] = []
    for blob in container.list_blobs(name_starts_with=_prefix()):
        if not blob.name.endswith(".agent.md"):
            continue
        name = blob.name[len(_prefix()) : -len(".agent.md")]
        try:
            text = container.download_blob(blob.name).readall().decode("utf-8")
        except ResourceNotFoundError:
            continue
        front, _ = parse_frontmatter(text)
        summaries.append(
            AgentSummary(
                name=name,
                display_name=str(front.get("name") or name),
                description=str(front.get("description") or ""),
                trigger=str((front.get("trigger") or {}).get("type", "http"))
                if isinstance(front.get("trigger"), dict)
                else "http",
                builtin_endpoints=bool(front.get("builtin_endpoints", False)),
                last_modified=blob.last_modified,
                size=blob.size,
            )
        )
    summaries.sort(key=lambda s: s.name)
    return summaries


def get_agent(name: str) -> str:
    """Return the raw ``*.agent.md`` content for ``name``."""
    container = _container()
    try:
        return container.download_blob(_blob_name(name)).readall().decode("utf-8")
    except ResourceNotFoundError as exc:
        raise AgentNotFoundError(name) from exc


def create_agent(name: str, content: str) -> None:
    """Create a new agent blob. Fails if it already exists."""
    container = _container()
    try:
        container.upload_blob(_blob_name(name), content.encode("utf-8"), overwrite=False)
    except ResourceExistsError as exc:
        raise AgentExistsError(name) from exc


def update_agent(name: str, content: str) -> None:
    """Overwrite an existing agent blob. Fails if it does not exist."""
    container = _container()
    if not exists(name):
        raise AgentNotFoundError(name)
    container.upload_blob(_blob_name(name), content.encode("utf-8"), overwrite=True)


def exists(name: str) -> bool:
    """Return True if an agent blob exists for ``name``."""
    return _container().get_blob_client(_blob_name(name)).exists()
