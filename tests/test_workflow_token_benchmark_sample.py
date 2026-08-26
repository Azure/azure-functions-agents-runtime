import importlib.util
import json
import sys
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
    Path(__file__).resolve().parents[1] / "samples" / "workflow-token-benchmark" / "src"
)
TOOLS_DIR = SAMPLE_SRC / "tools"


def _load_core() -> Any:
    module_name = "workflow_token_benchmark_core_test"
    spec = importlib.util.spec_from_file_location(module_name, TOOLS_DIR / "_benchmark_core.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_benchmark() -> Any:
    module_name = "workflow_token_benchmark_script_test"
    script = SAMPLE_SRC.parent / "scripts" / "benchmark.py"
    spec = importlib.util.spec_from_file_location(module_name, script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_sample_declares_paired_queue_agents() -> None:
    specs = {Path(spec.source_file).name: spec for spec in load_agent_specs(SAMPLE_SRC)}

    assert set(specs) == {"baseline.agent.md", "workflow.agent.md"}
    baseline = specs["baseline.agent.md"]
    workflow = specs["workflow.agent.md"]
    assert baseline.trigger is not None
    assert baseline.trigger.type == "queue_trigger"
    assert baseline.trigger.args["queue_name"] == "token-benchmark-baseline"
    assert baseline.workflows is None
    assert baseline.tools is True
    assert workflow.trigger is not None
    assert workflow.trigger.type == "queue_trigger"
    assert workflow.trigger.args["queue_name"] == "token-benchmark-workflow"
    assert workflow.tools is False
    assert workflow.workflows is not None
    assert workflow.workflows.enabled is True
    assert workflow.workflows.subagents == ()
    assert "Do not use a Sub Agent" in workflow.instructions


def test_sample_dual_registers_the_same_tools() -> None:
    clear_tool_discovery_cache()
    discovered = discover_project_tools(SAMPLE_SRC)

    assert {tool.name for tool in discovered.user_tools} == {
        "inspect_service_evidence",
        "publish_benchmark_report",
    }
    assert {tool.name for tool in discovered.workflow_tools} == {
        "inspect_service_evidence",
        "publish_benchmark_report",
    }
    assert discovered.failed_loads == []


def test_sample_indexes_both_queues_and_durable_runtime() -> None:
    app = create_function_app(app_root=SAMPLE_SRC)

    assert isinstance(app, df.DFApp)
    functions = {
        builder._function._name: [
            binding.get_dict_repr() for binding in builder._function._bindings
        ]
        for builder in app._function_builders
    }
    assert {
        "agents_workflow_run_tool",
        "agents_workflow_orchestrator",
        "handler_Token_Benchmark_Baseline",
        "handler_Token_Benchmark_Dynamic_Workflow",
    } <= functions.keys()


def test_evidence_and_canonical_report_are_deterministic() -> None:
    core = _load_core()
    services = ["checkout-api-00", "checkout-api-01"]
    first = [
        core.build_service_evidence("trial-1", service, 40) for service in services
    ]
    second = [
        core.build_service_evidence("trial-1", service, 40) for service in services
    ]

    assert first == second
    first_report = core.build_canonical_report("trial-1", first)
    second_report = core.build_canonical_report("trial-1", second)
    assert core.canonical_json_bytes(first_report) == core.canonical_json_bytes(
        second_report
    )
    assert [service["service"] for service in first_report["services"]] == services
    assert len(first[0]["logs"]) == 40


def test_publisher_uploads_canonical_json(monkeypatch: Any) -> None:
    clear_tool_discovery_cache()
    tools = {
        tool.name: tool.handler
        for tool in discover_project_tools(SAMPLE_SRC).workflow_tools
    }
    inspect = tools["inspect_service_evidence"]
    publish = tools["publish_benchmark_report"]
    assert inspect is not None and publish is not None
    reports = [
        inspect({"trial_id": "trial-2", "service": service, "evidence_lines": 5})
        for service in ("one", "two")
    ]
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
            assert name == "runs/trial-2.json"
            return FakeBlob()

    class FakeService:
        def get_container_client(self, name: str) -> FakeContainer:
            assert name == "test-token-reports"
            return FakeContainer()

    monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")
    monkeypatch.setenv("TOKEN_BENCHMARK_CONTAINER", "test-token-reports")
    monkeypatch.setattr(
        "azure.storage.blob.BlobServiceClient.from_connection_string",
        lambda value: FakeService(),
    )

    result = publish(
        {
            "trial_id": "trial-2",
            "report_blob": "runs/trial-2.json",
            "service_reports": reports,
        }
    )

    assert len(uploads) == 1
    content, overwrite, content_type = uploads[0]
    assert overwrite is True
    assert content_type == "application/json"
    decoded = json.loads(content)
    assert decoded["trial_id"] == "trial-2"
    assert decoded["service_count"] == 2
    assert result["bytes"] == len(content)


def test_sample_runtime_files_and_isolation_settings() -> None:
    for name in (
        ".funcignore",
        "function_app.py",
        "host.json",
        "local.settings.template.json",
        "requirements.txt",
    ):
        assert (SAMPLE_SRC / name).is_file()

    host = json.loads((SAMPLE_SRC / "host.json").read_text(encoding="utf-8"))
    assert host["extensions"]["queues"] == {
        "batchSize": 1,
        "newBatchThreshold": 0,
        "maxDequeueCount": 1,
        "messageEncoding": "base64",
    }
    settings = json.loads(
        (SAMPLE_SRC / "local.settings.template.json").read_text(encoding="utf-8")
    )
    assert settings["Values"]["TASKHUB_NAME"] == "tokenbenchmark"
    assert settings["Values"]["TOKEN_BENCHMARK_CONTAINER"] == (
        "token-benchmark-reports"
    )
    assert settings["Values"]["AZURE_FUNCTIONS_AGENTS_DETAILED_TOKEN_USAGE"] == "true"


def test_usage_parser_requires_exact_expected_primary() -> None:
    benchmark = _load_benchmark()
    expected = (
        'Agent token usage: {"agent_name":"baseline","event_name":"agent_token_usage",'
        '"execution_role":"primary","input_tokens":100,"model":"gpt-test",'
        '"model_publisher":"openai","output_tokens":20,"provider":"foundry"}\n'
    )
    detail = (
        'Agent token usage detail: {"agent_name":"baseline",'
        '"event_name":"agent_token_usage_detail","execution_role":"primary",'
        '"model":"gpt-test","model_publisher":"openai","provider":"foundry",'
        '"schema_version":1,"usage_details":{"input_token_count":100,'
        '"openai.cached_input_tokens":25,"output_token_count":20,'
        '"total_token_count":120}}\n'
    )
    usage = benchmark.select_trial_usage(
        [
            f"[2026-08-25] {expected.rstrip()}[2026-08-25] next host log\n",
            f"[2026-08-25] {detail.rstrip()}[2026-08-25] next host log\n",
        ],
        expected_agent="baseline",
        workflow_mode=False,
    )
    assert usage.total_tokens == 120
    assert usage.model == "gpt-test"
    assert usage.usage_details["openai.cached_input_tokens"] == 25

    for lines, match in (
        ([], "found 0"),
        ([expected, expected, detail], "found 2"),
        (
            [
                expected.replace('"agent_name":"baseline"', '"agent_name":"workflow"'),
                detail,
            ],
            "other agent",
        ),
        ([expected], "detailed usage record"),
        ([expected, detail, detail], "detailed usage record"),
        (
            [
                expected,
                detail.replace(
                    '"agent_name":"baseline"', '"agent_name":"workflow"'
                ),
            ],
            "other primary agent",
        ),
        (
            [expected, detail.replace('"schema_version":1', '"schema_version":2')],
            "unsupported schema",
        ),
        (
            [
                expected,
                detail.replace(
                    '"openai.cached_input_tokens":25',
                    '"openai.cached_input_tokens":"25"',
                ),
            ],
            "dimensions are invalid",
        ),
        (
            [
                expected,
                detail.replace(
                    '"input_token_count":100', '"input_token_count":101'
                ),
            ],
            "input token count does not match",
        ),
        (
            [
                expected,
                detail.replace(
                    '"output_token_count":20', '"output_token_count":21'
                ),
            ],
            "output token count does not match",
        ),
    ):
        try:
            benchmark.select_trial_usage(
                lines,
                expected_agent="baseline",
                workflow_mode=False,
            )
        except RuntimeError as exc:
            assert match in str(exc)
        else:
            raise AssertionError("invalid usage interval was accepted")


def test_benchmark_uses_runtime_from_current_checkout() -> None:
    benchmark = _load_benchmark()

    assert Path(__file__).resolve().parents[1] / "src" == benchmark.RUNTIME_SRC


def test_host_environment_prepends_runtime_from_current_checkout(
    monkeypatch: Any,
) -> None:
    benchmark = _load_benchmark()
    monkeypatch.setenv("PYTHONPATH", "existing-runtime")

    environment = benchmark._host_environment()

    assert environment["PYTHONPATH"].split(benchmark.os.pathsep) == [
        str(benchmark.RUNTIME_SRC),
        "existing-runtime",
    ]


def test_usage_parser_rejects_workflow_subagent_record() -> None:
    benchmark = _load_benchmark()
    primary = (
        'Agent token usage: {"agent_name":"workflow","event_name":"agent_token_usage",'
        '"execution_role":"primary","input_tokens":100,"output_tokens":20}\n'
    )
    subagent = (
        'Agent token usage: {"agent_name":"analyst","event_name":"agent_token_usage",'
        '"execution_role":"workflow_subagent","input_tokens":10,"output_tokens":5}\n'
    )
    detail = (
        'Agent token usage detail: {"agent_name":"workflow",'
        '"event_name":"agent_token_usage_detail","execution_role":"primary",'
        '"schema_version":1,"usage_details":{"input_token_count":100,'
        '"output_token_count":20}}\n'
    )

    try:
        benchmark.select_trial_usage(
            [primary, subagent, detail],
            expected_agent="workflow",
            workflow_mode=True,
        )
    except RuntimeError as exc:
        assert "forbidden workflow_subagent" in str(exc)
    else:
        raise AssertionError("workflow_subagent usage was accepted")


def test_percentile_interpolates_small_samples() -> None:
    benchmark = _load_benchmark()

    assert benchmark._percentile([0.25], 0.5) == 0.25
    assert benchmark._percentile([0.0, 1.0], 0.25) == 0.25
