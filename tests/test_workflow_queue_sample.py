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
    Path(__file__).resolve().parents[1] / "samples" / "workflow-queue-p0-report" / "src"
)


def _tools() -> dict[str, Any]:
    clear_tool_discovery_cache()
    return {tool.name: tool.handler for tool in discover_project_tools(SAMPLE_SRC).workflow_tools}


def test_workflow_queue_sample_declares_workflow_queue() -> None:
    [spec] = load_agent_specs(SAMPLE_SRC)

    assert spec.is_main is True
    assert spec.trigger is not None
    assert spec.trigger.type == "queue_trigger"
    assert spec.trigger.args == {
        "queue_name": "issue-report-requests",
        "connection": "AzureWebJobsStorage",
    }
    assert spec.workflows == {"enabled": True}


def test_workflow_queue_sample_uses_dts_backend() -> None:
    host_config = json.loads((SAMPLE_SRC / "host.json").read_text(encoding="utf-8"))
    assert host_config["extensions"]["durableTask"] == {
        "hubName": "p0reports",
        "storageProvider": {
            "type": "azureManaged",
            "connectionStringName": "DURABLE_TASK_SCHEDULER_CONNECTION_STRING",
        },
    }


def test_workflow_queue_sample_indexes_queue_and_durable_functions() -> None:
    function_app = create_function_app(app_root=SAMPLE_SRC)

    assert isinstance(function_app, df.DFApp)
    functions = {
        builder._function._name: [
            binding.get_dict_repr() for binding in builder._function._bindings
        ]
        for builder in function_app._function_builders
    }
    assert {
        "agents_workflow_run_tool",
        "agents_workflow_orchestrator",
        "handler_P0_Issue_Portfolio_Reporter",
    } <= functions.keys()
    trigger_bindings = functions["handler_P0_Issue_Portfolio_Reporter"]
    assert [binding["type"] for binding in trigger_bindings] == [
        "durableClient",
        "queueTrigger",
    ]
    assert trigger_bindings[1]["queueName"] == "issue-report-requests"


def test_fake_issue_inspection_is_deterministic() -> None:
    inspect = _tools()["inspect_repository_p0_issues"]
    assert inspect is not None

    first = inspect({"repository": "Azure/azure-functions-host"})
    second = inspect({"repository": "Azure/azure-functions-host"})

    assert first == second
    assert first["repository"] == "Azure/azure-functions-host"
    assert first["p0_count"] == len(first["issues"])


def test_html_renderer_preserves_repository_order_and_escapes() -> None:
    render = _tools()["render_p0_html_report"]
    assert render is not None

    result = render(
        {
            "repository_reports": [
                {"repository": "owner/<first>", "issues": [], "p0_count": 0},
                {
                    "repository": "owner/second",
                    "issues": [
                        {
                            "number": 42,
                            "title": "P0: <unsafe>",
                            "owner": "oncall",
                            "age_hours": 2,
                            "url": "https://example.test/issues/42",
                        }
                    ],
                    "p0_count": 1,
                },
            ]
        }
    )

    assert result["repository_count"] == 2
    assert result["p0_count"] == 1
    assert "owner/&lt;first&gt;" in result["html"]
    assert "P0: &lt;unsafe&gt;" in result["html"]
    assert result["html"].index("owner/&lt;first&gt;") < result["html"].index("owner/second")


def test_blob_publisher_uploads_html(monkeypatch: Any) -> None:
    publish = _tools()["publish_p0_html_report"]
    assert publish is not None
    uploads: list[tuple[bytes, bool, str]] = []

    class FakeBlob:
        def upload_blob(
            self,
            data: bytes,
            *,
            overwrite: bool,
            content_settings: Any,
        ) -> None:
            uploads.append((data, overwrite, content_settings.content_type))

    class FakeContainer:
        def create_container(self) -> None:
            return None

        def get_blob_client(self, name: str) -> FakeBlob:
            assert name == "reports/p0.html"
            return FakeBlob()

    class FakeService:
        def get_container_client(self, name: str) -> FakeContainer:
            assert name == "test-reports"
            return FakeContainer()

    monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")
    monkeypatch.setenv("P0_REPORT_CONTAINER", "test-reports")
    monkeypatch.setattr(
        "azure.storage.blob.BlobServiceClient.from_connection_string",
        lambda value: FakeService(),
    )

    result = publish(
        {
            "report": {
                "html": "<html>report</html>",
                "repository_count": 3,
                "p0_count": 2,
            },
            "blob_name": "reports/p0.html",
        }
    )

    assert uploads == [(b"<html>report</html>", True, "text/html; charset=utf-8")]
    assert result == {
        "event": "p0_report_published",
        "container": "test-reports",
        "blob_name": "reports/p0.html",
        "repository_count": 3,
        "p0_count": 2,
    }
