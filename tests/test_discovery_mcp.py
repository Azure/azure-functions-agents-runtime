from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_framework import MCPStreamableHTTPTool

import azure_functions_agents.discovery.mcp as mcp_discovery
from azure_functions_agents.discovery.mcp import clear_mcp_cache, discover_mcp_servers


@pytest.fixture(autouse=True)
def clear_discovery_cache() -> None:
    clear_mcp_cache()
    yield
    clear_mcp_cache()


def _write_mcp_config(
    app_root: Path, server_config: dict[str, object] | None = None
) -> None:
    (app_root / "mcp.json").write_text(
        json.dumps(
            {
                "servers": {
                    "demo": server_config
                    or {
                        "type": "http",
                        "url": "https://example.com/mcp",
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def _write_mcp_json(app_root: Path, data: dict[str, object]) -> None:
    config_path = app_root / "mcp.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")


class _CapturedMCPStreamableHTTPTool:
    def __init__(
        self,
        name: str,
        url: str,
        *,
        allowed_tools: list[str] | None = None,
        load_tools: bool = True,
        load_prompts: bool = True,
        header_provider: object = None,
        http_client: object = None,
        **_: object,
    ) -> None:
        self.name = name
        self.url = url
        self.allowed_tools = allowed_tools
        self.load_tools = load_tools
        self.load_prompts = load_prompts
        self.header_provider = header_provider
        self.http_client = http_client


def test_discover_mcp_servers_caches_by_resolved_app_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_mcp_config(tmp_path)

    target_path = (tmp_path / "mcp.json").resolve()
    read_count = 0
    original_read_text = Path.read_text

    def counting_read_text(self: Path, *args: object, **kwargs: object) -> str:
        nonlocal read_count
        if self.resolve() == target_path:
            read_count += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    first = discover_mcp_servers(tmp_path)
    second = discover_mcp_servers(tmp_path / ".")

    assert list(first.servers) == ["demo"]
    assert list(second.servers) == ["demo"]
    assert read_count == 1


def test_discover_mcp_servers_returns_independent_dicts(tmp_path: Path) -> None:
    _write_mcp_config(tmp_path)

    first_result = discover_mcp_servers(tmp_path)
    first_result.servers["extra"] = first_result.servers["demo"]

    second_result = discover_mcp_servers(tmp_path)

    assert list(second_result.servers) == ["demo"]


def test_clear_mcp_cache_reruns_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_mcp_config(tmp_path)

    target_path = (tmp_path / "mcp.json").resolve()
    read_count = 0
    original_read_text = Path.read_text

    def counting_read_text(self: Path, *args: object, **kwargs: object) -> str:
        nonlocal read_count
        if self.resolve() == target_path:
            read_count += 1
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    discover_mcp_servers(tmp_path)
    clear_mcp_cache()
    discover_mcp_servers(tmp_path)

    assert read_count == 2


def test_discover_mcp_servers_handles_top_level_list(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text("[1, 2, 3]", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        result = discover_mcp_servers(tmp_path)

    assert result.servers == {}
    assert any(
        record.levelno == logging.WARNING
        and record.getMessage()
        == f"Ignoring {config_path}: expected a JSON object at the top level, got list."
        for record in caplog.records
    )


def test_discover_mcp_servers_handles_top_level_string(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(json.dumps("hello"), encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        result = discover_mcp_servers(tmp_path)

    assert result.servers == {}
    assert any(
        record.levelno == logging.WARNING
        and record.getMessage()
        == f"Ignoring {config_path}: expected a JSON object at the top level, got str."
        for record in caplog.records
    )


def test_discover_mcp_servers_skips_stdio_command_config(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_mcp_config(
        tmp_path,
        {
            "command": "python",
            "args": ["-m", "demo_server"],
        },
    )

    with caplog.at_level(logging.WARNING):
        result = discover_mcp_servers(tmp_path)

    assert result.servers == {}
    assert len(result.failed_loads) == 1
    assert "demo" in result.failed_loads[0][0]
    assert "stdio" in result.failed_loads[0][1].lower()
    assert any(
        record.levelno == logging.WARNING
        and record.getMessage()
        == "MCP stdio transport is not supported; skipping server 'demo'"
        for record in caplog.records
    )


def test_discover_mcp_servers_skips_sse_config(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_mcp_config(
        tmp_path,
        {
            "type": "sse",
            "url": "https://example.com/mcp",
        },
    )

    with caplog.at_level(logging.WARNING):
        result = discover_mcp_servers(tmp_path)

    assert result.servers == {}
    assert len(result.failed_loads) == 1
    assert "demo" in result.failed_loads[0][0]
    assert "sse" in result.failed_loads[0][1].lower()
    assert any(
        record.levelno == logging.WARNING
        and record.getMessage()
        == "MCP server 'demo': unknown server type 'sse'; supported types are 'http' and 'streamable-http'"
        for record in caplog.records
    )


def test_discover_mcp_servers_supports_streamable_http(tmp_path: Path) -> None:
    _write_mcp_config(
        tmp_path,
        {
            "type": "streamable-http",
            "url": "https://example.com/mcp",
        },
    )

    discovered_servers = discover_mcp_servers(tmp_path)

    assert list(discovered_servers.servers) == ["demo"]
    assert isinstance(discovered_servers.servers["demo"], MCPStreamableHTTPTool)


def test_discover_mcp_servers_accepts_url_without_type(tmp_path: Path) -> None:
    _write_mcp_config(
        tmp_path,
        {"url": "https://example.com/mcp"},
    )

    discovered_servers = discover_mcp_servers(tmp_path)

    assert list(discovered_servers.servers) == ["demo"]
    assert isinstance(discovered_servers.servers["demo"], MCPStreamableHTTPTool)


def test_discover_mcp_servers_skips_http_type_missing_url(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_mcp_config(tmp_path, {"type": "http"})

    with caplog.at_level(logging.WARNING):
        discovered_servers = discover_mcp_servers(tmp_path)

    assert discovered_servers.servers == {}
    assert any(
        record.levelno == logging.WARNING
        and record.getMessage() == "MCP server 'demo': missing 'url', skipping"
        for record in caplog.records
    )


def test_discover_mcp_servers_ignores_vscode_mcp_json(tmp_path: Path) -> None:
    (tmp_path / ".vscode").mkdir()
    (tmp_path / ".vscode" / "mcp.json").write_text(
        json.dumps(
            {
                "servers": {
                    "demo": {"type": "http", "url": "https://example.com/mcp"}
                }
            }
        ),
        encoding="utf-8",
    )

    discovered_servers = discover_mcp_servers(tmp_path)

    assert discovered_servers.servers == {}


def test_discover_substitutes_dollar_in_http_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_HOST", "example.com")
    _write_mcp_json(
        tmp_path,
        {
            "servers": {
                "demo": {
                    "type": "http",
                    "url": "https://$MCP_HOST/api",
                }
            }
        },
    )

    result = discover_mcp_servers(tmp_path)
    tool = result.servers["demo"]

    assert isinstance(tool, MCPStreamableHTTPTool)
    assert tool.url == "https://example.com/api"


def test_discover_substitutes_inline_in_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKEN", "abc123")
    monkeypatch.setattr(
        mcp_discovery, "MCPStreamableHTTPTool", _CapturedMCPStreamableHTTPTool
    )
    _write_mcp_json(
        tmp_path,
        {
            "servers": {
                "demo": {
                    "type": "http",
                    "url": "https://example.com/api",
                    "headers": {"Authorization": "Bearer $TOKEN"},
                }
            }
        },
    )

    result = discover_mcp_servers(tmp_path)
    tool = result.servers["demo"]

    assert isinstance(tool, _CapturedMCPStreamableHTTPTool)
    assert tool.header_provider is not None
    assert tool.http_client is not None
    assert tool.header_provider(None) == {"Authorization": "Bearer abc123"}


def test_discover_undefined_variable_stays_literal(tmp_path: Path) -> None:
    _write_mcp_json(
        tmp_path,
        {"servers": {"demo": {"type": "http", "url": "https://$MISSING_VAR/api"}}},
    )

    discovered_servers = discover_mcp_servers(tmp_path)

    assert discovered_servers.servers == {}


def test_discover_mcp_servers_supports_auth_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeCredential:
        def __init__(self) -> None:
            self.calls = 0

        def get_token(self, scope: str) -> SimpleNamespace:
            self.calls += 1
            assert scope == "https://apihub.azure.com/.default"
            return SimpleNamespace(token=f"token-{self.calls}", expires_on=9999999999)

    credential = FakeCredential()
    monkeypatch.setattr(mcp_discovery, "build_credential", lambda: credential)
    monkeypatch.setattr(
        mcp_discovery, "MCPStreamableHTTPTool", _CapturedMCPStreamableHTTPTool
    )
    _write_mcp_json(
        tmp_path,
        {
            "servers": {
                "office365": {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "headers": {"X-Test": "yes"},
                    "auth": {
                        "scope": "https://apihub.azure.com/.default",
                    },
                }
            }
        },
    )

    result = discover_mcp_servers(tmp_path)
    tool = result.servers["office365"]

    assert isinstance(tool, _CapturedMCPStreamableHTTPTool)
    assert tool.header_provider is not None
    assert tool.http_client is not None
    assert tool.header_provider(None) == {
        "Authorization": "Bearer token-1",
        "X-Test": "yes",
    }
    assert tool.header_provider(None) == {
        "Authorization": "Bearer token-1",
        "X-Test": "yes",
    }
    assert credential.calls == 1


def test_discover_mcp_servers_auth_without_scope_uses_static_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        mcp_discovery, "MCPStreamableHTTPTool", _CapturedMCPStreamableHTTPTool
    )
    _write_mcp_json(
        tmp_path,
        {
            "servers": {
                "office365": {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "headers": {"X-Test": "yes"},
                    "auth": {},
                }
            }
        },
    )

    with caplog.at_level(logging.WARNING):
        result = discover_mcp_servers(tmp_path)
    tool = result.servers["office365"]

    assert isinstance(tool, _CapturedMCPStreamableHTTPTool)
    assert tool.header_provider is not None
    assert tool.header_provider(None) == {"X-Test": "yes"}
    assert "requires a non-empty 'scope'" in caplog.text


def test_discover_mcp_servers_supports_auth_client_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeCredential:
        def get_token(self, scope: str) -> SimpleNamespace:
            assert scope == "https://apihub.azure.com/.default"
            return SimpleNamespace(token="client-token", expires_on=9999999999)

    captured_client_ids: list[str | None] = []

    def fake_build_credential_with_client_id(client_id: str | None) -> FakeCredential:
        captured_client_ids.append(client_id)
        return FakeCredential()

    monkeypatch.setattr(
        mcp_discovery,
        "build_credential_with_client_id",
        fake_build_credential_with_client_id,
    )
    monkeypatch.setattr(
        mcp_discovery, "MCPStreamableHTTPTool", _CapturedMCPStreamableHTTPTool
    )
    _write_mcp_json(
        tmp_path,
        {
            "servers": {
                "office365": {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "auth": {
                        "scope": "https://apihub.azure.com/.default",
                        "client_id": "client-123",
                    },
                }
            }
        },
    )

    result = discover_mcp_servers(tmp_path)
    tool = result.servers["office365"]

    assert isinstance(tool, _CapturedMCPStreamableHTTPTool)
    assert tool.header_provider is not None
    assert tool.header_provider(None) == {"Authorization": "Bearer client-token"}
    assert captured_client_ids == ["client-123"]


def test_discover_mcp_servers_ignores_unresolved_auth_client_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeCredential:
        def get_token(self, scope: str) -> SimpleNamespace:
            assert scope == "https://apihub.azure.com/.default"
            return SimpleNamespace(token="fallback-token", expires_on=9999999999)

    monkeypatch.setattr(mcp_discovery, "build_credential", lambda: FakeCredential())
    monkeypatch.setattr(
        mcp_discovery,
        "build_credential_with_client_id",
        lambda client_id: pytest.fail(f"unexpected client id credential: {client_id}"),
    )
    monkeypatch.setattr(
        mcp_discovery, "MCPStreamableHTTPTool", _CapturedMCPStreamableHTTPTool
    )
    _write_mcp_json(
        tmp_path,
        {
            "servers": {
                "office365": {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "auth": {
                        "scope": "https://apihub.azure.com/.default",
                        "client_id": "$O365_MCP_CLIENT_ID",
                    },
                }
            }
        },
    )

    result = discover_mcp_servers(tmp_path)
    tool = result.servers["office365"]

    assert isinstance(tool, _CapturedMCPStreamableHTTPTool)
    assert tool.header_provider is not None
    assert tool.header_provider(None) == {"Authorization": "Bearer fallback-token"}


def test_discover_mcp_servers_ignores_load_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        mcp_discovery, "MCPStreamableHTTPTool", _CapturedMCPStreamableHTTPTool
    )
    _write_mcp_json(
        tmp_path,
        {
            "servers": {
                "demo": {
                    "type": "http",
                    "url": "https://example.com/mcp",
                    "load_tools": False,
                    "load_prompts": False,
                }
            }
        },
    )

    result = discover_mcp_servers(tmp_path)
    tool = result.servers["demo"]

    assert isinstance(tool, _CapturedMCPStreamableHTTPTool)
    assert tool.load_tools is True
    assert tool.load_prompts is False


def test_discover_does_not_substitute_server_name_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEYNAME", "substituted")
    _write_mcp_json(
        tmp_path,
        {"servers": {"$KEYNAME": {"type": "http", "url": "https://example.com/api"}}},
    )

    discovered_servers = discover_mcp_servers(tmp_path)

    assert list(discovered_servers.servers) == ["$KEYNAME"]
    assert isinstance(discovered_servers.servers["$KEYNAME"], MCPStreamableHTTPTool)


def test_discover_does_not_substitute_header_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HEADERKEY", "substituted")
    monkeypatch.setattr(
        mcp_discovery, "MCPStreamableHTTPTool", _CapturedMCPStreamableHTTPTool
    )
    _write_mcp_json(
        tmp_path,
        {
            "servers": {
                "demo": {
                    "type": "http",
                    "url": "https://example.com/api",
                    "headers": {"$HEADERKEY": "value"},
                }
            }
        },
    )

    result = discover_mcp_servers(tmp_path)
    tool = result.servers["demo"]

    assert isinstance(tool, _CapturedMCPStreamableHTTPTool)
    assert tool.header_provider is not None
    assert tool.header_provider(None) == {"$HEADERKEY": "value"}


def test_discover_inline_mix_in_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOST", "example.com")
    monkeypatch.setenv("PORT", "8080")
    _write_mcp_json(
        tmp_path,
        {
            "servers": {
                "demo": {
                    "type": "http",
                    "url": "https://$HOST:$PORT/api",
                }
            }
        },
    )

    result = discover_mcp_servers(tmp_path)
    tool = result.servers["demo"]

    assert isinstance(tool, MCPStreamableHTTPTool)
    assert tool.url == "https://example.com:8080/api"
