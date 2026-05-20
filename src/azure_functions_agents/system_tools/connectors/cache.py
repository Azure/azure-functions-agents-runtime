from __future__ import annotations

import asyncio
from typing import Any

from ..._function_tool import FunctionTool
from ..._logger import logger
from ...config.env import substitute_env_vars_in_value
from .arm import ArmClient, DataPlaneClient
from .connectors import is_v2_connection, load_connection
from .tools import generate_tools

ConnectionSpec = dict[str, Any]


class _ConnectorToolCache:
    """Lazy-init singleton cache for connector tools discovered from ARM API."""

    def __init__(self) -> None:
        self._tools: list[FunctionTool] | None = None
        self._arm: ArmClient | None = None
        self._data_plane: DataPlaneClient | None = None
        self._lock = asyncio.Lock()
        self._connection_specs: list[ConnectionSpec] = []

    def add_connection_specs(self, specs: list[ConnectionSpec]) -> None:
        """Append tools_from_connections specs from an agent file.

        Deduplicates by resolved connection_id so the same connector
        isn't loaded twice even if referenced from multiple agents.
        """
        if not specs:
            return
        existing_ids = {
            substitute_env_vars_in_value(str(spec.get("connection_id", "")))
            for spec in self._connection_specs
        }
        for spec in specs:
            connection_id = substitute_env_vars_in_value(str(spec.get("connection_id", "")))
            if connection_id and connection_id not in existing_ids:
                self._connection_specs.append(spec)
                existing_ids.add(connection_id)

    async def get_tools(self) -> list[FunctionTool]:
        """Return cached connector tools, discovering them on first call."""
        if self._tools is not None:
            return self._tools

        async with self._lock:
            if self._tools is not None:
                return self._tools

            if not self._connection_specs:
                self._tools = []
                return self._tools

            self._arm = ArmClient()
            all_tools: list[FunctionTool] = []

            has_v2 = any(
                is_v2_connection(
                    substitute_env_vars_in_value(str(spec.get("connection_id", "")))
                )
                for spec in self._connection_specs
            )
            if has_v2:
                self._data_plane = DataPlaneClient()

            for spec in self._connection_specs:
                raw_connection_id = spec.get("connection_id", "")
                if not raw_connection_id:
                    logger.warning("tools_from_connections entry missing 'connection_id', skipping")
                    continue

                connection_id = substitute_env_vars_in_value(str(raw_connection_id))
                if (
                    not connection_id
                    or connection_id.startswith("%")
                    or connection_id.startswith("$")
                ):
                    logger.warning(
                        "tools_from_connections: could not resolve connection_id '%s', skipping",
                        raw_connection_id,
                    )
                    continue
                if connection_id.lower().startswith(("http://", "https://")):
                    logger.warning(
                        "tools_from_connections: connection_id must be an ARM resource ID "
                        "(e.g. /subscriptions/.../providers/Microsoft.Web/connections/...), "
                        "got URL '%s'. Skipping.",
                        connection_id,
                    )
                    continue

                try:
                    is_v2 = is_v2_connection(connection_id)
                    connection = await load_connection(
                        self._arm,
                        connection_id,
                        data_plane_client=self._data_plane if is_v2 else None,
                    )

                    prefix = spec.get("prefix")
                    prefix = prefix.strip() if isinstance(prefix, str) and prefix.strip() else None

                    tools = generate_tools(
                        self._arm,
                        connection,
                        prefix=prefix,
                        data_plane_client=self._data_plane if is_v2 else None,
                    )
                    all_tools.extend(tools)
                    version_label = "V2" if is_v2 else "V1"
                    logger.info(
                        "Connector tools discovered (%s): %s (%s): %d tools [%s]",
                        version_label,
                        connection.display_name,
                        connection.api_name,
                        len(tools),
                        connection.status,
                    )
                    for tool in tools:
                        logger.info("  - %s: %s", tool.name, tool.description[:100])
                except Exception as exc:
                    logger.warning(
                        "Failed to load connector tools for '%s': %s",
                        connection_id,
                        exc,
                    )

            self._tools = all_tools
            return self._tools


_cache = _ConnectorToolCache()


def configure_connector_tools(tools_from_connections: list[ConnectionSpec]) -> None:
    """Add connector tool specs from an agent file to the global cache.

    Can be called multiple times (once per agent file). Specs are
    deduplicated by connection_id so the same connector isn't loaded twice.
    """
    _cache.add_connection_specs(tools_from_connections)


async def get_connector_tools() -> list[FunctionTool]:
    """Get cached connector tools (lazy-discovers on first call)."""
    return await _cache.get_tools()
