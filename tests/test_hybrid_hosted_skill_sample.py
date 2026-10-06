from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import get_type_hints

import pytest
from azurefunctions.extensions.http.fastapi import JSONResponse, Request

from azure_functions_agents import _harness
from azure_functions_agents.config import paths


def _load_sample(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    source = (
        Path(__file__).resolve().parents[1]
        / "samples"
        / "hybrid-hosted-skill"
        / "src"
        / "function_app.py"
    )
    monkeypatch.setattr(paths, "_app_root", None)
    monkeypatch.setattr(_harness, "_HARNESSES", {})
    monkeypatch.setenv("AzureWebJobsScriptRoot", str(source.parent))
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://fixture.services.ai.azure.com/api/projects/test",
    )
    monkeypatch.setenv("FOUNDRY_MODEL", "gpt-4.1")

    spec = importlib.util.spec_from_file_location("hybrid_hosted_skill_function_app", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _request(payload: object) -> Request:
    body = json.dumps(payload).encode()

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/summarize",
            "raw_path": b"/api/summarize",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("test", 123),
            "server": ("test", 80),
        },
        receive,
    )


def test_sample_registers_http_v2_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_sample(monkeypatch)

    functions = module.app.get_functions()
    assert [function.get_function_name() for function in functions] == ["summarize"]
    annotations = get_type_hints(functions[0].get_user_function())
    assert annotations == {"req": Request, "return": JSONResponse}


@pytest.mark.asyncio
async def test_sample_rejects_invalid_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_sample(monkeypatch)
    [function] = module.app.get_functions()

    response = await function.get_user_function()(
        _request({"prompt": "Summarize this", "session_id": "../unsafe"})
    )

    assert response.status_code == 400
    assert json.loads(response.body) == {
        "error": "Invalid session_id (must match ^[A-Za-z0-9._-]{1,128}$)"
    }