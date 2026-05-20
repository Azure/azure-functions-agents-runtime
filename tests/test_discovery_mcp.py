from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from agent_framework import MCPStdioTool, MCPStreamableHTTPTool

import azure_functions_agents.discovery.mcp as mcp_discovery
from azure_functions_agents.discovery.mcp import clear_mcp_cache, discover_mcp_servers


@pytest.fixture(autouse=True)
def clear_discovery_cache() -> None:
    clear_mcp_cache()
    yield
    clear_mcp_cache()


def _write_mcp_config(app_root: Path) -> None:
    _write_mcp_json(
        app_root,
        {
            "servers": {
                "demo": {
                    "command": "python",
                    "args": ["-m", "demo_server"],
                }
            }
        },
    )


def _write_mcp_json(
    app_root: Path, data: dict[str, object], *, vscode: bool = True
) -> None:
    if vscode:
        (app_root / ".vscode").mkdir(exist_ok=True)
        config_path = app_root / ".vscode" / "mcp.json"
    else:
        config_path = app_root / "mcp.json"
    config_path.write_text(json.dumps(data), encoding="utf-8")


class _CapturedMCPStreamableHTTPTool:
    def __init__(
        self,
        name: str,
        url: str,
        *,
        allowed_tools: list[str] | None = None,
        header_provider: object = None,
        **_: object,
    ) -> None:
        self.name = name
        self.url = url
        self.allowed_tools = allowed_tools
        self.header_provider = header_provider


def test_discover_mcp_servers_caches_by_resolved_app_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_mcp_config(tmp_path)

    target_path = (tmp_path / ".vscode" / "mcp.json").resolve()
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

    assert list(first) == ["demo"]
    assert list(second) == ["demo"]
    assert read_count == 1


def test_discover_mcp_servers_returns_independent_dicts(tmp_path: Path) -> None:
    _write_mcp_config(tmp_path)

    discovered_servers = discover_mcp_servers(tmp_path)
    discovered_servers["extra"] = discovered_servers["demo"]

    subsequent_servers = discover_mcp_servers(tmp_path)

    assert list(subsequent_servers) == ["demo"]


def test_clear_mcp_cache_reruns_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_mcp_config(tmp_path)

    target_path = (tmp_path / ".vscode" / "mcp.json").resolve()
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
    (tmp_path / ".vscode").mkdir()
    config_path = tmp_path / ".vscode" / "mcp.json"
    config_path.write_text("[1, 2, 3]", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        discovered_servers = discover_mcp_servers(tmp_path)

    assert discovered_servers == {}
    assert any(
        record.levelno == logging.WARNING
        and record.getMessage()
        == f"Ignoring {config_path}: expected a JSON object at the top level, got list."
        for record in caplog.records
    )


def test_discover_mcp_servers_handles_top_level_string(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / ".vscode").mkdir()
    config_path = tmp_path / ".vscode" / "mcp.json"
    config_path.write_text(json.dumps("hello"), encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        discovered_servers = discover_mcp_servers(tmp_path)

    assert discovered_servers == {}
    assert any(
        record.levelno == logging.WARNING
        and record.getMessage()
        == f"Ignoring {config_path}: expected a JSON object at the top level, got str."
        for record in caplog.records
    )


def test_discover_substitutes_dollar_in_stdio_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_COMMAND_PATH", "/usr/local/bin/server")
    _write_mcp_json(
        tmp_path,
        {"servers": {"demo": {"command": "$MCP_COMMAND_PATH"}}},
        vscode=False,
    )

    tool = discover_mcp_servers(tmp_path)["demo"]

    assert isinstance(tool, MCPStdioTool)
    assert tool.command == "/usr/local/bin/server"


def test_discover_substitutes_dollar_and_percent_in_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARG_A", "alpha")
    monkeypatch.setenv("ARG_B", "beta")
    _write_mcp_json(
        tmp_path,
        {
            "servers": {
                "demo": {
                    "command": "cmd",
                    "args": ["$ARG_A", "%ARG_B%", "literal"],
                }
            }
        },
        vscode=False,
    )

    tool = discover_mcp_servers(tmp_path)["demo"]

    assert isinstance(tool, MCPStdioTool)
    assert tool.args == ["alpha", "beta", "literal"]


def test_discover_substitutes_in_env_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_KEY", "secret")
    _write_mcp_json(
        tmp_path,
        {
            "servers": {
                "demo": {
                    "command": "cmd",
                    "env": {"KEY": "$API_KEY", "OTHER": "literal"},
                }
            }
        },
        vscode=False,
    )

    tool = discover_mcp_servers(tmp_path)["demo"]

    assert isinstance(tool, MCPStdioTool)
    assert tool.env == {"KEY": "secret", "OTHER": "literal"}


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
        vscode=False,
    )

    tool = discover_mcp_servers(tmp_path)["demo"]

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
        vscode=False,
    )

    tool = discover_mcp_servers(tmp_path)["demo"]

    assert isinstance(tool, _CapturedMCPStreamableHTTPTool)
    assert tool.header_provider is not None
    assert tool.header_provider(None) == {"Authorization": "Bearer abc123"}


def test_discover_substitution_works_in_vscode_mcp_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("URL", "https://vscode.example.com")
    _write_mcp_json(
        tmp_path,
        {"servers": {"demo": {"type": "http", "url": "$URL"}}},
    )

    tool = discover_mcp_servers(tmp_path)["demo"]

    assert isinstance(tool, MCPStreamableHTTPTool)
    assert tool.url == "https://vscode.example.com"


def test_discover_undefined_variable_stays_literal(tmp_path: Path) -> None:
    _write_mcp_json(
        tmp_path,
        {"servers": {"demo": {"command": "$MISSING_VAR"}}},
        vscode=False,
    )

    tool = discover_mcp_servers(tmp_path)["demo"]

    assert isinstance(tool, MCPStdioTool)
    assert tool.command == "$MISSING_VAR"


def test_discover_does_not_substitute_server_name_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEYNAME", "substituted")
    _write_mcp_json(
        tmp_path,
        {"servers": {"$KEYNAME": {"command": "cmd"}}},
        vscode=False,
    )

    discovered_servers = discover_mcp_servers(tmp_path)

    assert list(discovered_servers) == ["$KEYNAME"]
    assert isinstance(discovered_servers["$KEYNAME"], MCPStdioTool)


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
        vscode=False,
    )

    tool = discover_mcp_servers(tmp_path)["demo"]

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
        vscode=False,
    )

    tool = discover_mcp_servers(tmp_path)["demo"]

    assert isinstance(tool, MCPStreamableHTTPTool)
    assert tool.url == "https://example.com:8080/api"
