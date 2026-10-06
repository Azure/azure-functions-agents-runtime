from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import get_type_hints

from azurefunctions.extensions.http.fastapi import JSONResponse, Request

from azure_functions_agents import _harness
from azure_functions_agents.config import paths


def test_sample_registers_http_v2_handler(monkeypatch: object) -> None:
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

    functions = module.app.get_functions()
    assert [function.get_function_name() for function in functions] == ["summarize"]
    annotations = get_type_hints(functions[0].get_user_function())
    assert annotations == {"req": Request, "return": JSONResponse}