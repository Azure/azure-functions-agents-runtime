from __future__ import annotations

import json
from pathlib import Path

import pytest

from azure_functions_agents.discovery.mcp import clear_mcp_cache, discover_mcp_servers


@pytest.fixture(autouse=True)
def clear_discovery_cache() -> None:
    clear_mcp_cache()
    yield
    clear_mcp_cache()


def _write_mcp_config(app_root: Path) -> None:
    (app_root / ".vscode").mkdir()
    (app_root / ".vscode" / "mcp.json").write_text(
        json.dumps(
            {
                "servers": {
                    "demo": {
                        "command": "python",
                        "args": ["-m", "demo_server"],
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
