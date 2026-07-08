from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from azure_functions_agents.system_tools import sandbox
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


def _run_with_streams(
    monkeypatch: pytest.MonkeyPatch, *, stdout: str, stderr: str
) -> tuple[str, list[bool]]:
    """Run execute_python with a faked ACA response and capture the error-metric calls."""
    errors: list[bool] = []

    async def fake_ensure_shared_resources(_client_id: str | None) -> tuple[object, object]:
        return object(), object()

    async def fake_execute_code(
        endpoint: str,
        code: str,
        session_id: str,
        token_provider: Any,
        http_session: Any,
    ) -> str:
        import json

        return json.dumps({"result": None, "stdout": stdout, "stderr": stderr})

    monkeypatch.setattr(sandbox, "_ensure_shared_resources", fake_ensure_shared_resources)
    monkeypatch.setattr(sandbox, "_execute_code", fake_execute_code)
    monkeypatch.setattr(sandbox, "_setup_sessions", set())
    monkeypatch.setattr(
        sandbox, "record_sandbox_execution", lambda *, error: errors.append(error)
    )

    # Must be a valid *.dynamicsessions.io host so create_sandbox_tools()'s endpoint allow-list
    # (added in #83) still returns the tool.
    tool = create_sandbox_tools({"endpoint": "https://eastus.dynamicsessions.io"})[0]
    result = asyncio.run(tool.func(code="print('hi')"))
    return result, errors


def test_sandbox_clean_run_records_success(monkeypatch: pytest.MonkeyPatch) -> None:
    result, errors = _run_with_streams(monkeypatch, stdout="ok", stderr="")
    assert '"stdout": "ok"' in result
    assert errors == [False]


def test_sandbox_stderr_is_surfaced_as_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A "successful" tool call whose stderr is non-empty must be recorded as an error, even though
    # the tool still returns the payload string to the model.
    result, errors = _run_with_streams(
        monkeypatch, stdout="", stderr="TesseractNotFoundError: tesseract is not installed"
    )
    assert "TesseractNotFoundError" in result
    assert errors == [True]


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
