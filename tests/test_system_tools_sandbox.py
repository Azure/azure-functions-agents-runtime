from __future__ import annotations

import logging

import pytest

from azure_functions_agents.system_tools.sandbox import create_sandbox_tools


def test_create_sandbox_tools_skips_unresolved_inline_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("HOST", raising=False)

    with caplog.at_level(logging.WARNING):
        tools = create_sandbox_tools({"endpoint": "https://$HOST/api"})

    assert tools == []
    assert "could not resolve endpoint" in caplog.text


def test_create_sandbox_tools_accepts_resolved_inline_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOST", "eastus.dynamicsessions.io")

    tools = create_sandbox_tools({"endpoint": "https://$HOST/api"})

    assert len(tools) == 1


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://eastus.dynamicsessions.io",
        "https://westus2.dynamicsessions.io/",
    ],
)
def test_create_sandbox_tools_accepts_dynamic_sessions_host(endpoint: str) -> None:
    tools = create_sandbox_tools({"endpoint": endpoint})

    assert len(tools) == 1


def test_create_sandbox_tools_accepts_endpoint_resolved_from_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SESSION_POOL_ENDPOINT", "https://westus2.dynamicsessions.io")

    tools = create_sandbox_tools({"endpoint": "$SESSION_POOL_ENDPOINT"})

    assert len(tools) == 1


def test_create_sandbox_tools_rejects_non_dynamic_sessions_host(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        tools = create_sandbox_tools({"endpoint": "https://collector.example/ingest"})

    assert tools == []
    assert "failed validation" in caplog.text


def test_create_sandbox_tools_rejects_userinfo_endpoint(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        tools = create_sandbox_tools(
            {"endpoint": "https://eastus.dynamicsessions.io@collector.example/x"}
        )

    assert tools == []
    assert "failed validation" in caplog.text


def test_create_sandbox_tools_rejects_non_tls_endpoint(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        tools = create_sandbox_tools({"endpoint": "http://eastus.dynamicsessions.io"})

    assert tools == []
    assert "failed validation" in caplog.text


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://eastus.dynamicsessions.io.collector.example",
        "https://evil-dynamicsessions.io",
    ],
)
def test_create_sandbox_tools_rejects_suffix_lookalike_host(
    endpoint: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        tools = create_sandbox_tools({"endpoint": endpoint})

    assert tools == []
    assert "failed validation" in caplog.text
