import json
from pathlib import Path

import azure.durable_functions as df
import pytest

from azure_functions_agents.app import create_function_app
from azure_functions_agents.config.loader import load_agent_specs
from azure_functions_agents.discovery.tools import (
    clear_tool_discovery_cache,
    discover_project_tools,
)

SAMPLE_SRC = (
    Path(__file__).resolve().parents[1] / "samples" / "workflow-timer-trigger" / "src"
)


def test_workflow_timer_sample_exists() -> None:
    assert SAMPLE_SRC.is_dir()


def test_workflow_timer_sample_uses_dts_emulator_backend() -> None:
    host_config = json.loads((SAMPLE_SRC / "host.json").read_text(encoding="utf-8"))
    durable_config = host_config["extensions"]["durableTask"]
    assert durable_config == {
        "hubName": "default",
        "storageProvider": {
            "type": "azureManaged",
            "connectionStringName": "DURABLE_TASK_SCHEDULER_CONNECTION_STRING",
        },
    }
    assert host_config["extensionBundle"]["version"] == "[4.32.0, 5.0.0)"

    local_settings = json.loads(
        (SAMPLE_SRC / "local.settings.template.json").read_text(encoding="utf-8")
    )
    assert local_settings["Values"]["DURABLE_TASK_SCHEDULER_CONNECTION_STRING"] == (
        "Endpoint=http://localhost:8080;TaskHub=default;Authentication=None"
    )


def test_workflow_timer_sample_declares_workflow_timer() -> None:
    [spec] = load_agent_specs(SAMPLE_SRC)

    assert spec.is_main is True
    assert spec.trigger is not None
    assert spec.trigger.type == "timer_trigger"
    assert spec.trigger.args == {"schedule": "0 */5 * * * *"}
    assert spec.workflows == {"enabled": True}


def test_workflow_timer_sample_tools_are_discoverable() -> None:
    clear_tool_discovery_cache()
    discovered = discover_project_tools(SAMPLE_SRC)

    assert discovered.user_tools == []
    assert {tool.name for tool in discovered.workflow_tools} == {
        "capture_timer_event",
        "publish_timer_result",
    }


def test_workflow_timer_sample_indexes_timer_and_durable_functions() -> None:
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
        "handler_Scheduled_Workflow_Starter",
    } <= functions.keys()
    trigger_bindings = functions["handler_Scheduled_Workflow_Starter"]
    assert [binding["type"] for binding in trigger_bindings] == [
        "durableClient",
        "timerTrigger",
    ]
    assert trigger_bindings[1]["schedule"] == "0 */5 * * * *"
    assert trigger_bindings[1].get("runOnStartup") is not True


def test_workflow_timer_sample_terminal_sink_logs_marker(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clear_tool_discovery_cache()
    discovered = discover_project_tools(SAMPLE_SRC)
    publish = next(
        tool for tool in discovered.workflow_tools if tool.name == "publish_timer_result"
    )
    assert publish.handler is not None

    result = publish.handler(
        {
            "capture": {"captured_at": "2026-07-17T00:00:00+00:00"},
            "pause": {"waited_until": "2026-07-17T00:00:05+00:00"},
        }
    )

    assert result["event"] == "timer_workflow_completed"
    assert "TIMER_WORKFLOW_COMPLETED" in caplog.text
