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
    sys.path.insert(0, str(script.parent))
    spec = importlib.util.spec_from_file_location(module_name, script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    sys.path.pop(0)
    return module


def _load_sender() -> Any:
    module_name = "workflow_token_benchmark_sender_test"
    script = SAMPLE_SRC.parent / "scripts" / "send_trial.py"
    spec = importlib.util.spec_from_file_location(module_name, script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_evaluation() -> Any:
    module_name = "workflow_token_benchmark_evaluation_test"
    script = SAMPLE_SRC.parent / "scripts" / "evaluation.py"
    spec = importlib.util.spec_from_file_location(module_name, script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_grader() -> Any:
    module_name = "workflow_token_benchmark_grader_test"
    script = SAMPLE_SRC.parent / "scripts" / "grade_quality.py"
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
    requirements = (SAMPLE_SRC / "requirements.txt").read_text(encoding="utf-8")
    assert "azure-storage-queue==12.13.*,<13" in requirements


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


def test_manual_sender_builds_matching_queue_requests() -> None:
    sender = _load_sender()

    requests = sender.build_requests(
        trial_id="manual-comparison",
        service_count=2,
        evidence_lines=40,
        modes=("baseline", "workflow"),
    )

    assert requests == [
        (
            "baseline",
            {
                "trial_id": "manual-comparison",
                "services": ["checkout-api-00", "checkout-api-01"],
                "evidence_lines": 40,
                "report_blob": "runs/manual-comparison/baseline.json",
            },
        ),
        (
            "workflow",
            {
                "trial_id": "manual-comparison",
                "services": ["checkout-api-00", "checkout-api-01"],
                "evidence_lines": 40,
                "report_blob": "runs/manual-comparison/workflow.json",
            },
        ),
    ]


def test_manual_sender_expands_development_storage_connection() -> None:
    sender = _load_sender()

    connection = sender._development_storage_connection()

    assert "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1" in connection
    assert "QueueEndpoint=http://127.0.0.1:10001/devstoreaccount1" in connection


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


def test_quality_oracle_matches_complete_canonical_report() -> None:
    core = _load_core()
    evaluation = _load_evaluation()
    services = ["checkout-api-00", "checkout-api-01"]
    expected = evaluation.build_expected_report(
        trial_id="quality-trial",
        services=services,
        evidence_lines=10,
    )
    actual = core.build_canonical_report(
        "quality-trial",
        [
            core.build_service_evidence("quality-trial", service, 10)
            for service in services
        ],
    )

    quality = evaluation.score_report(actual, expected)

    assert actual == expected
    assert quality.score == 1.0
    assert quality.exact is True
    assert quality.matching_fields == quality.compared_fields


def test_quality_score_penalizes_missing_wrong_and_extra_fields() -> None:
    evaluation = _load_evaluation()
    expected = {"trial_id": "trial", "services": [{"service": "one", "errors": 2}]}
    candidate = {
        "trial_id": "trial",
        "services": [{"service": "one", "errors": 9}],
        "fabricated": True,
    }

    quality = evaluation.score_report(candidate, expected)

    assert quality.matching_fields == 2
    assert quality.compared_fields == 4
    assert quality.score == 0.5
    assert quality.exact is False


def test_quality_score_penalizes_reordered_services() -> None:
    evaluation = _load_evaluation()
    expected = {
        "services": [
            {"service": "one", "errors": 1},
            {"service": "two", "errors": 2},
        ]
    }
    candidate = {
        "services": [
            {"service": "two", "errors": 2},
            {"service": "one", "errors": 1},
        ]
    }

    quality = evaluation.score_report(candidate, expected)

    assert quality.score == 0.0
    assert quality.exact is False


def test_atif_export_contains_reference_candidate_metrics_and_quality() -> None:
    evaluation = _load_evaluation()
    quality = evaluation.QualityScore(
        score=1.0,
        exact=True,
        matching_fields=10,
        compared_fields=10,
    )
    trajectory = evaluation.build_atif_trajectory(
        trial_id="trial",
        request={"trial_id": "trial", "services": ["one"], "evidence_lines": 5},
        report={"trial_id": "trial"},
        expected_report={"trial_id": "trial"},
        quality=quality,
        report_latency_ms=1234,
        input_tokens=100,
        output_tokens=20,
        model="gpt-test",
    )

    assert trajectory["schema_version"] == "ATIF-v1.7"
    assert trajectory["session_id"] == "trial-candidate"
    assert trajectory["agent"]["name"] == "workflow-token-benchmark"
    assert "benchmark_mode" not in trajectory["extra"]
    assert json.loads(trajectory["steps"][0]["message"])["reference_report"] == {
        "trial_id": "trial"
    }
    assert json.loads(trajectory["steps"][1]["message"]) == {"trial_id": "trial"}
    assert trajectory["final_metrics"]["total_prompt_tokens"] == 100
    assert trajectory["final_metrics"]["total_completion_tokens"] == 20
    assert trajectory["final_metrics"]["extra"] == {
        "report_latency_ms": 1234,
        "deterministic_quality_score": 1.0,
        "deterministic_quality_exact": True,
    }


def test_run_mode_records_report_ready_latency_before_usage_wait(
    monkeypatch: Any,
) -> None:
    benchmark = _load_benchmark()

    class FakeQueue:
        def clear_messages(self) -> None:
            return None

        def send_message(self, message: str) -> None:
            assert json.loads(message)["trial_id"] == "trial"

    class FakeContainer:
        def delete_blob(self, name: str) -> None:
            assert name == "runs/trial/baseline.json"

    class FakeHost:
        def mark(self) -> int:
            return 7

        def wait_for_usage(self, *args: Any, **kwargs: Any) -> Any:
            return benchmark.Usage(
                agent_name="baseline",
                execution_role="primary",
                input_tokens=100,
                output_tokens=20,
                provider="foundry",
                model="gpt-test",
                usage_details={"input_token_count": 100, "output_token_count": 20},
            )

    times = iter([10.0, 12.0, 14.0])
    monkeypatch.setattr(benchmark.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(
        benchmark,
        "_wait_for_blob",
        lambda *args, **kwargs: ({"trial_id": "trial"}, b"{}"),
    )

    result = benchmark._run_mode(
        mode="baseline",
        request={
            "trial_id": "trial",
            "report_blob": "runs/trial/baseline.json",
        },
        queue_client=FakeQueue(),
        container=FakeContainer(),
        host=FakeHost(),
        timeout=30,
    )

    assert result.report_latency_ms == 2000
    assert result.elapsed_ms == 4000


def test_finalize_pair_preserves_report_quality_when_telemetry_fails() -> None:
    benchmark = _load_benchmark()
    expected = {"trial_id": "trial", "services": [{"service": "one"}]}
    baseline = benchmark.ModeResult(
        mode="baseline",
        agent_name="baseline",
        report_json=expected,
        report_latency_ms=100,
        error="token usage record did not arrive",
    )
    workflow = benchmark.ModeResult(
        mode="workflow",
        agent_name="workflow",
        report_json=expected,
        report_latency_ms=80,
        total_tokens=50,
    )
    pair = benchmark.PairResult(
        trial_id="trial",
        service_count=1,
        evidence_lines=5,
        execution_order=["baseline", "workflow"],
        baseline=baseline,
        workflow=workflow,
    )

    benchmark._finalize_pair(pair, expected)

    assert pair.reports_equal is True
    assert pair.reduction is None
    assert baseline.quality_score == 1.0
    assert baseline.quality_exact is True
    assert workflow.quality_score == 1.0
    assert workflow.quality_exact is True


def test_vally_grader_uses_resolved_npx_and_writes_jsonl(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    grader = _load_grader()
    atif_dir = tmp_path / "atif"
    atif_dir.mkdir()
    trajectory = atif_dir / "services-1-repeat-1-baseline.json"
    trajectory.write_text('{"schema_version":"ATIF-v1.7"}\n', encoding="utf-8")
    calls: list[tuple[list[str], str, int]] = []

    class Completed:
        returncode = 0
        stdout = '{"status":"success"}\n'
        stderr = ""

    def fake_run(
        command: list[str],
        **kwargs: Any,
    ) -> Completed:
        calls.append((command, kwargs["input"], kwargs["timeout"]))
        return Completed()

    monkeypatch.setattr(grader.shutil, "which", lambda name: "C:\\tools\\npx.cmd")
    monkeypatch.setattr(grader.subprocess, "run", fake_run)

    outputs = grader.grade_trajectories(
        results_dir=tmp_path,
        judge_model="gpt-test",
        repeat=1,
    )

    assert calls[0][0][0] == "C:\\tools\\npx.cmd"
    assert calls[0][0][-2:] == ["--judge-model", "gpt-test"]
    assert calls[0][1] == '{"schema_version":"ATIF-v1.7"}\n'
    assert calls[0][2] == 600
    assert outputs[0].read_text(encoding="utf-8") == '{"status":"success"}\n'
