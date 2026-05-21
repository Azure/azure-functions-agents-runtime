from __future__ import annotations

import asyncio
import re
from typing import Any
from unittest.mock import patch

import pytest

from azure_functions_agents._credential import build_async_credential
from azure_functions_agents.system_tools import sandbox


def _sandbox_config() -> dict[str, str]:
    return {"session_pool_management_endpoint": "https://sandbox.example"}


def _capture_aca_session_ids(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fallback_session_id: str | None | object = ...,
) -> list[str]:
    aca_session_ids: list[str] = []

    async def fake_ensure_shared_resources() -> None:
        return None

    async def fake_execute_code(
        endpoint: str,
        code: str,
        session_id: str,
        token_provider: Any,
        http_session: Any,
    ) -> str:
        aca_session_ids.append(session_id)
        return '{"result": "ok", "stdout": "", "stderr": ""}'

    monkeypatch.setattr(sandbox, "_ensure_shared_resources", fake_ensure_shared_resources)
    monkeypatch.setattr(sandbox, "_execute_code", fake_execute_code)
    monkeypatch.setattr(sandbox, "_token_provider", object())
    monkeypatch.setattr(sandbox, "_http_session", object())
    monkeypatch.setattr(sandbox, "_setup_sessions", set())

    if fallback_session_id is ...:
        tool = sandbox.create_sandbox_tools(_sandbox_config())[0]
    else:
        tool = sandbox.create_sandbox_tools(
            _sandbox_config(),
            fallback_session_id=fallback_session_id,
        )[0]

    asyncio.run(tool.func(code="print('ok')"))
    return aca_session_ids


def test_create_sandbox_tools_generates_hex_guid_when_fallback_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aca_session_ids = _capture_aca_session_ids(monkeypatch)

    assert len(set(aca_session_ids)) == 1
    assert re.fullmatch(r"[0-9a-f]{32}", aca_session_ids[0])


def test_create_sandbox_tools_generates_unique_session_ids_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_ids = _capture_aca_session_ids(monkeypatch)
    second_ids = _capture_aca_session_ids(monkeypatch)

    first_session_id = first_ids[0]
    second_session_id = second_ids[0]

    assert re.fullmatch(r"[0-9a-f]{32}", first_session_id)
    assert re.fullmatch(r"[0-9a-f]{32}", second_session_id)
    assert first_session_id != second_session_id


@pytest.mark.parametrize("fallback_session_id", [None, ""])
def test_create_sandbox_tools_never_uses_default_literal_session_id(
    monkeypatch: pytest.MonkeyPatch,
    fallback_session_id: str | None,
) -> None:
    aca_session_ids = _capture_aca_session_ids(
        monkeypatch,
        fallback_session_id=fallback_session_id,
    )

    assert "default" not in aca_session_ids
    assert len(set(aca_session_ids)) == 1
    assert re.fullmatch(r"[0-9a-f]{32}", aca_session_ids[0])


def test_build_managed_identity_credential_uses_azure_client_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_CLIENT_ID", "client-id-123")

    with patch("azure.identity.aio.DefaultAzureCredential") as credential_ctor:
        credential = object()
        credential_ctor.return_value = credential

        assert build_async_credential() is credential

    credential_ctor.assert_called_once_with(managed_identity_client_id="client-id-123")


def test_build_managed_identity_credential_omits_kwargs_without_azure_client_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)

    with patch("azure.identity.aio.DefaultAzureCredential") as credential_ctor:
        credential = object()
        credential_ctor.return_value = credential

        assert build_async_credential() is credential

    credential_ctor.assert_called_once_with()
