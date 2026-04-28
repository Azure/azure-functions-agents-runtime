from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

from .arm import ArmClient, DataPlaneClient
from .config import resolve_env_var
from .connectors import load_connection, is_v2_connection
from .connector_tools import generate_tools


class _ConnectorToolCache:
    """Lazy-init singleton cache for connector tools discovered from ARM API."""

    def __init__(self):
        self._tools: list | None = None
        self._arm: ArmClient | None = None
        self._data_plane: DataPlaneClient | None = None
        self._lock = asyncio.Lock()
        self._connection_specs: List[Dict[str, Any]] = []

    def add_connection_specs(self, specs: List[Dict[str, Any]]) -> None:
        """Append tools_from_connections specs from an agent file.

        Deduplicates by resolved connection_id so the same connector
        isn't loaded twice even if referenced from multiple agents.
        """
        if not specs:
            return
        existing_ids = {
            resolve_env_var(str(s.get("connection_id", "")))
            for s in self._connection_specs
        }
        for spec in specs:
            cid = resolve_env_var(str(spec.get("connection_id", "")))
            if cid and cid not in existing_ids:
                self._connection_specs.append(spec)
                existing_ids.add(cid)

    async def get_tools(self) -> list:
        """Return cached connector tools, discovering them on first call."""
        if self._tools is not None:
            return self._tools

        async with self._lock:
            # Double-check after acquiring lock
            if self._tools is not None:
                return self._tools

            if not self._connection_specs:
                self._tools = []
                return self._tools

            self._arm = ArmClient()
            all_tools = []

            # Check if any V2 connections need a data plane client
            has_v2 = any(
                is_v2_connection(resolve_env_var(str(s.get("connection_id", ""))))
                for s in self._connection_specs
            )
            if has_v2:
                self._data_plane = DataPlaneClient()

            for spec in self._connection_specs:
                raw_connection_id = spec.get("connection_id", "")
                if not raw_connection_id:
                    logging.warning("tools_from_connections entry missing 'connection_id', skipping")
                    continue

                connection_id = resolve_env_var(str(raw_connection_id))
                if not connection_id or connection_id.startswith("%") or connection_id.startswith("$"):
                    logging.warning(f"tools_from_connections: could not resolve connection_id '{raw_connection_id}', skipping")
                    continue

                try:
                    v2 = is_v2_connection(connection_id)
                    connection = await load_connection(
                        self._arm, connection_id,
                        data_plane_client=self._data_plane if v2 else None,
                    )

                    # Determine tool name prefix: explicit > connection name > api_name
                    prefix = spec.get("prefix")
                    if isinstance(prefix, str) and prefix.strip():
                        prefix = prefix.strip()
                    else:
                        prefix = None  # generate_tools will use connection.name

                    tools = generate_tools(
                        self._arm, connection, prefix=prefix,
                        data_plane_client=self._data_plane if v2 else None,
                    )
                    all_tools.extend(tools)
                    version_label = "V2" if v2 else "V1"
                    logging.info(
                        f"Connector tools discovered ({version_label}): {connection.display_name} ({connection.api_name}): "
                        f"{len(tools)} tools [{connection.status}]"
                    )
                    for tool in tools:
                        logging.info(f"  - {tool.name}: {tool.description[:100]}")
                except Exception as e:
                    logging.warning(f"Failed to load connector tools for '{connection_id}': {e}")

            self._tools = all_tools
            return self._tools


_cache = _ConnectorToolCache()


def configure_connector_tools(tools_from_connections: List[Dict[str, Any]]) -> None:
    """Add connector tool specs from an agent file to the global cache.

    Can be called multiple times (once per agent file). Specs are
    deduplicated by connection_id so the same connector isn't loaded twice.
    """
    _cache.add_connection_specs(tools_from_connections)


async def get_connector_tools() -> list:
    """Get cached connector tools (lazy-discovers on first call)."""
    return await _cache.get_tools()
