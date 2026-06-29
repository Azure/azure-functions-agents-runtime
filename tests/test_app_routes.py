import json
from pathlib import Path

import azure.durable_functions as df
import pytest

from azure_functions_agents import app as app_module
from azure_functions_agents.workflows import context as workflow_context
from azure_functions_agents.workflows import tools as workflow_tools


def _write_main_agent(tmp_path: Path, *, workflows: bool = False) -> None:
    workflows_block = "workflows:\n  enabled: true\n" if workflows else ""
    (tmp_path / "main.agent.md").write_text(
        (
            "---\n"
            "name: Main\n"
            "description: Main agent\n"
            "builtin_endpoints: true\n"
            f"{workflows_block}"
            "---\n"
            "Test agent\n"
        ),
        encoding="utf-8",
    )


class _WorkflowRequest:
    session_id = "session-1"

    def __init__(self) -> None:
        self.headers = {"x-ms-session-id": self.session_id}
        self.query_params = {
            "workflow_id": workflow_context.new_workflow_instance_id(self.session_id)
        }


def _registered_function(function_app, name):
    for builder in function_app._function_builders:
        function = builder._function
        if function._name == name:
            return getattr(function._func, "__wrapped__", function._func)
    raise AssertionError(f"function {name!r} was not registered")


def _binding_types(function_app, name):
    for builder in function_app._function_builders:
        function = builder._function
        if function._name == name:
            return [binding.get_dict_repr()["type"] for binding in function._bindings]
    raise AssertionError(f"function {name!r} was not registered")


def test_non_workflow_app_does_not_use_durable_function_app(tmp_path: Path):
    _write_main_agent(tmp_path)

    function_app = app_module.create_function_app(app_root=tmp_path)

    assert not isinstance(function_app, df.DFApp)


def test_workflow_app_uses_durable_function_app(tmp_path: Path):
    _write_main_agent(tmp_path, workflows=True)

    function_app = app_module.create_function_app(app_root=tmp_path)

    assert isinstance(function_app, df.DFApp)


def test_non_workflow_routes_do_not_register_durable_client_binding(tmp_path: Path):
    _write_main_agent(tmp_path)

    function_app = app_module.create_function_app(app_root=tmp_path)

    for function_name in [
        "chat",
        "chat_stream",
        "mcp_agent_chat",
    ]:
        assert "durableClient" not in _binding_types(function_app, function_name)


def test_workflow_routes_register_durable_client_binding(tmp_path: Path):
    _write_main_agent(tmp_path, workflows=True)

    function_app = app_module.create_function_app(app_root=tmp_path)

    for function_name in [
        "chat",
        "chat_stream",
        "mcp_agent_chat",
        "list_session_workflows",
        "get_session_workflow_status",
    ]:
        assert "durableClient" in _binding_types(function_app, function_name)


@pytest.mark.asyncio
async def test_workflow_list_endpoint_logs_exception_without_returning_details(
    tmp_path: Path, monkeypatch, caplog
):
    _write_main_agent(tmp_path, workflows=True)
    secret_message = "storage account secret details"

    async def fail_fetch(*args, **kwargs):
        raise RuntimeError(secret_message)

    monkeypatch.setattr(workflow_tools, "fetch_session_workflows", fail_fetch)
    function_app = app_module.create_function_app(app_root=tmp_path)
    list_workflows = _registered_function(function_app, "list_session_workflows")

    response = await list_workflows(_WorkflowRequest(), client=object())

    assert response.status_code == 500
    body = json.loads(response.body)
    assert body == {"error": "failed to list workflows"}
    assert secret_message not in response.body.decode()
    assert any(
        record.message == "workflows list endpoint failed"
        and record.exc_info
        and secret_message in str(record.exc_info[1])
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_workflow_status_endpoint_logs_exception_without_returning_details(
    tmp_path: Path, monkeypatch, caplog
):
    _write_main_agent(tmp_path, workflows=True)
    secret_message = "durable backend internal details"

    async def fail_fetch(*args, **kwargs):
        raise RuntimeError(secret_message)

    monkeypatch.setattr(workflow_tools, "fetch_session_workflow_status", fail_fetch)
    function_app = app_module.create_function_app(app_root=tmp_path)
    workflow_status = _registered_function(
        function_app,
        "get_session_workflow_status",
    )

    response = await workflow_status(_WorkflowRequest(), client=object())

    assert response.status_code == 500
    body = json.loads(response.body)
    assert body == {"error": "failed to fetch workflow status"}
    assert secret_message not in response.body.decode()
    assert any(
        record.message == "workflow status endpoint failed"
        and record.exc_info
        and secret_message in str(record.exc_info[1])
        for record in caplog.records
    )
