from __future__ import annotations

import gc
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock
from weakref import ref

import pytest
from agent_framework import MCPStreamableHTTPTool
from azure.core.credentials import AccessToken
from httpx import AsyncClient, MockTransport, Request, Response

import azure_functions_agents._maf_mcp as maf_mcp
import azure_functions_agents._mcp_auth as mcp_auth
from azure_functions_agents._mcp_auth import MCPHeaderProvider
from azure_functions_agents.discovery.mcp import (
    MCPServerDescriptor,
    clear_mcp_cache,
    discover_mcp_servers,
)

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
        http_client: AsyncClient | None,
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
    monkeypatch.setattr(maf_mcp, "MCPStreamableHTTPTool", _CapturedMCPStreamableHTTPTool)


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


def test_adapter_cache_does_not_retain_discarded_descriptors(
    capture_tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(maf_mcp, "_build_http_client", lambda _provider: None)
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
    monkeypatch.setattr(maf_mcp, "_build_http_client", lambda _provider: None)
    server = _server(scope=_SCOPE)

    [tool] = maf_mcp.build_maf_mcp_tools([server])

    assert tool.header_provider is not None
    default_builder.assert_not_called()
    client_builder.assert_not_called()


@pytest.mark.asyncio
async def test_httpx_request_hook_preserves_static_headers(
    capture_tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[Request] = []

    def record_request(request: Request) -> Response:
        requests.append(request)
        return Response(200)

    client_factory = Mock(
        side_effect=lambda **kwargs: AsyncClient(transport=MockTransport(record_request), **kwargs)
    )
    monkeypatch.setattr(maf_mcp, "AsyncClient", client_factory)
    server = _server(headers={"Authorization": "static-value", "X-Test": "yes"})
    [tool] = maf_mcp.build_maf_mcp_tools([server])
    assert tool.header_provider is not None
    assert tool.http_client is not None
    assert tool.header_provider({}) == {"Authorization": "static-value", "X-Test": "yes"}

    async with tool.http_client as client:
        await client.get(server.url)

    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == "static-value"
    assert requests[0].headers["X-Test"] == "yes"
    assert client_factory.call_args.kwargs["follow_redirects"] is True
    assert len(client_factory.call_args.kwargs["event_hooks"]["request"]) == 1


@pytest.mark.asyncio
async def test_httpx_request_hook_refreshes_auth_at_expiry_offset(
    capture_tools: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    authorizations: list[str] = []

    def record_request(request: Request) -> Response:
        authorizations.append(request.headers["Authorization"])
        assert request.headers["X-Test"] == "yes"
        return Response(200)

    monkeypatch.setattr(
        maf_mcp,
        "AsyncClient",
        lambda **kwargs: AsyncClient(transport=MockTransport(record_request), **kwargs),
    )
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
    assert tool.http_client is not None
    default_builder.assert_not_called()

    async with tool.http_client as client:
        await client.get(server.url)
        monkeypatch.setattr(mcp_auth.time, "time", lambda: 699)
        await client.get(server.url)
        monkeypatch.setattr(mcp_auth.time, "time", lambda: 700)
        await client.get(server.url)

    assert authorizations == ["Bearer first-token", "Bearer first-token", "Bearer second-token"]
    assert credential.get_token.call_count == 2
    credential.get_token.assert_called_with(_SCOPE)
    default_builder.assert_called_once_with()


@pytest.mark.asyncio
async def test_request_hook_failure_sends_no_stale_header_or_retry(
    capture_tools: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    authorizations: list[str] = []

    def record_request(request: Request) -> Response:
        authorizations.append(request.headers["Authorization"])
        return Response(200)

    monkeypatch.setattr(
        maf_mcp,
        "AsyncClient",
        lambda **kwargs: AsyncClient(transport=MockTransport(record_request), **kwargs),
    )
    credential = Mock()
    credential.get_token.side_effect = [
        AccessToken("first-token", 1000),
        RuntimeError("test refresh failure"),
    ]
    monkeypatch.setattr(mcp_auth, "build_credential", lambda: credential)
    monkeypatch.setattr(mcp_auth.time, "time", lambda: 100)
    server = _server(scope=_SCOPE, headers={"Authorization": "static-value"})
    [tool] = maf_mcp.build_maf_mcp_tools([server])
    assert tool.http_client is not None

    async with tool.http_client as client:
        await client.get(server.url)
        monkeypatch.setattr(mcp_auth.time, "time", lambda: 700)
        with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError, match="test refresh failure"):
            await client.get(server.url)

    assert authorizations == ["Bearer first-token"]
    assert credential.get_token.call_count == 2
    assert "first-token" not in caplog.text
    assert "static-value" not in caplog.text
