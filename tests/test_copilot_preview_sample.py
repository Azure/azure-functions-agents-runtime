"""The preview verifier must not require customer-managed native-runtime settings."""

from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from azure_functions_agents import _harness
from azure_functions_agents.config import paths


def _load_verifier():
    source = Path(__file__).resolve().parents[1] / "samples" / "copilot-preview" / "verify.py"
    spec = importlib.util.spec_from_file_location("copilot_preview_verify", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_restart_verifier_uses_sdk_default_native_resolution(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    module = _load_verifier()

    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT", "true")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_DIR", str(tmp_path / "state"))
    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://fixture.services.ai.azure.com/api/projects/test",
    )
    monkeypatch.setenv("FOUNDRY_MODEL", "gpt-4.1-mini")
    monkeypatch.delenv("AzureWebJobsStorage", raising=False)
    monkeypatch.delenv("AzureWebJobsStorage__blobServiceUri", raising=False)
    monkeypatch.delenv("COPILOT_CLI_EXTRACT_DIR", raising=False)
    monkeypatch.delenv("COPILOT_SKIP_CLI_DOWNLOAD", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["verify.py", "--restart-host", "--evidence", str(tmp_path / "evidence.json")],
    )

    starts: list[Path] = []
    phases: list[str] = []

    @contextmanager
    def running_host(app: Path, *, timeout: float) -> Any:
        starts.append(app)
        yield SimpleNamespace(base_url="http://127.0.0.1:7071")

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> Client:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

    monkeypatch.setitem(sys.modules, "_func_host", SimpleNamespace(running_host=running_host))
    monkeypatch.setattr(module.httpx, "Client", Client)
    monkeypatch.setattr(module, "first", lambda *args: phases.append("first"))
    monkeypatch.setattr(module, "followup", lambda *args: phases.append("followup"))
    monkeypatch.setattr(module, "negative", lambda *args: phases.append("negative"))

    module.main()

    assert len(starts) == 2
    assert phases == ["first", "followup", "negative"]


def test_negative_verifier_checks_invalid_identity_without_native_session_policy() -> None:
    module = _load_verifier()

    class Client:
        def post(self, path: str, **_kwargs: Any) -> Any:
            if path == "/agents/main/chat":
                assert _kwargs["headers"]["x-ms-session-id"] == "../outside"
                return SimpleNamespace(
                    status_code=400,
                    json=lambda: {"error": "Invalid session_id (must match the safe pattern)"},
                )
            return SimpleNamespace(status_code=501)

        def get(self, _path: str) -> Any:
            return SimpleNamespace(status_code=501)

    module.negative(Client())


def test_blob_restart_requires_explicit_disposable_storage_and_host_settings(
    monkeypatch: Any, tmp_path: Path, capsys: Any,
) -> None:
    module = _load_verifier()
    monkeypatch.setattr(module, "__file__", str(tmp_path / "verify.py"))
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT", "true")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")
    monkeypatch.delenv("AzureWebJobsStorage__blobServiceUri", raising=False)
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER", "disposable")
    monkeypatch.setattr(sys, "argv", ["verify.py", "--restart-host"])
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_TEST_DISPOSABLE_BLOB", raising=False)
    with pytest.raises(SystemExit, match="2"):
        module.main()
    assert "AZURE_FUNCTIONS_AGENTS_TEST_DISPOSABLE_BLOB=1" in capsys.readouterr().err

    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_TEST_DISPOSABLE_BLOB", "1")
    with pytest.raises(SystemExit, match="2"):
        module.main()
    assert "local.settings.json" in capsys.readouterr().err


def test_blob_restart_with_entra_service_uri_rejects_shadowing_settings(
    monkeypatch: Any, tmp_path: Path, capsys: Any,
) -> None:
    module = _load_verifier()
    monkeypatch.setattr(module, "__file__", str(tmp_path / "verify.py"))
    (tmp_path / "src").mkdir()
    settings = tmp_path / "src" / "local.settings.json"
    uri = "https://account.blob.core.windows.net"
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT", "true")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER", "disposable")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_TEST_DISPOSABLE_BLOB", "1")
    monkeypatch.setenv("AzureWebJobsStorage__blobServiceUri", uri)
    monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")
    monkeypatch.delenv("FOUNDRY_PROJECT_ENDPOINT", raising=False)
    monkeypatch.setattr(sys, "argv", ["verify.py", "--restart-host"])

    settings.write_text(json.dumps({
        "Values": {"AzureWebJobsStorage": "UseDevelopmentStorage=true",
                   "AzureWebJobsStorage__blobServiceUri": "https://ignored.invalid"}
    }), encoding="utf-8")
    with pytest.raises(SystemExit, match="2"):
        module.main()
    assert "FOUNDRY_PROJECT_ENDPOINT" in capsys.readouterr().err

    monkeypatch.delenv("AzureWebJobsStorage")
    for values in (
        {"AzureWebJobsStorage": "UseDevelopmentStorage=true"},
        {"AzureWebJobsStorage": "", "AzureWebJobsStorage__blobServiceUri": uri + "/other"},
    ):
        settings.write_text(json.dumps({"Values": values}), encoding="utf-8")
        with pytest.raises(SystemExit, match="2"):
            module.main()
        assert "Entra ID service URI" in capsys.readouterr().err

    # Blank connection plus the matching URI passes the storage gate (then stops on Foundry settings).
    settings.write_text(json.dumps({"Values": {"AzureWebJobsStorage": ""}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="2"):
        module.main()
    err = capsys.readouterr().err
    assert "FOUNDRY_PROJECT_ENDPOINT" in err
    assert uri not in err


@pytest.mark.parametrize("api_key", [None, "fixture-azure-key"])
def test_azure_restart_verifier_does_not_require_an_openai_key(
    monkeypatch, tmp_path, capsys, api_key,
):
    module = _load_verifier()
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT", "true")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "azure_openai")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://fixture.openai.azure.com")
    for name in (
        "AzureWebJobsStorage", "AzureWebJobsStorage__blobServiceUri",
        "AZURE_FUNCTIONS_AGENTS_MODEL", "AZURE_OPENAI_DEPLOYMENT", "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    if api_key is None:
        monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", api_key)
    monkeypatch.setattr(sys, "argv", ["verify.py", "--restart-host"])

    with pytest.raises(SystemExit, match="2"):
        module.main()

    error = capsys.readouterr().err
    assert "AZURE_OPENAI_DEPLOYMENT" in error
    assert "OPENAI_API_KEY" not in error


def test_sample_entrypoint_uses_functions_script_root(monkeypatch: Any, tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "samples" / "copilot-preview" / "src" / "function_app.py"
    monkeypatch.setattr(paths, "_app_root", None)
    monkeypatch.setattr(_harness, "_HARNESSES", {})
    monkeypatch.delenv("AZURE_FUNCTIONS_AGENTS_APP_ROOT", raising=False)
    monkeypatch.setenv("AzureWebJobsScriptRoot", str(source.parent))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_harness.FLAG, "true")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "openai")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_MODEL", "gpt-4.1-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "sentinel-not-a-secret")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("WEBSITE_INSTANCE_ID", raising=False)
    monkeypatch.delenv("FUNCTIONS_WORKER_PROCESS_COUNT", raising=False)
    spec = importlib.util.spec_from_file_location("copilot_preview_function_app", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    names = {item.get_function_name() for item in module.app.get_functions()}
    assert paths.get_app_root() == source.parent.resolve()
    assert "main" in names
    assert "agent_main_builtin_chat" in names
