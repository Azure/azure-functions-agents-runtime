from __future__ import annotations

import ast
import inspect
import json
import logging
from collections.abc import Iterator
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from unittest.mock import Mock

import pytest

import azure_functions_agents._credential as credential_helpers
import azure_functions_agents._mcp_auth as mcp_auth
import azure_functions_agents.discovery.mcp as mcp_discovery
from azure_functions_agents.discovery.mcp import (
    MCPServerDescriptor,
    clear_mcp_cache,
    discover_mcp_servers,
)


@pytest.fixture(autouse=True)
def clear_discovery_cache() -> Iterator[None]:
    clear_mcp_cache()
    yield
    clear_mcp_cache()


def _write_mcp_config(
    app_root: Path, server_config: dict[str, object] | None = None
) -> None:
    config = (
        server_config
        if server_config is not None
        else {"type": "http", "url": "https://example.com/mcp"}
    )
    _write_mcp_json(app_root, {"servers": {"demo": config}})


def _write_mcp_json(app_root: Path, data: object) -> None:
    (app_root / "mcp.json").write_text(json.dumps(data), encoding="utf-8")


def test_descriptor_create_copies_mutable_inputs_deeply() -> None:
    metadata = {"nested": ["first"]}
    headers: dict[str, object] = {"X-Test": "yes", "X-Metadata": metadata}
    tools = ["search_issues", "list_pull_requests"]
    server = MCPServerDescriptor.create(
        name="demo",
        url="https://example.com/mcp",
        headers=headers,
        tools=tools,
    )
    original_headers = server.headers
    original_hash = hash(server)

    headers["X-Test"] = "changed"
    metadata["nested"].append("changed")
    tools.append("changed")

    assert server.headers == original_headers
    assert server.headers == (("X-Test", "yes"), ("X-Metadata", "{'nested': ['first']}"))
    assert server.tools == ("search_issues", "list_pull_requests")
    assert hash(server) == original_hash


def test_descriptor_create_copies_header_pairs() -> None:
    headers = [("X-Test", "yes")]
    server = MCPServerDescriptor.create(
        name="demo", url="https://example.com/mcp", headers=headers
    )
    headers[0] = ("X-Test", "changed")

    assert server.headers == (("X-Test", "yes"),)


def test_descriptor_is_frozen_with_immutable_headers_and_tools() -> None:
    server = MCPServerDescriptor.create(
        name="demo",
        url="https://example.com/mcp",
        headers={"X-Test": "yes"},
        tools=["search_issues"],
    )

    with pytest.raises(FrozenInstanceError):
        server.name = "changed"
    with pytest.raises(TypeError):
        server.headers[0][1] = "changed"
    with pytest.raises(TypeError):
        server.tools[0] = "changed"

    assert server.name == "demo"
    assert server.headers == (("X-Test", "yes"),)
    assert server.tools == ("search_issues",)


def test_descriptor_repr_hides_all_static_headers() -> None:
    server = MCPServerDescriptor.create(
        name="demo",
        url="https://example.com/mcp",
        headers={"Authorization": "private-static-value", "X-Private-Header": "private-value"},
    )

    rendered = repr(server)
    assert "demo" in rendered
    assert "https://example.com/mcp" in rendered
    assert "Authorization" not in rendered
    assert "X-Private-Header" not in rendered
    assert "private-static-value" not in rendered
    assert "private-value" not in rendered


def test_descriptor_has_only_sdk_free_configuration_fields() -> None:
    assert [field.name for field in fields(MCPServerDescriptor)] == [
        "name",
        "url",
        "transport",
        "headers",
        "tools",
        "auth_scope",
        "client_id",
    ]
    assert mcp_discovery.MCPTool.__value__ is MCPServerDescriptor


def test_discovery_imports_no_harness_sdk() -> None:
    tree = ast.parse(inspect.getsource(mcp_discovery))
    imports = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    imports.extend(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )

    assert all(not module.startswith(("agent_framework", "copilot")) for module in imports)


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
    assert first.servers["demo"] is second.servers["demo"]
    assert read_count == 1


def test_discover_mcp_servers_returns_independent_dicts(tmp_path: Path) -> None:
    _write_mcp_config(tmp_path)
    first_result = discover_mcp_servers(tmp_path)
    first_result.servers["extra"] = first_result.servers["demo"]
    del first_result.servers["demo"]

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

    first = discover_mcp_servers(tmp_path)
    clear_mcp_cache()
    second = discover_mcp_servers(tmp_path)

    assert read_count == 2
    assert first.servers["demo"] == second.servers["demo"]
    assert first.servers["demo"] is not second.servers["demo"]


@pytest.mark.parametrize("value", [[1, 2, 3], "hello", 42, None])
def test_discover_mcp_servers_handles_non_object_top_level(
    value: object, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_mcp_json(tmp_path, value)
    config_path = tmp_path / "mcp.json"
    with caplog.at_level(logging.WARNING):
        result = discover_mcp_servers(tmp_path)

    assert result.servers == {}
    assert result.failed_loads == []
    assert any(
        record.getMessage()
        == f"Ignoring {config_path}: expected a JSON object at the top level, got {type(value).__name__}."
        for record in caplog.records
    )


def test_discover_mcp_servers_handles_unreadable_json(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "mcp.json").write_text("{invalid", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        result = discover_mcp_servers(tmp_path)

    assert result.servers == {}
    assert result.failed_loads == []
    assert "Failed to read MCP config" in caplog.text


@pytest.mark.parametrize("servers", [[], "invalid", None, 42])
def test_discover_mcp_servers_handles_non_object_servers(
    servers: object, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_mcp_json(tmp_path, {"servers": servers})
    with caplog.at_level(logging.WARNING):
        result = discover_mcp_servers(tmp_path)

    assert result.servers == {}
    assert result.failed_loads == []
    assert "'servers' must be an object" in caplog.text


@pytest.mark.parametrize(
    "config",
    [
        {"command": "python", "args": ["-m", "demo_server"]},
        {"type": "stdio", "url": "https://example.com/mcp"},
        {"type": "local"},
    ],
)
def test_discover_mcp_servers_skips_stdio_config(
    config: dict[str, object], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_mcp_config(tmp_path, config)
    with caplog.at_level(logging.WARNING):
        result = discover_mcp_servers(tmp_path)

    assert result.servers == {}
    assert result.failed_loads == [("demo", "MCP stdio transport is not supported")]
    assert "MCP stdio transport is not supported; skipping server 'demo'" in caplog.text


def test_discover_mcp_servers_skips_sse_config(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_mcp_config(tmp_path, {"type": "sse", "url": "https://example.com/mcp"})
    with caplog.at_level(logging.WARNING):
        result = discover_mcp_servers(tmp_path)

    error = "unknown server type 'sse'; supported types are 'http' and 'streamable-http'"
    assert result.servers == {}
    assert result.failed_loads == [("demo", error)]
    assert f"MCP server 'demo': {error}" in caplog.text


@pytest.mark.parametrize("transport", ["http", "HTTP", "streamable-http"])
def test_discover_mcp_servers_supports_remote_http_transports(
    transport: str, tmp_path: Path
) -> None:
    _write_mcp_config(tmp_path, {"type": transport, "url": " https://example.com/mcp "})

    result = discover_mcp_servers(tmp_path)

    assert result.failed_loads == []
    assert result.servers["demo"].name == "demo"
    assert result.servers["demo"].url == "https://example.com/mcp"
    assert result.servers["demo"].transport == transport.lower()


def test_discover_mcp_servers_accepts_url_without_type(tmp_path: Path) -> None:
    _write_mcp_config(tmp_path, {"url": "https://example.com/mcp"})

    result = discover_mcp_servers(tmp_path)

    assert list(result.servers) == ["demo"]
    assert result.servers["demo"].transport == "http"


@pytest.mark.parametrize("url_config", [{}, {"url": ""}, {"url": "  "}])
def test_discover_mcp_servers_skips_http_type_missing_url(
    url_config: dict[str, object], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_mcp_config(tmp_path, {"type": "http", **url_config})
    with caplog.at_level(logging.WARNING):
        result = discover_mcp_servers(tmp_path)

    assert result.servers == {}
    assert result.failed_loads == [("demo", "missing 'url'")]
    assert "MCP server 'demo': missing 'url', skipping" in caplog.text


def test_discover_mcp_servers_skips_unrecognized_config(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _write_mcp_config(tmp_path, {})
    with caplog.at_level(logging.WARNING):
        result = discover_mcp_servers(tmp_path)

    error = "unrecognized config (expected 'url' plus type 'http' or 'streamable-http')"
    assert result.servers == {}
    assert result.failed_loads == [("demo", error)]
    assert f"MCP server 'demo': {error}, skipping" in caplog.text


def test_discovery_keeps_valid_servers_and_reports_only_rejected_entries(tmp_path: Path) -> None:
    _write_mcp_json(
        tmp_path,
        {
            "servers": {
                "valid": {"url": "https://example.com/mcp"},
                "missing": {"type": "http"},
                "ignored": ["not", "a", "server"],
                "unsupported": {"type": "sse"},
            }
        },
    )

    result = discover_mcp_servers(tmp_path)

    assert list(result.servers) == ["valid"]
    assert [name for name, _error in result.failed_loads] == ["missing", "unsupported"]
    assert discover_mcp_servers(tmp_path).failed_loads == []


def test_discover_mcp_servers_ignores_vscode_mcp_json(tmp_path: Path) -> None:
    (tmp_path / ".vscode").mkdir()
    _write_mcp_config(tmp_path / ".vscode")

    result = discover_mcp_servers(tmp_path)

    assert result.servers == {}


@pytest.mark.parametrize(
    ("tool_config", "expected"),
    [
        ({}, None),
        ({"tools": ["*"]}, None),
        ({"tools": ["search_issues", "*", "list_pull_requests"]}, None),
        ({"tools": []}, ()),
        ({"tools": ["search_issues", "list_pull_requests"]}, ("search_issues", "list_pull_requests")),
        ({"tools": [" exact name ", "exact name"]}, (" exact name ", "exact name")),
        ({"tools": [1, True, None]}, ("1", "True", "None")),
        ({"tools": "search_issues"}, None),
        ({"tools": {"search_issues": True}}, None),
        ({"tools": None}, None),
    ],
)
def test_discover_preserves_all_none_and_exact_name_tool_filters(
    tool_config: dict[str, object], expected: tuple[str, ...] | None, tmp_path: Path
) -> None:
    _write_mcp_config(tmp_path, {"url": "https://example.com/mcp", **tool_config})

    result = discover_mcp_servers(tmp_path)

    assert result.servers["demo"].tools == expected
    assert result.failed_loads == []


@pytest.mark.parametrize("headers", [None, [], "invalid"])
def test_discovery_preserves_legacy_malformed_header_handling(
    headers: object, tmp_path: Path
) -> None:
    _write_mcp_config(tmp_path, {"url": "https://example.com/mcp", "headers": headers})

    result = discover_mcp_servers(tmp_path)

    assert result.servers["demo"].headers == ()
    assert result.failed_loads == []


def test_discovery_preserves_legacy_header_string_coercion(tmp_path: Path) -> None:
    _write_mcp_config(
        tmp_path, {"url": "https://example.com/mcp", "headers": {"X-Number": 42, "X-Flag": True}}
    )

    result = discover_mcp_servers(tmp_path)

    assert dict(result.servers["demo"].headers) == {"X-Number": "42", "X-Flag": "True"}


def test_discover_substitutes_dollar_in_http_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_HOST", "example.com")
    _write_mcp_config(tmp_path, {"type": "http", "url": "https://$MCP_HOST/api"})

    result = discover_mcp_servers(tmp_path)

    assert result.servers["demo"].url == "https://example.com/api"


def test_discover_substitutes_inline_in_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TOKEN", "abc123")
    _write_mcp_config(
        tmp_path,
        {"url": "https://example.com/api", "headers": {"Authorization": "Bearer $TOKEN"}},
    )

    result = discover_mcp_servers(tmp_path)

    assert dict(result.servers["demo"].headers) == {"Authorization": "Bearer abc123"}


def test_discover_undefined_url_variable_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("MISSING_VAR", raising=False)
    _write_mcp_config(tmp_path, {"type": "http", "url": "https://$MISSING_VAR/api"})
    with caplog.at_level(logging.WARNING):
        result = discover_mcp_servers(tmp_path)

    assert result.servers == {}
    assert result.failed_loads == [("demo", "could not resolve url 'https://$MISSING_VAR/api'")]
    assert "could not resolve url" in caplog.text


@pytest.mark.parametrize(
    ("auth", "scope", "client_id"),
    [
        (None, None, None),
        ([], None, None),
        ({}, None, None),
        ({"scope": " \t "}, None, None),
        ({"scope": " https://resource.example/.default "}, "https://resource.example/.default", None),
        (
            {"scope": "https://resource.example/.default", "client_id": " client-123 "},
            "https://resource.example/.default",
            "client-123",
        ),
        (
            {"scope": "https://resource.example/.default", "client_id": "$MCP_CLIENT_MISSING"},
            "https://resource.example/.default",
            "$MCP_CLIENT_MISSING",
        ),
    ],
)
def test_discovery_records_auth_inputs_without_acquiring_credentials(
    auth: object,
    scope: str | None,
    client_id: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    default_builder = Mock(side_effect=AssertionError("unexpected credential acquisition"))
    client_builder = Mock(side_effect=AssertionError("unexpected credential acquisition"))
    monkeypatch.setattr(credential_helpers, "build_credential", default_builder)
    monkeypatch.setattr(credential_helpers, "build_credential_with_client_id", client_builder)
    monkeypatch.setattr(mcp_auth, "build_credential", default_builder)
    monkeypatch.setattr(mcp_auth, "build_credential_with_client_id", client_builder)
    monkeypatch.delenv("MCP_CLIENT_MISSING", raising=False)
    _write_mcp_config(
        tmp_path,
        {"url": "https://example.com/mcp", "auth": auth, "headers": {"X-Test": "yes"}},
    )

    server = discover_mcp_servers(tmp_path).servers["demo"]

    assert server.auth_scope == scope
    assert server.client_id == client_id
    assert server.headers == (("X-Test", "yes"),)
    default_builder.assert_not_called()
    client_builder.assert_not_called()


def test_discovery_substitutes_auth_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MCP_SCOPE", "https://resource.example/.default")
    monkeypatch.setenv("MCP_CLIENT", "client-123")
    _write_mcp_config(
        tmp_path,
        {"url": "https://example.com/mcp", "auth": {"scope": "$MCP_SCOPE", "client_id": "%MCP_CLIENT%"}},
    )

    server = discover_mcp_servers(tmp_path).servers["demo"]

    assert server.auth_scope == "https://resource.example/.default"
    assert server.client_id == "client-123"


@pytest.mark.parametrize("auth", [{}, {"scope": ""}, {"scope": " \t "}])
def test_discover_mcp_servers_auth_without_scope_keeps_static_headers_and_warning(
    auth: dict[str, str], tmp_path: Path, caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbidden = Mock(side_effect=AssertionError("Blank MCP scope must not acquire a credential"))
    monkeypatch.setattr(mcp_auth, "_build_mcp_credential", forbidden)
    _write_mcp_config(
        tmp_path, {"url": "https://example.com/mcp", "headers": {"X-Test": "yes"}, "auth": auth}
    )
    with caplog.at_level(logging.WARNING):
        server = discover_mcp_servers(tmp_path).servers["demo"]
        assert discover_mcp_servers(tmp_path).servers["demo"] is server
        provider = mcp_auth.build_mcp_header_provider(server)
        assert provider is not None
        assert provider({}) == {"X-Test": "yes"}
        assert provider({}) == {"X-Test": "yes"}

    assert server.auth_scope is None
    assert server.headers == (("X-Test", "yes"),)
    assert [record.getMessage() for record in caplog.records] == [
        "MCP server auth requires a non-empty 'scope'"
    ]
    forbidden.assert_not_called()


def test_discover_does_not_substitute_server_name_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEYNAME", "substituted")
    _write_mcp_json(
        tmp_path, {"servers": {"$KEYNAME": {"type": "http", "url": "https://example.com/api"}}}
    )

    result = discover_mcp_servers(tmp_path)

    assert list(result.servers) == ["$KEYNAME"]
    assert result.servers["$KEYNAME"].name == "$KEYNAME"


def test_discover_does_not_substitute_header_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HEADERKEY", "substituted")
    _write_mcp_config(
        tmp_path, {"url": "https://example.com/api", "headers": {"$HEADERKEY": "value"}}
    )

    result = discover_mcp_servers(tmp_path)

    assert dict(result.servers["demo"].headers) == {"$HEADERKEY": "value"}


def test_discover_undefined_header_value_stays_literal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MCP_HEADER_MISSING", raising=False)
    _write_mcp_config(
        tmp_path,
        {"url": "https://example.com/api", "headers": {"X-Test": "$MCP_HEADER_MISSING"}},
    )

    result = discover_mcp_servers(tmp_path)

    assert dict(result.servers["demo"].headers) == {"X-Test": "$MCP_HEADER_MISSING"}


def test_discover_inline_mix_in_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOST", "example.com")
    monkeypatch.setenv("PORT", "8080")
    _write_mcp_config(tmp_path, {"type": "http", "url": "https://$HOST:$PORT/api"})

    result = discover_mcp_servers(tmp_path)

    assert result.servers["demo"].url == "https://example.com:8080/api"
