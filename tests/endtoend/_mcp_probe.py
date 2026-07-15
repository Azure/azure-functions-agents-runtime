"""Minimal MCP client helpers for E2E tests.

Agents with ``builtin_endpoints.mcp: true`` are exposed as tools on the Azure
Functions MCP extension, served at ``/runtime/webhooks/mcp`` (streamable HTTP)
with an SSE fallback at ``/runtime/webhooks/mcp/sse``. Locally the Functions
host does not enforce the MCP system key, so a plain MCP client can connect.

These helpers wrap the async ``mcp`` SDK in blocking functions (each opens a
fresh event loop via ``asyncio.run``) so the synchronous E2E tests can list and
call MCP tools without managing an event loop or ``pytest-asyncio`` fixtures.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError

MCP_WEBHOOK_PATH = "/runtime/webhooks/mcp"


@dataclass(frozen=True)
class McpToolInfo:
    """A tool advertised by the MCP server's ``tools/list`` response."""

    name: str
    description: str | None
    input_schema: dict[str, Any]

    def required_properties(self) -> list[str]:
        required = self.input_schema.get("required", [])
        return [str(item) for item in required] if isinstance(required, list) else []

    def property_names(self) -> list[str]:
        props = self.input_schema.get("properties", {})
        return list(props) if isinstance(props, dict) else []


@dataclass(frozen=True)
class McpCallResult:
    """The result of an MCP ``tools/call`` invocation."""

    is_error: bool
    text: str

    def json(self) -> Any:
        return json.loads(self.text)


async def _open_and_run[T](
    base_url: str,
    op: Callable[[ClientSession], Awaitable[T]],
) -> T:
    """Connect to the MCP endpoint and run ``op`` against an initialized session.

    Tries the streamable-HTTP transport first, then falls back to SSE, so the
    helper works across Functions MCP extension versions.
    """
    errors: list[str] = []

    http_url = f"{base_url}{MCP_WEBHOOK_PATH}"
    try:
        async with (
            streamable_http_client(http_url) as (read, write, _get_session_id),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return await op(session)
    except Exception as exc:  # fall back to SSE transport
        errors.append(f"streamable_http: {type(exc).__name__}: {exc}")

    sse_url = f"{base_url}{MCP_WEBHOOK_PATH}/sse"
    try:
        async with (
            sse_client(sse_url) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return await op(session)
    except Exception as exc:  # surface both transport failures
        errors.append(f"sse: {type(exc).__name__}: {exc}")

    raise RuntimeError("MCP connect failed on all transports:\n  " + "\n  ".join(errors))


def _run_blocking[T](coro: Awaitable[T], *, timeout: float) -> T:
    async def _guarded() -> T:
        return await asyncio.wait_for(coro, timeout=timeout)

    return asyncio.run(_guarded())


def list_mcp_tools(
    base_url: str,
    *,
    timeout: float = 30.0,
    attempts: int = 5,
    retry_delay: float = 1.0,
) -> list[McpToolInfo]:
    """Return the tools advertised by the MCP server.

    Retries the connection a few times because the MCP webhook can lag slightly
    behind the admin API becoming responsive after ``func start``.
    """

    async def op(session: ClientSession) -> list[McpToolInfo]:
        result = await session.list_tools()
        return [
            McpToolInfo(
                name=tool.name,
                description=tool.description,
                input_schema=dict(tool.inputSchema or {}),
            )
            for tool in result.tools
        ]

    return _with_retries(
        lambda: _run_blocking(_open_and_run(base_url, op), timeout=timeout),
        attempts=attempts,
        retry_delay=retry_delay,
    )


def call_mcp_tool(
    base_url: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    timeout: float = 120.0,
    attempts: int = 5,
    retry_delay: float = 1.0,
) -> McpCallResult:
    """Invoke ``tool_name`` and return its (text) result."""

    async def op(session: ClientSession) -> McpCallResult:
        try:
            result = await session.call_tool(tool_name, arguments)
        except McpError as exc:
            # A JSON-RPC protocol error (e.g. a missing required argument the
            # extension rejects before running the tool) is a call outcome, not
            # a transport failure: represent it as an errored result.
            return McpCallResult(is_error=True, text=str(exc))
        text = "".join(
            block.text
            for block in result.content
            if getattr(block, "type", None) == "text" and hasattr(block, "text")
        )
        return McpCallResult(is_error=bool(result.isError), text=text)

    return _with_retries(
        lambda: _run_blocking(_open_and_run(base_url, op), timeout=timeout),
        attempts=attempts,
        retry_delay=retry_delay,
    )


def _with_retries[T](fn: Callable[[], T], *, attempts: int, retry_delay: float) -> T:
    import time

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # retry transient MCP connect errors
            last_error = exc
            if attempt < attempts:
                time.sleep(retry_delay)
    assert last_error is not None
    raise last_error
