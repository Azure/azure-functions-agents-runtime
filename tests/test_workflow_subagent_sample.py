import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import azure.durable_functions as df
import pytest

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


def test_coordinator_example_passes_each_review_to_writer() -> None:
    main = next(spec for spec in load_agent_specs(SAMPLE_SRC) if spec.is_main)
    example = json.loads(main.instructions.split("```json\n", 1)[1].split("```", 1)[0])

    assert example["agent"] == "actionable_report_writer"
    assert example["type"] == "sub_agent"
    for task_id in example["depends_on"]:
        assert f"${{{task_id}.result.text}}" in example["task"]
    assert "pull_request_urls" in main.instructions


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
    assert settings["Values"]["DURABLE_TASK_SCHEDULER_CONNECTION_STRING"] == (
        "Endpoint=http://localhost:8080;Authentication=None"
    )
    assert settings["Values"]["TASKHUB_NAME"] == "prstatusreports"
    assert settings["Values"]["PR_STATUS_REPORT_CONTAINER"] == "workflow-reports"

    host = json.loads((SAMPLE_SRC / "host.json").read_text(encoding="utf-8"))
    assert host["telemetryMode"] == "OpenTelemetry"
    durable_task = host["extensions"]["durableTask"]
    assert durable_task == {
        "hubName": "%TASKHUB_NAME%",
        "tracing": {
            "distributedTracingEnabled": True,
            "version": "V2",
        },
        "storageProvider": {
            "type": "azureManaged",
            "connectionStringName": "DURABLE_TASK_SCHEDULER_CONNECTION_STRING",
        },
    }

    assert "mcr.microsoft.com/dts/dts-emulator:latest" in readme
    assert "DTS_TASK_HUB_NAMES=prstatusreports" in readme
    assert "DTS_USE_DYNAMIC_TASK_HUBS" not in readme
    assert "docker rm -f workflow-subagents-dts" in readme
    assert "-p 8080:8080 -p 8082:8082" in readme
    assert "http://localhost:8082" in readme
    assert "Azurite must still be running" in readme
    assert "Restart the Functions host" in readme
    assert "python scripts/verify.py" in readme


def test_fake_pr_tools_are_deterministic() -> None:
    tools = _user_tools()
    url = "https://github.com/Azure/azure-functions-host/pull/123"

    assert tools["get_pull_request_status"](url) == tools["get_pull_request_status"](url)
    assert tools["get_pull_request_activity"](url) == tools[
        "get_pull_request_activity"
    ](url)


@pytest.mark.parametrize(
    "html",
    [
        "I am missing the pull-request summaries.",
        "<p>Please provide the summaries.</p>",
        "<html><body>Incomplete",
        "```html\n<html><body>Report</body></html>\n```",
        "<html><body>Report without links</body></html>",
        "<html>Report without body</html>",
        "<html><body>https://github.com/Azure/azure-functions-host/pull/123 "
        "https://github.com/Azure/azure-functions-python-worker/pull/456</body></html>",
        '<html><body><a href="https://github.com/Azure/azure-functions-host/pull/123">PR</a></body></html>',
    ],
)
def test_report_publisher_rejects_invalid_report_before_storage(
    monkeypatch: Any, html: str,
) -> None:
    publish = _workflow_tools()["publish_pr_status_report"]
    connect = Mock()
    monkeypatch.setattr(
        "azure.storage.blob.BlobServiceClient.from_connection_string", connect,
    )
    monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")

    with pytest.raises(ValueError, match=r"HTML|pull-request"):
        publish({
            "html": html,
            "blob_name": "reports/pr-status.html",
            "pull_request_urls": [
                "https://github.com/Azure/azure-functions-host/pull/123",
                "https://github.com/Azure/azure-functions-python-worker/pull/456",
            ],
        })

    connect.assert_not_called()


@pytest.mark.parametrize("urls", [None, [], "", [""]])
def test_report_publisher_requires_requested_urls(monkeypatch: Any, urls: Any) -> None:
    publish = _workflow_tools()["publish_pr_status_report"]
    connect = Mock()
    monkeypatch.setattr(
        "azure.storage.blob.BlobServiceClient.from_connection_string", connect,
    )
    monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")

    with pytest.raises(ValueError, match="pull_request_urls"):
        publish({
            "html": "<html><body>Report</body></html>",
            "blob_name": "reports/pr-status.html",
            "pull_request_urls": urls,
        })

    connect.assert_not_called()


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

    url = "https://github.com/Azure/azure-functions-host/pull/123"
    html = f'<!doctype html><html><body><a href="{url}">PR</a></body></html>'
    args = {
        "html": html,
        "blob_name": "reports/pr-status.html",
        "pull_request_urls": [url],
    }
    first = publish(args)
    second = publish(args)

    assert first["blob_name"] == second["blob_name"] == "reports/pr-status.html"
    assert uploads == [
        (
            "reports/pr-status.html",
            html.encode("utf-8"),
            True,
            "text/html; charset=utf-8",
        ),
        (
            "reports/pr-status.html",
            html.encode("utf-8"),
            True,
            "text/html; charset=utf-8",
        ),
    ]
