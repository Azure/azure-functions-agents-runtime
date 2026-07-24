import json
from pathlib import Path
from typing import Any

import azure.durable_functions as df

from azure_functions_agents.app import create_function_app
from azure_functions_agents.config.loader import load_agent_specs
from azure_functions_agents.discovery.tools import (
    clear_tool_discovery_cache,
    discover_project_tools,
)

SAMPLE_SRC = (
    Path(__file__).resolve().parents[1] / "samples" / "workflow-subagents-preview" / "src"
)


def _workflow_tools() -> dict[str, Any]:
    clear_tool_discovery_cache()
    return {
        tool.name: tool.handler
        for tool in discover_project_tools(SAMPLE_SRC).workflow_tools
    }


def _user_tools() -> dict[str, Any]:
    clear_tool_discovery_cache()
    return {
        tool.name: tool.func
        for tool in discover_project_tools(SAMPLE_SRC).user_tools
    }


def test_sample_declares_queue_workflow_subagents() -> None:
    specs = load_agent_specs(SAMPLE_SRC)
    main = next(spec for spec in specs if spec.is_main)

    assert main.trigger is not None
    assert main.trigger.type == "queue_trigger"
    assert main.trigger.args == {
        "queue_name": "pr-status-requests",
        "connection": "AzureWebJobsStorage",
    }
    assert main.workflows is not None
    assert main.workflows.enabled is True
    assert [ref.agent for ref in main.workflows.subagents] == [
        "pr_status_analyst",
        "actionable_report_writer",
    ]
    assert {Path(spec.source_file).name for spec in specs} == {
        "main.agent.md",
        "pr_status_analyst.agent.md",
        "actionable_report_writer.agent.md",
    }


def test_sample_is_runnable_and_indexes_subagent_activity() -> None:
    app = create_function_app(app_root=SAMPLE_SRC)

    assert isinstance(app, df.DFApp)
    functions = {
        builder._function._name: [
            binding.get_dict_repr() for binding in builder._function._bindings
        ]
        for builder in app._function_builders
    }
    assert {
        "agents_workflow_run_sub_agent",
        "agents_workflow_run_tool",
        "agents_workflow_orchestrator",
        "handler_PR_Status_Portfolio_Coordinator",
    } <= functions.keys()
    assert [
        binding["type"]
        for binding in functions["handler_PR_Status_Portfolio_Coordinator"]
    ] == ["durableClient", "queueTrigger"]


def test_sample_runtime_files_and_customer_readme_are_complete() -> None:
    for name in (
        ".funcignore",
        "function_app.py",
        "host.json",
        "local.settings.template.json",
        "requirements.txt",
    ):
        assert (SAMPLE_SRC / name).is_file()

    readme = (SAMPLE_SRC.parent / "README.md").read_text(encoding="utf-8")
    for internal_term in (
        "design preview",
        "not a runnable sample",
        "FRD",
        "Durable Activity",
        "child orchestrator",
        "at-least-once",
    ):
        assert internal_term not in readme

    settings = json.loads(
        (SAMPLE_SRC / "local.settings.template.json").read_text(encoding="utf-8")
    )
    assert settings["Values"]["AzureWebJobsStorage"] == "UseDevelopmentStorage=true"
    assert settings["Values"]["PR_STATUS_REPORT_CONTAINER"] == "workflow-reports"


def test_fake_pr_tools_are_deterministic() -> None:
    tools = _user_tools()
    url = "https://github.com/Azure/azure-functions-host/pull/123"

    assert tools["get_pull_request_status"](url) == tools["get_pull_request_status"](url)
    assert tools["get_pull_request_activity"](url) == tools[
        "get_pull_request_activity"
    ](url)


def test_report_publisher_repeatedly_overwrites_same_blob(monkeypatch: Any) -> None:
    publish = _workflow_tools()["publish_pr_status_report"]
    uploads: list[tuple[str, bytes, bool, str]] = []

    class FakeBlob:
        url = "http://127.0.0.1/workflow-reports/reports/pr-status.html"

        def __init__(self, name: str) -> None:
            self.name = name

        def upload_blob(
            self,
            data: bytes,
            *,
            overwrite: bool,
            content_settings: Any,
        ) -> None:
            uploads.append(
                (self.name, data, overwrite, content_settings.content_type)
            )

    class FakeContainer:
        def create_container(self) -> None:
            return None

        def get_blob_client(self, name: str) -> FakeBlob:
            return FakeBlob(name)

    class FakeService:
        def get_container_client(self, name: str) -> FakeContainer:
            assert name == "workflow-reports"
            return FakeContainer()

    monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")
    monkeypatch.setattr(
        "azure.storage.blob.BlobServiceClient.from_connection_string",
        lambda value: FakeService(),
    )

    args = {
        "html": "<html><body>Portfolio</body></html>",
        "blob_name": "reports/pr-status.html",
    }
    first = publish(args)
    second = publish(args)

    assert first["blob_name"] == second["blob_name"] == "reports/pr-status.html"
    assert uploads == [
        (
            "reports/pr-status.html",
            b"<html><body>Portfolio</body></html>",
            True,
            "text/html; charset=utf-8",
        ),
        (
            "reports/pr-status.html",
            b"<html><body>Portfolio</body></html>",
            True,
            "text/html; charset=utf-8",
        ),
    ]
