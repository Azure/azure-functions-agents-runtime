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

from azure_functions_agents.config import paths
from azure_functions_agents.config.loader import load_agent_specs, load_global_config
from azure_functions_agents.config.merge import compose
from azure_functions_agents.discovery.mcp import discover_mcp_servers
from azure_functions_agents.discovery.skills import discover_skills
from azure_functions_agents.discovery.tools import discover_user_tools
from azure_functions_agents.harness import _harness_binding as _harness
from azure_functions_agents.registration.capabilities import build_capabilities

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "copilot-preview" / "src"


def test_sample_exposes_learn_mcp_and_scoped_reference_skill():
    [spec] = load_agent_specs(SAMPLE, strict=True)
    servers = discover_mcp_servers(SAMPLE)
    skills = discover_skills(SAMPLE)
    resolved = compose(
        spec, load_global_config(SAMPLE),
        discovered_mcp_names=list(servers.servers), discovered_skill_names=list(skills.skills),
    )
    capabilities = build_capabilities(
        resolved,
        discovered_user_tools=discover_user_tools(SAMPLE).tools,
        discovered_mcp_tools=servers.servers,
        discovered_skills=skills.skills,
        discovered_skill_descriptors=skills.descriptors,
    )
    assert servers.failed_loads == skills.failed_loads == []
    assert capabilities.filtered_mcp_tools is not None
    [server] = capabilities.filtered_mcp_tools
    assert server.name == "microsoft-learn"
    assert server.url == "https://learn.microsoft.com/api/mcp"
    assert server.tools == ("microsoft_docs_search", "microsoft_docs_fetch")
    assert server.headers == ()
    assert server.auth_scope is None
    [skill] = capabilities.skills
    assert skill.name == "preview-check"
    assert skill.path == (SAMPLE / "skills" / "preview-check").resolve()
    assert (skill.path / "references" / "check.txt").read_text(
        encoding="utf-8",
    ).strip() == "REFERENCE_READ_7C42A9"
    assert "REFERENCE_READ_7C42A9" not in (skill.path / "SKILL.md").read_text(encoding="utf-8")
    assert {tool.name for tool in capabilities.filtered_user_tools} == {"make_receipt"}
    assert {tool.name for tool in capabilities.web_request_tools} == {"web_request"}


@pytest.mark.parametrize("fault", [None, "no-tools", "failed-view", "wrong-path", "missing-result"])
def test_skill_verifier_requires_successful_reference_read(fault):
    module = _load_verifier()
    reference = SAMPLE / "skills" / "preview-check" / "references" / "check.txt"
    view = {
        "tool_name": "view", "success": True,
        "arguments": json.dumps({"path": str(reference)}), "result": "REFERENCE_READ_7C42A9",
    }
    result = {
        "response": "SKILL_LOADED_PREVIEW_CHECK REFERENCE_READ_7C42A9",
        "tool_calls": [{"tool_name": "skill", "success": True}, view],
    }
    if fault == "no-tools":
        result["tool_calls"] = []
    elif fault == "failed-view":
        view["success"] = False
    elif fault == "wrong-path":
        view["arguments"] = {"path": str(reference.parent / "wrong.txt")}
    elif fault == "missing-result":
        view["result"] = "No marker"

    class Client:
        def post(self, path, *, json):
            assert path == "/agents/main/chat"
            assert "REFERENCE_READ_7C42A9" not in json["prompt"]
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: result)

    if fault is None:
        module.skill(Client())
    else:
        with pytest.raises(AssertionError):
            module.skill(Client())


@pytest.mark.parametrize(
    "fault", [None, "no-tools", "failed-search", "wrong-tool", "missing-result", "wrong-link"],
)
def test_mcp_verifier_requires_successful_learn_tool_evidence(fault):
    module = _load_verifier()
    link = "https://learn.microsoft.com/azure/azure-functions/functions-reference-python"
    call = {
        "tool_name": "microsoft-learn-microsoft_docs_search", "success": True, "result": link,
    }
    result = {"response": link, "tool_calls": [call]}
    if fault == "no-tools":
        result["tool_calls"] = []
    elif fault == "failed-search":
        call["success"] = False
    elif fault == "wrong-tool":
        call["tool_name"] = "web_request"
    elif fault == "missing-result":
        call["result"] = "No link"
    elif fault == "wrong-link":
        result["response"] = link + "-unrelated"

    class Client:
        def post(self, path, *, json):
            assert path == "/agents/main/chat"
            assert "microsoft_docs_search" in json["prompt"]
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: result)

    if fault is None:
        module.mcp(Client())
    else:
        with pytest.raises(AssertionError):
            module.mcp(Client())


def test_capabilities_verifier_runs_both_checks(monkeypatch):
    module = _load_verifier()
    phases = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["base_url"] == "http://127.0.0.1:7073"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(
        sys, "argv",
        ["verify.py", "--base-url", "http://127.0.0.1:7073", "--phase", "capabilities"],
    )
    monkeypatch.setattr(module.httpx, "Client", Client)
    monkeypatch.setattr(module, "skill", lambda _client: phases.append("skill"))
    monkeypatch.setattr(module, "mcp", lambda _client: phases.append("mcp"))
    module.main()
    assert phases == ["skill", "mcp"]


@pytest.mark.parametrize(
    "fault",
    [
        None, "windows-path", "json-arguments", "failed-search", "failed-view",
        "wrong-path", "not-advertised", "before-search", "wrong-link", "no-search",
        "traversal", "sibling-prefix", "preview-path",
    ],
)
def test_mcp_verifier_reads_only_the_saved_search_output(fault):
    module = _load_verifier()
    link = "https://learn.microsoft.com/azure/azure-functions/functions-reference-python"
    output = "/session-state/temp/mcp-output.txt"
    if fault == "windows-path":
        output = output.replace("/", "\\")
    elif fault == "traversal":
        output = "/session-state/temp/../other.txt"
    elif fault == "sibling-prefix":
        output = "/session-state/tempx/mcp-output.txt"
    search = {
        "tool_name": "microsoft-learn-microsoft_docs_search",
        "success": True,
        "result": f"Output too large to read at once (44.9 KB). Saved to: {output}\n"
        "Consider using view to read the saved output.",
    }
    read = {
        "tool_name": "view", "success": True, "arguments": {"path": output}, "result": link,
    }
    result = {"response": link, "tool_calls": [search, read]}
    if fault == "json-arguments":
        read["arguments"] = json.dumps(read["arguments"])
    elif fault == "failed-search":
        result["tool_calls"].insert(0, {**search, "success": False})
    elif fault == "failed-view":
        read["success"] = False
    elif fault == "wrong-path":
        read["arguments"] = {"path": "/workspace/main.agent.md"}
    elif fault == "not-advertised":
        search["result"] = "No saved output."
    elif fault == "before-search":
        result["tool_calls"].reverse()
    elif fault == "wrong-link":
        result["response"] = link + "-unrelated"
    elif fault == "no-search":
        result["tool_calls"] = [read]
    elif fault == "preview-path":
        search["result"] += "\nPreview:\nSaved to: /session-state/temp/other.txt\n"
        read["arguments"] = {"path": "/session-state/temp/other.txt"}

    class Client:
        def post(self, path, *, json):
            assert path == "/agents/main/chat"
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: result)

    if fault in {None, "windows-path", "json-arguments"}:
        module.mcp(Client())
    else:
        with pytest.raises(AssertionError):
            module.mcp(Client())


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
