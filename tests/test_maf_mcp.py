from __future__ import annotations

import asyncio
import gc
import json
import logging
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import ClassVar
from unittest.mock import Mock
from weakref import ref

import httpx
import pytest
from agent_framework import MCPStreamableHTTPTool
from azure.core.credentials import AccessToken
from httpx import AsyncClient

import azure_functions_agents._mcp_auth as mcp_auth
from azure_functions_agents._mcp_auth import MCPHeaderProvider
from azure_functions_agents.discovery.mcp import (
    MCPServerDescriptor,
    clear_mcp_cache,
    discover_mcp_servers,
)
from azure_functions_agents.harness.agent_framework import _maf_mcp as maf_mcp

_SCOPE = "https://resource.example/.default"


class _CapturedMCPStreamableHTTPTool:
    def __init__(
        self,
        name: str,
        url: str,
        *,
        allowed_tools: list[str] | None,
        load_tools: bool,
        load_prompts: bool,
        approval_mode: str,
        header_provider: MCPHeaderProvider | None,
        http_client: AsyncClient | None = None,
    ) -> None:
        self.name = name
        self.url = url
        self.allowed_tools = allowed_tools
        self.load_tools = load_tools
        self.load_prompts = load_prompts
        self.approval_mode = approval_mode
        self.header_provider = header_provider
        self.http_client = http_client


@pytest.fixture(autouse=True)
def clear_adapter_cache() -> Iterator[None]:
    maf_mcp.clear_maf_mcp_cache()
    clear_mcp_cache()
    yield
    maf_mcp.clear_maf_mcp_cache()
    clear_mcp_cache()


@pytest.fixture
def capture_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        maf_mcp, "_OwnedHTTPClientMCPStreamableHTTPTool", _CapturedMCPStreamableHTTPTool
    )


def _server(
    *,
    tools: tuple[str, ...] | None = None,
    headers: dict[str, str] | None = None,
    scope: str | None = None,
) -> MCPServerDescriptor:
    return MCPServerDescriptor.create(
        name="demo",
        url="https://example.com/mcp",
        tools=tools,
        headers=headers or {},
        auth_scope=scope,
    )


@pytest.mark.parametrize("tools", [None, (), ("search_issues", "list_pull_requests")])
def test_adapter_preserves_tool_filters_and_noninteractive_defaults(
    tools: tuple[str, ...] | None, capture_tools: None
) -> None:
    server = _server(tools=tools)

    [tool] = maf_mcp.build_maf_mcp_tools([server])

    assert tool.name == server.name
    assert tool.url == server.url
    assert tool.allowed_tools == (list(tools) if tools is not None else None)
    assert tool.load_tools is True
    assert tool.load_prompts is False
    assert tool.approval_mode == "never_require"
    assert tool.header_provider is None
    assert tool.http_client is None


def test_adapter_builds_real_maf_wrapper_without_connecting() -> None:
    server = _server()

    [tool] = maf_mcp.build_maf_mcp_tools([server])

    assert isinstance(tool, MCPStreamableHTTPTool)
    assert tool.name == server.name
    assert tool.url == server.url
    assert tool.approval_mode == "never_require"


def test_adapter_returns_independent_lists_with_reused_wrappers(capture_tools: None) -> None:
    server = _server()
    first = maf_mcp.build_maf_mcp_tools([server])
    second = maf_mcp.build_maf_mcp_tools([server])

    assert first is not second
    assert first[0] is second[0]
    first.clear()
    assert len(maf_mcp.build_maf_mcp_tools([server])) == 1


def test_equal_descriptors_from_different_discoveries_do_not_share_wrappers(
    capture_tools: None,
) -> None:
    first_server = _server()
    second_server = _server()
    assert first_server == second_server

    [first] = maf_mcp.build_maf_mcp_tools([first_server])
    [second] = maf_mcp.build_maf_mcp_tools([second_server])

    assert first is not second


def test_wrapper_tool_filter_is_an_independent_copy(capture_tools: None) -> None:
    server = _server(tools=("search_issues",))
    [tool] = maf_mcp.build_maf_mcp_tools([server])
    tool.allowed_tools.append("changed")

    assert server.tools == ("search_issues",)


def test_clear_adapter_cache_rebuilds_wrappers(capture_tools: None) -> None:
    server = _server()
    [first] = maf_mcp.build_maf_mcp_tools([server])

    maf_mcp.clear_maf_mcp_cache()
    [second] = maf_mcp.build_maf_mcp_tools([server])

    assert first is not second


def test_adapter_cache_does_not_retain_discarded_descriptors(capture_tools: None) -> None:
    server = _server(headers={"X-Test": "yes"}, scope=_SCOPE)
    descriptor_ref = ref(server)
    [tool] = maf_mcp.build_maf_mcp_tools([server])

    del server
    gc.collect()

    assert descriptor_ref() is None
    assert maf_mcp._MAF_MCP_TOOLS_CACHE == {}
    assert tool.name == "demo"


def test_adapter_ignores_authored_load_flags(
    tmp_path: Path, capture_tools: None
) -> None:
    (tmp_path / "mcp.json").write_text(
        json.dumps(
            {
                "servers": {
                    "demo": {
                        "type": "http",
                        "url": "https://example.com/mcp",
                        "load_tools": False,
                        "load_prompts": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    servers = discover_mcp_servers(tmp_path).servers

    [tool] = maf_mcp.build_maf_mcp_tools(list(servers.values()))

    assert tool.load_tools is True
    assert tool.load_prompts is False


def test_adapter_construction_does_not_acquire_credentials(
    capture_tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    default_builder = Mock(side_effect=AssertionError("unexpected credential acquisition"))
    client_builder = Mock(side_effect=AssertionError("unexpected credential acquisition"))
    monkeypatch.setattr(mcp_auth, "build_credential", default_builder)
    monkeypatch.setattr(mcp_auth, "build_credential_with_client_id", client_builder)
    server = _server(scope=_SCOPE)

    [tool] = maf_mcp.build_maf_mcp_tools([server])

    assert tool.header_provider is not None
    assert tool.http_client is None
    default_builder.assert_not_called()
    client_builder.assert_not_called()


def test_authenticated_mcp_uses_sdk_header_provider_without_custom_http_client(
    capture_tools: None,
) -> None:
    server = _server(headers={"Authorization": "static-value", "X-Test": "yes"})
    [tool] = maf_mcp.build_maf_mcp_tools([server])
    assert tool.header_provider is not None
    assert tool.http_client is None
    assert tool.header_provider({}) == {"Authorization": "static-value", "X-Test": "yes"}

    auth_server = _server(scope=_SCOPE, headers={"Authorization": "static-value", "X-Test": "yes"})
    [auth_tool] = maf_mcp.build_maf_mcp_tools([auth_server])
    assert auth_tool.header_provider is not None
    assert auth_tool.http_client is None


def test_header_provider_refreshes_auth_at_expiry_offset(
    capture_tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential = Mock()
    credential.get_token.side_effect = [
        AccessToken("first-token", 1000),
        AccessToken("second-token", 2000),
    ]
    default_builder = Mock(return_value=credential)
    monkeypatch.setattr(mcp_auth, "build_credential", default_builder)
    monkeypatch.setattr(mcp_auth.time, "time", lambda: 100)
    server = _server(scope=_SCOPE, headers={"Authorization": "static-value", "X-Test": "yes"})
    [tool] = maf_mcp.build_maf_mcp_tools([server])
    assert tool.http_client is None
    assert tool.header_provider is not None
    default_builder.assert_not_called()

    assert tool.header_provider({}) == {"Authorization": "Bearer first-token", "X-Test": "yes"}
    monkeypatch.setattr(mcp_auth.time, "time", lambda: 699)
    assert tool.header_provider({}) == {"Authorization": "Bearer first-token", "X-Test": "yes"}
    monkeypatch.setattr(mcp_auth.time, "time", lambda: 700)
    assert tool.header_provider({}) == {"Authorization": "Bearer second-token", "X-Test": "yes"}

    assert credential.get_token.call_count == 2
    credential.get_token.assert_called_with(_SCOPE)
    default_builder.assert_called_once_with()


def test_header_provider_failure_sends_no_stale_header_or_retry(
    capture_tools: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    credential = Mock()
    credential.get_token.side_effect = [
        AccessToken("first-token", 1000),
        RuntimeError("test refresh failure"),
    ]
    monkeypatch.setattr(mcp_auth, "build_credential", lambda: credential)
    monkeypatch.setattr(mcp_auth.time, "time", lambda: 100)
    server = _server(scope=_SCOPE, headers={"Authorization": "static-value"})
    [tool] = maf_mcp.build_maf_mcp_tools([server])
    assert tool.http_client is None
    assert tool.header_provider is not None

    assert tool.header_provider({}) == {"Authorization": "Bearer first-token"}
    monkeypatch.setattr(mcp_auth.time, "time", lambda: 700)
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError, match="test refresh failure"):
        tool.header_provider({})

    assert credential.get_token.call_count == 2
    assert "first-token" not in caplog.text
    assert "static-value" not in caplog.text


class _CloseTrackingAsyncClient:
    instances: ClassVar[list[_CloseTrackingAsyncClient]] = []
    close_error: ClassVar[BaseException | None] = None

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.event_hooks = {"request": []}
        self.close_calls = 0
        self.instances.append(self)

    async def aclose(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


@pytest.mark.asyncio
async def test_owned_http_client_closes_with_sdk_exit_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent_framework._mcp as sdk_mcp

    _CloseTrackingAsyncClient.instances.clear()
    _CloseTrackingAsyncClient.close_error = None

    @asynccontextmanager
    async def fake_transport(*_args, **_kwargs):
        yield (object(), object(), lambda: None)

    monkeypatch.setattr(httpx, "AsyncClient", _CloseTrackingAsyncClient)
    monkeypatch.setattr(sdk_mcp, "streamable_http_client", fake_transport)
    [tool] = maf_mcp.build_maf_mcp_tools([_server(scope=_SCOPE)])

    await tool._exit_stack.enter_async_context(tool.get_mcp_client())
    client = tool._httpx_client
    assert isinstance(client, _CloseTrackingAsyncClient)
    assert client.close_calls == 0

    await tool._safe_close_exit_stack()

    assert client.close_calls == 1
    assert tool._httpx_client is None


@pytest.mark.asyncio
async def test_owned_http_client_closes_when_transport_connect_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_framework._mcp as sdk_mcp

    _CloseTrackingAsyncClient.instances.clear()
    _CloseTrackingAsyncClient.close_error = None

    @asynccontextmanager
    async def failing_transport(*_args, **_kwargs):
        raise RuntimeError("connect failed")
        yield

    monkeypatch.setattr(httpx, "AsyncClient", _CloseTrackingAsyncClient)
    monkeypatch.setattr(sdk_mcp, "streamable_http_client", failing_transport)
    [tool] = maf_mcp.build_maf_mcp_tools([_server(scope=_SCOPE)])

    with pytest.raises(RuntimeError, match="connect failed"):
        async with tool.get_mcp_client():
            pytest.fail("transport should not yield")

    [client] = _CloseTrackingAsyncClient.instances
    assert client.close_calls == 1
    assert tool._httpx_client is None


@pytest.mark.asyncio
async def test_owned_http_client_closes_when_owner_path_handles_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_framework._mcp as sdk_mcp

    _CloseTrackingAsyncClient.instances.clear()
    _CloseTrackingAsyncClient.close_error = None

    @asynccontextmanager
    async def fake_transport(*_args, **_kwargs):
        yield (object(), object(), lambda: None)

    monkeypatch.setattr(httpx, "AsyncClient", _CloseTrackingAsyncClient)
    monkeypatch.setattr(sdk_mcp, "streamable_http_client", fake_transport)
    [tool] = maf_mcp.build_maf_mcp_tools([_server(scope=_SCOPE)])

    with pytest.raises(asyncio.CancelledError):
        async with tool.get_mcp_client():
            client = tool._httpx_client
            assert isinstance(client, _CloseTrackingAsyncClient)
            raise asyncio.CancelledError

    [client] = _CloseTrackingAsyncClient.instances
    assert client.close_calls == 1
    assert tool._httpx_client is None


@pytest.mark.asyncio
async def test_owned_http_client_close_failure_surfaces_without_primary_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import agent_framework._mcp as sdk_mcp

    _CloseTrackingAsyncClient.instances.clear()
    _CloseTrackingAsyncClient.close_error = RuntimeError("close failed")

    @asynccontextmanager
    async def fake_transport(*_args, **_kwargs):
        yield (object(), object(), lambda: None)

    monkeypatch.setattr(httpx, "AsyncClient", _CloseTrackingAsyncClient)
    monkeypatch.setattr(sdk_mcp, "streamable_http_client", fake_transport)
    [tool] = maf_mcp.build_maf_mcp_tools([_server(scope=_SCOPE)])

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="close failed"):
        async with tool.get_mcp_client():
            pass

    [client] = _CloseTrackingAsyncClient.instances
    assert client.close_calls == 1
    assert tool._httpx_client is None
    assert "MAF MCP HTTP client cleanup failed." in caplog.text


@pytest.mark.asyncio
async def test_owned_http_client_close_failure_preserves_connect_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import agent_framework._mcp as sdk_mcp

    _CloseTrackingAsyncClient.instances.clear()
    _CloseTrackingAsyncClient.close_error = RuntimeError("close failed")

    @asynccontextmanager
    async def failing_transport(*_args, **_kwargs):
        raise RuntimeError("connect failed")
        yield

    monkeypatch.setattr(httpx, "AsyncClient", _CloseTrackingAsyncClient)
    monkeypatch.setattr(sdk_mcp, "streamable_http_client", failing_transport)
    [tool] = maf_mcp.build_maf_mcp_tools([_server(scope=_SCOPE)])

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="connect failed"):
        async with tool.get_mcp_client():
            pytest.fail("transport should not yield")

    [client] = _CloseTrackingAsyncClient.instances
    assert client.close_calls == 1
    assert tool._httpx_client is None
    assert "MAF MCP HTTP client cleanup failed." in caplog.text


@pytest.mark.asyncio
async def test_owned_http_client_close_failure_preserves_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import agent_framework._mcp as sdk_mcp

    _CloseTrackingAsyncClient.instances.clear()
    _CloseTrackingAsyncClient.close_error = RuntimeError("close failed")

    @asynccontextmanager
    async def fake_transport(*_args, **_kwargs):
        yield (object(), object(), lambda: None)

    monkeypatch.setattr(httpx, "AsyncClient", _CloseTrackingAsyncClient)
    monkeypatch.setattr(sdk_mcp, "streamable_http_client", fake_transport)
    [tool] = maf_mcp.build_maf_mcp_tools([_server(scope=_SCOPE)])

    with caplog.at_level(logging.ERROR), pytest.raises(asyncio.CancelledError):
        async with tool.get_mcp_client():
            raise asyncio.CancelledError

    [client] = _CloseTrackingAsyncClient.instances
    assert client.close_calls == 1
    assert tool._httpx_client is None
    assert "MAF MCP HTTP client cleanup failed." in caplog.text
