"""The preview verifier must not require customer-managed native-runtime settings."""

from __future__ import annotations

import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def test_restart_verifier_uses_sdk_default_native_resolution(
    monkeypatch: Any, tmp_path: Path,
) -> None:
    source = Path(__file__).resolve().parents[1] / "samples" / "copilot-preview" / "verify.py"
    spec = importlib.util.spec_from_file_location("copilot_preview_verify", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT", "true")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_PROVIDER", "foundry")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_SESSION_DIR", str(tmp_path / "state"))
    monkeypatch.setenv(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://fixture.services.ai.azure.com/api/projects/test",
    )
    monkeypatch.setenv("FOUNDRY_MODEL", "gpt-4.1-mini")
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
