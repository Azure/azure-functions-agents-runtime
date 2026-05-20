from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from agent_framework import MCPStreamableHTTPTool

from azure_functions_agents.discovery.mcp import clear_mcp_cache, discover_mcp_servers


@pytest.fixture(autouse=True)
def clear_discovery_cache() -> None:
    clear_mcp_cache()
    yield
    clear_mcp_cache()


def _write_mcp_config(
    app_root: Path, server_config: dict[str, object] | None = None
) -> None:
    (app_root / ".vscode").mkdir()
    (app_root / ".vscode" / "mcp.json").write_text(
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
        discovered_servers = discover_mcp_servers(tmp_path)

    assert discovered_servers == {}
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
        discovered_servers = discover_mcp_servers(tmp_path)

    assert discovered_servers == {}
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

    assert list(discovered_servers) == ["demo"]
    assert isinstance(discovered_servers["demo"], MCPStreamableHTTPTool)
