from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import get_type_hints

import pytest

from azure_functions_agents._function_tool import WorkflowTool, WorkflowToolMetadata, tool


def test_package_imports_resolve_to_real_init() -> None:
    import azure_functions_agents

    package_path = Path(azure_functions_agents.__file__).resolve()
    expected_path = (
        Path(__file__).resolve().parents[1] / "src" / "azure_functions_agents" / "__init__.py"
    )

    assert package_path == expected_path
    for name in azure_functions_agents.__all__:
        assert hasattr(azure_functions_agents, name)


def test_public_exports_include_only_supported_preview_api() -> None:
    import azure_functions_agents

    assert azure_functions_agents.__all__ == [
        "DEFAULT_MODEL",
        "DEFAULT_TIMEOUT",
        "AgentResult",
        "AgentStreamEvent",
        "AgentStreamEventKind",
        "ClientManager",
        "HostedSkill",
        "HostedSkillApp",
        "HostedSkillDFApp",
        "HostedSkillEvent",
        "HostedSkillEventKind",
        "HostedSkillFunctionApp",
        "HostedSkillResponseError",
        "MAFClientManager",
        "WorkflowRetryBackoff",
        "WorkflowRetryPolicy",
        "WorkflowRetryableError",
        "WorkflowTaskContext",
        "WorkflowTerminalError",
        "__version__",
        "create_function_app",
        "create_sandbox_tools",
        "create_web_request_tools",
        "current_workflow_task_context",
        "get_client_manager",
        "resolve_config_dir",
        "run_agent",
        "run_agent_stream",
        "set_app_root",
        "set_client_manager",
        "shutdown_client_manager",
        "tool",
        "validate_session_id",
        "workflow_tool",
    ]
    assert not hasattr(azure_functions_agents, "run_copilot_agent")
    assert not hasattr(azure_functions_agents, "run_copilot_agent_stream")


def test_tool_shim_is_callable() -> None:
    assert callable(tool)


def test_workflow_retry_annotations_resolve_at_runtime() -> None:
    from azure_functions_agents import WorkflowRetryPolicy, workflow_tool

    assert get_type_hints(WorkflowToolMetadata)["retry"] == WorkflowRetryPolicy | None
    assert get_type_hints(WorkflowTool)["retry"] == WorkflowRetryPolicy | None
    assert get_type_hints(workflow_tool)["retry"] == WorkflowRetryPolicy | None


@pytest.mark.parametrize("selected", ["maf", "copilot"])
def test_selected_persistence_runs_and_shuts_down_without_opposite_imports(tmp_path, selected):
    root = tmp_path / "app with spaces"
    root.mkdir()
    script = """
import asyncio
import json
import os
import sys
from pathlib import Path

selected, root = sys.argv[1], Path(sys.argv[2])
opposite = (
    ["azure_functions_agents.harness.copilot_sdk", "copilot"]
    if selected == "maf"
    else [
        "azure_functions_agents.harness.agent_framework._maf_blob_history",
        "azure_functions_agents.harness.agent_framework._maf_file_history",
    ]
)
for name in opposite:
    sys.modules[name] = None
os.environ["AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT"] = "true" if selected == "copilot" else "false"
os.environ["AZURE_FUNCTIONS_AGENTS_SESSION_DIR"] = str(root / "sessions")
os.environ["AZURE_FUNCTIONS_AGENTS_PROVIDER"] = "openai"
os.environ["AZURE_FUNCTIONS_AGENTS_MODEL"] = "offline"
os.environ["OPENAI_API_KEY"] = "offline-fixture"
os.environ["COPILOT_SKIP_CLI_DOWNLOAD"] = "1"
for name in (
    "AzureWebJobsStorage", "AzureWebJobsStorage__blobServiceUri", "WEBSITE_INSTANCE_ID",
    "WEBSITE_OWNER_NAME", "WEBSITE_DEPLOYMENT_ID", "WEBSITE_SITE_NAME",
    "FUNCTIONS_WORKER_PROCESS_COUNT", "AZURE_FUNCTIONS_AGENTS_REASONING_EFFORT",
    "AZURE_FUNCTIONS_AGENTS_REASONING_SUMMARY",
):
    os.environ.pop(name, None)

import azure_functions_agents as runtime
from azure_functions_agents import runner
from azure_functions_agents.harness import _harness_binding as _harness

async def run():
    harness = _harness.get_harness(root, new_app=True)
    if selected == "copilot":
        import copilot
        from tests.test_copilot_execution import _fake_client
        client = _fake_client()
        copilot.CopilotClient = lambda **kwargs: client
        result = await runner.run_agent("offline", tools=[], mcp_tools=[], _harness=harness)
        assert result.content == "synthetic reply"
        await runtime.shutdown_client_manager()
        client.stop.assert_awaited_once()
    else:
        from agent_framework import Message
        from azure_functions_agents.harness.agent_framework import _maf_execution
        provider = _maf_execution._build_history_provider("agent")
        await provider.save_messages("session", [Message("user", ["ordinary MAF history"])])
        assert [message.text for message in await provider.get_messages("session")] == [
            "ordinary MAF history"
        ]
        await runtime.shutdown_client_manager()
    assert all(sys.modules.get(name) is None for name in opposite)
    print(json.dumps({"selected": harness.name.value, "opposite_loaded": False}))

asyncio.run(run())
"""
    repository = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", script, selected, str(root)],
        cwd=repository,
        env={**os.environ, "PYTHONPATH": str(repository / "src")},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"selected": selected, "opposite_loaded": False}
