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
    monkeypatch.setenv("HOST", "example.com")

    tools = create_sandbox_tools({"endpoint": "https://$HOST/api"})

    assert len(tools) == 1


def test_create_sandbox_tools_accepts_legacy_endpoint_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOST", "example.com")

    tools = create_sandbox_tools({"session_pool_management_endpoint": "https://$HOST/api"})

    assert len(tools) == 1
