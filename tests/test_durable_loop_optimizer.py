from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import azure.durable_functions as df
import pytest
from agent_framework import (
    BaseChatClient,
    ChatResponse,
    Content,
    FunctionInvocationLayer,
    Message,
)

from azure_functions_agents import app as app_module
from azure_functions_agents.client_manager import ClientManager, InferenceTarget
from azure_functions_agents.execution.aca_composition import compose_aca_application
from azure_functions_agents.experimental.durable_loop import (
    DurableLoopPlan,
    DurableLoopRunner,
    create_run_identity,
)
from azure_functions_agents.experimental.durable_loop_activities import (
    DeterministicContextCompactor,
    InMemoryDurableContentStore,
    MafOneStepModelProvider,
    OneStepModelRequest,
    ScriptedModelStep,
    _durable_model_options,
    put_protocol_model,
)
from azure_functions_agents.experimental.durable_loop_config import (
    DURABLE_LOOP_DECISION_ROUTING_ENABLED_ENV,
    DURABLE_LOOP_ENABLED_ENV,
    DURABLE_LOOP_FAST_MODEL_ENV,
    DurableLoopSettings,
)
from azure_functions_agents.experimental.durable_loop_optimizer import (
    COMPILE_SEGMENT_DAG_TOOL_NAME,
    DecisionEvaluation,
    DurableSegmentOptimizer,
    HeuristicDecisionOneClient,
    ModelTier,
    PrefixCacheLedger,
    SegmentMode,
    build_decision_one_payload,
    parse_decision_one_response,
)
from azure_functions_agents.experimental.durable_loop_protocol import (
    DURABLE_LOOP_ORCHESTRATOR_V2_NAME,
    CheckpointStateV1,
    DurableLoopPlanDocumentV1,
    DurableLoopRunStatus,
    DurableOrchestrationInputV1,
    DurableRunDocumentV1,
    FrozenToolDescriptorV1,
    MAFMessageBundleV1,
    ModelDecisionEnvelopeV1,
    ToolBehavior,
    ToolProvenance,
    WorkingContextV1,
    canonical_hash,
)
from azure_functions_agents.experimental.durable_loop_registration import (
    DURABLE_LOOP_APPEND_ACTIVITY_NAME,
    DURABLE_LOOP_DAG_WAVE_ACTIVITY_NAME,
    DURABLE_LOOP_MCP_TOOL_ACTIVITY_NAME,
    DURABLE_LOOP_MODEL_ACTIVITY_NAME,
    DURABLE_LOOP_SANDBOX_TOOL_ACTIVITY_NAME,
    DurableLoopActivityRuntime,
    reset_durable_loop_activity_runtime_factory,
    set_durable_loop_activity_runtime_factory,
)
from azure_functions_agents.experimental.durable_loop_registration import (
    DURABLE_LOOP_ORCHESTRATOR_V2_NAME as REGISTERED_ORCHESTRATOR_V2_NAME,
)
from azure_functions_agents.experimental.durable_loop_state import (
    InMemoryDurableLoopStateStore,
)
from azure_functions_agents.experimental.durable_loop_tools import (
    DurableToolRegistry,
)
from azure_functions_agents.strict_json import canonical_json_bytes


class _RecordingOneStepProvider:
    """Record requested model per step and return scripted decisions."""

    def __init__(self, steps: Sequence[ScriptedModelStep]) -> None:
        self._steps = tuple(steps)
        self.requests: list[OneStepModelRequest] = []
        self.models_seen: list[str] = []

    async def run_one_step(
        self,
        request: OneStepModelRequest,
    ) -> ModelDecisionEnvelopeV1:
        self.requests.append(request)
        routed = request.model_settings.get("_routed_model")
        model_name = (
            str(routed)
            if isinstance(routed, str) and routed
            else request.working_context.bundle.model
        )
        self.models_seen.append(model_name)
        step = self._steps[request.step_index]
        return step.build(request)


class _MultiModelChatClient(FunctionInvocationLayer[Any], BaseChatClient[Any]):
    """Fake MAF chat client that records the model and options used."""

    def __init__(self, model_name: str, text: str) -> None:
        super().__init__(function_invocation_configuration={"enabled": False})
        self.model_name = model_name
        self._text = text
        self.received_options: list[dict[str, Any]] = []
        self.received_messages: list[list[Message]] = []

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any:
        del stream, kwargs
        self.received_options.append(dict(options))
        self.received_messages.append(list(messages))

        async def response() -> ChatResponse[Any]:
            return ChatResponse(
                messages=[Message("assistant", [Content.from_text(self._text)])],
                response_id=f"resp-{self.model_name}",
                finish_reason="stop",
                usage_details={"input_token_count": 600, "output_token_count": 80},
            )

        return response()


class _MultiModelClientManager(ClientManager):
    """Return a distinct client and target for Frontier vs. Fast models."""

    def __init__(self) -> None:
        self.requested_models: list[str] = []
        self.clients: dict[str, _MultiModelChatClient] = {}

    def resolve_model(self, requested: str | None) -> str:
        return requested or "gpt-6-astra"

    def build_chat_client(self, model: str | None) -> Any:
        resolved = self.resolve_model(model)
        self.requested_models.append(resolved)
        client = _MultiModelChatClient(resolved, f"answered-by-{resolved}")
        self.clients[resolved] = client
        return client

    def build_chat_client_with_target(
        self,
        model: str | None,
    ) -> tuple[Any, InferenceTarget]:
        resolved = self.resolve_model(model)
        client = self.build_chat_client(resolved)
        return (
            client,
            InferenceTarget(
                provider="azure_openai_apim",
                model=resolved,
                endpoint="https://apim.example.test/openai/v1",
                api_version="responses-v1",
            ),
        )


def _build_triage_registry(call_log: list[tuple[str, dict[str, object]]]) -> DurableToolRegistry:
    registry = DurableToolRegistry()

    def lookup_services(args: Mapping[str, object], _call_key: str) -> dict[str, object]:
        call_log.append(("lookup_services", dict(args)))
        return {
            "incident_id": args.get("incident_id", "INC-402"),
            "primary": "checkout-api",
            "secondary": "payment-worker",
        }

    def fetch_metrics(args: Mapping[str, object], _call_key: str) -> dict[str, object]:
        call_log.append(("fetch_metrics", dict(args)))
        service = str(args.get("service", "unknown"))
        return {
            "service": service,
            "p99_latency_ms": 1840 if service == "checkout-api" else 920,
            "error_rate": 0.14 if service == "checkout-api" else 0.02,
            "raw_series": [i * 10 for i in range(60)],
        }

    def fetch_logs(args: Mapping[str, object], _call_key: str) -> dict[str, object]:
        call_log.append(("fetch_logs", dict(args)))
        service = str(args.get("service", "unknown"))
        return {
            "service": service,
            "top_error": "ConnectionPoolExhausted: upstream payment-worker timeout",
            "log_lines": [f"[{service}] WARN retry #{i} pool saturated" for i in range(40)],
        }

    def correlate_signals(args: Mapping[str, object], _call_key: str) -> dict[str, object]:
        call_log.append(("correlate_signals", dict(args)))
        return {
            "root_cause": "checkout-api pool exhaustion triggered by payment-worker latency",
            "primary_p99_ms": args.get("primary_p99"),
            "secondary_p99_ms": args.get("secondary_p99"),
            "top_error": args.get("error_signature"),
            "recommended_action": "Increase checkout-api pool cap and scale payment-worker",
        }

    for name, handler in (
        ("lookup_services", lookup_services),
        ("fetch_metrics", fetch_metrics),
        ("fetch_logs", fetch_logs),
        ("correlate_signals", correlate_signals),
    ):
        registry.register(
            FrozenToolDescriptorV1(
                name=name,
                description=f"Incident triage tool {name}",
                parameters={"additionalProperties": True, "type": "object"},
                provenance=ToolProvenance.REMOTE,
                behavior=ToolBehavior.READ_ONLY,
                parallel_safe=True,
            ),
            handler,
        )
    return registry


def test_decision_routing_flag_defaults_false_and_parses_env() -> None:
    default_settings = DurableLoopSettings.from_environment(
        {DURABLE_LOOP_ENABLED_ENV: "true"}
    )
    assert default_settings is not None
    assert default_settings.decision_routing_enabled is False

    enabled_settings = DurableLoopSettings.from_environment(
        {
            DURABLE_LOOP_ENABLED_ENV: "true",
            DURABLE_LOOP_DECISION_ROUTING_ENABLED_ENV: "true",
            DURABLE_LOOP_FAST_MODEL_ENV: "gpt-5.4-mini",
        }
    )
    assert enabled_settings is not None
    assert enabled_settings.decision_routing_enabled is True
    assert enabled_settings.fast_model == "gpt-5.4-mini"


def test_decision_one_payload_and_response_schema() -> None:
    payload = build_decision_one_payload(
        decision_model="msft-decision-1",
        state={"step_index": 0, "user_goal": "Triage all services"},
    )
    assert set(payload["questions"]) == {"dag_viable", "step_tier", "dag_tier"}

    parsed = parse_decision_one_response(
        {
            "model": "msft-decision-1",
            "answers": {
                "dag_viable": {"type": "noul", "noul": 0.91},
                "step_tier": {
                    "type": "choice",
                    "choice": "fast",
                    "probabilities": {"fast": 0.88, "frontier": 0.12},
                },
                "dag_tier": {
                    "type": "choice",
                    "choice": "frontier",
                    "probabilities": {"fast": 0.15, "frontier": 0.85},
                },
            },
            "usage": {"input_tokens": 320},
        },
        fallback_input_tokens=100,
    )
    assert parsed == DecisionEvaluation(
        dag_viable=0.91,
        step_tier=ModelTier.FAST,
        step_fast_probability=0.88,
        dag_tier=ModelTier.FRONTIER,
        dag_fast_probability=0.15,
        decision_input_tokens=320,
    )


@pytest.mark.asyncio
async def test_flag_disabled_falls_back_to_single_model_without_decision_calls() -> None:
    call_log: list[tuple[str, dict[str, object]]] = []
    registry = _build_triage_registry(call_log)
    settings = DurableLoopSettings(decision_routing_enabled=False)
    catalog = registry.catalog(policy_hash="1" * 64, package_hash="2" * 64)
    plan = DurableLoopPlan(
        instructions="Triage incident.",
        catalog=catalog,
        model_settings={},
        maf_core_version="1.17.0",
        provider="azure_openai_apim",
        model="gpt-6-astra",
        api_version="responses-v1",
        settings=settings,
    )
    provider = _RecordingOneStepProvider(
        (
            ScriptedModelStep(
                calls=(("call-1", "lookup_services", {"incident_id": "INC-100"}),),
                input_tokens=800,
                output_tokens=120,
            ),
            ScriptedModelStep(
                final_text="Incident INC-100 resolved.",
                input_tokens=1000,
                output_tokens=90,
            ),
        )
    )
    runner = DurableLoopRunner(
        state=InMemoryDurableLoopStateStore(),
        content=InMemoryDurableContentStore(),
        model=provider,
        tools=registry.build_dispatcher(),
    )
    identity = create_run_identity(
        session_id="sess-fallback",
        request_id="req-fallback",
        request_body={"prompt": "Triage INC-100"},
        owner_hash="a" * 64,
        agent_slug="main",
        agent_hash="b" * 64,
        catalog_hash=catalog.catalog_hash,
        deployment_hash="c" * 64,
        tool_package_hash=catalog.package_hash,
        policy_hash=catalog.policy_hash,
        settings=settings,
    )
    result = await runner.start(
        identity,
        plan,
        ({"role": "user", "contents": [{"type": "text", "text": "Triage INC-100"}]},),
    )
    assert result.status.status is DurableLoopRunStatus.COMPLETED
    assert runner.segment_optimizer is None
    assert provider.models_seen == ["gpt-6-astra", "gpt-6-astra"]
    assert all(
        COMPILE_SEGMENT_DAG_TOOL_NAME not in req.catalog.by_name()
        for req in provider.requests
    )


@pytest.mark.asyncio
async def test_maf_provider_switches_models_and_strips_foreign_reasoning_without_hash_error() -> None:
    manager = _MultiModelClientManager()
    maf_provider = MafOneStepModelProvider(manager)
    settings = DurableLoopSettings(
        decision_routing_enabled=True,
        fast_model="gpt-4.1-mini",
    )
    optimizer = DurableSegmentOptimizer(
        maf_provider,
        settings=settings,
    )
    registry = DurableToolRegistry()
    catalog = registry.catalog(policy_hash="1" * 64, package_hash="2" * 64)
    plan = DurableLoopPlan(
        instructions="Summarize result.",
        catalog=catalog,
        model_settings={},
        maf_core_version="1.17.0",
        provider="azure_openai_apim",
        model="gpt-6-astra",
        api_version="responses-v1",
        settings=settings,
    )
    identity = create_run_identity(
        session_id="sess-switch",
        request_id="req-switch",
        request_body={"prompt": "hi"},
        owner_hash="a" * 64,
        agent_slug="main",
        agent_hash="b" * 64,
        catalog_hash=catalog.catalog_hash,
        deployment_hash="f" * 64,
        tool_package_hash=catalog.package_hash,
        policy_hash=catalog.policy_hash,
        settings=settings,
    )
    runner = DurableLoopRunner(
        state=InMemoryDurableLoopStateStore(),
        content=InMemoryDurableContentStore(),
        model=optimizer,
        tools=registry.build_dispatcher(),
    )
    initial_messages = (
        {"role": "user", "contents": [{"type": "text", "text": "Simple hello"}]},
        {
            "role": "assistant",
            "contents": [
                {"type": "reasoning", "summary": "unencrypted foreign reasoning"},
                {"type": "text", "text": "prior step"},
            ],
        },
    )
    result = await runner.start(identity, plan, initial_messages)
    assert result.status.status is DurableLoopRunStatus.COMPLETED
    assert manager.requested_models == ["gpt-4.1-mini"]
    assert optimizer.route_history[0].tier is ModelTier.FAST
    assert optimizer.route_history[0].target_model == "gpt-4.1-mini"


@pytest.mark.asyncio
async def test_prefix_cache_short_circuit_skips_decision_when_warm_frontier_is_cheaper() -> None:
    now = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    ledger = PrefixCacheLedger()
    decision_client = HeuristicDecisionOneClient()
    settings = DurableLoopSettings(
        decision_routing_enabled=True,
        fast_model="gpt-4.1-mini",
    )
    registry = DurableToolRegistry()
    catalog = registry.catalog(policy_hash="1" * 64, package_hash="2" * 64)
    large_text = "x" * 40000
    messages = (
        {"role": "user", "contents": [{"type": "text", "text": large_text}]},
    )
    ledger.record_step(
        run_id="run-cache",
        model="gpt-6-astra",
        tier=ModelTier.FRONTIER,
        messages=messages,
        input_tokens=10000,
        cached_tokens=0,
        output_tokens=100,
        decision_input_tokens=50,
        emitted_dag=True,
        now=now,
    )
    provider = _RecordingOneStepProvider(
        (
            ScriptedModelStep(
                final_text="Warm frontier response",
                input_tokens=10050,
                output_tokens=100,
            ),
        )
    )
    optimizer = DurableSegmentOptimizer(
        provider,
        settings=settings,
        decision_client=decision_client,
        ledger=ledger,
        clock=lambda: now + timedelta(seconds=15),
    )
    plan = DurableLoopPlan(
        instructions="Finish response.",
        catalog=catalog,
        model_settings={},
        maf_core_version="1.17.0",
        provider="azure_openai_apim",
        model="gpt-6-astra",
        api_version="responses-v1",
        settings=settings,
    )
    identity = create_run_identity(
        run_id="run-cache",
        session_id="sess-cache",
        request_id="req-cache",
        request_body={"prompt": large_text},
        owner_hash="a" * 64,
        agent_slug="main",
        agent_hash="b" * 64,
        catalog_hash=catalog.catalog_hash,
        deployment_hash="c" * 64,
        tool_package_hash=catalog.package_hash,
        policy_hash=catalog.policy_hash,
        settings=settings,
        now=now,
    )
    runner = DurableLoopRunner(
        state=InMemoryDurableLoopStateStore(),
        content=InMemoryDurableContentStore(),
        model=optimizer,
        tools=registry.build_dispatcher(),
        clock=lambda: now + timedelta(seconds=15),
    )
    result = await runner.start(identity, plan, messages)
    assert result.status.status is DurableLoopRunStatus.COMPLETED
    assert len(decision_client.requests) == 0
    assert optimizer.route_history[0].short_circuited is True
    assert optimizer.route_history[0].reason == "warm_frontier_cheaper_than_cold_fast"
    assert optimizer.route_history[0].target_model == "gpt-6-astra"


@pytest.mark.asyncio
async def test_realistic_incident_triage_frontier_baseline_vs_segment_optimizer() -> None:
    prompt = (
        "Triage all services for incident INC-402 in parallel, correlate "
        "their metrics and logs, and then produce the incident summary."
    )
    expected_final_report = (
        "INC-402 Root Cause: checkout-api pool exhaustion triggered by "
        "payment-worker latency (primary p99=1840ms, secondary p99=920ms). "
        "Action: Increase checkout-api pool cap and scale payment-worker."
    )

    # --- Arm A: Frontier-only step-by-step baseline (flag OFF) ---
    baseline_calls: list[tuple[str, dict[str, object]]] = []
    baseline_registry = _build_triage_registry(baseline_calls)
    baseline_settings = DurableLoopSettings(
        decision_routing_enabled=False,
        input_cost_microunits_per_million_tokens=2_500_000,
        output_cost_microunits_per_million_tokens=10_000_000,
    )
    baseline_catalog = baseline_registry.catalog(
        policy_hash="1" * 64,
        package_hash="2" * 64,
    )
    baseline_plan = DurableLoopPlan(
        instructions="You are the incident triage agent.",
        catalog=baseline_catalog,
        model_settings={},
        maf_core_version="1.17.0",
        provider="azure_openai_apim",
        model="gpt-6-astra",
        api_version="responses-v1",
        settings=baseline_settings,
    )
    baseline_provider = _RecordingOneStepProvider(
        (
            ScriptedModelStep(
                calls=(("c1", "lookup_services", {"incident_id": "INC-402"}),),
                input_tokens=1200,
                output_tokens=180,
                cost_microunits=4800,
            ),
            ScriptedModelStep(
                calls=(("c2", "fetch_metrics", {"service": "checkout-api"}),),
                input_tokens=1800,
                output_tokens=160,
                cost_microunits=6100,
            ),
            ScriptedModelStep(
                calls=(("c3", "fetch_logs", {"service": "checkout-api"}),),
                input_tokens=2900,
                output_tokens=160,
                cost_microunits=8850,
            ),
            ScriptedModelStep(
                calls=(("c4", "fetch_metrics", {"service": "payment-worker"}),),
                input_tokens=4200,
                output_tokens=160,
                cost_microunits=12100,
            ),
            ScriptedModelStep(
                calls=(
                    (
                        "c5",
                        "correlate_signals",
                        {
                            "primary_p99": 1840,
                            "secondary_p99": 920,
                            "error_signature": "ConnectionPoolExhausted: upstream payment-worker timeout",
                        },
                    ),
                ),
                input_tokens=5600,
                output_tokens=220,
                cost_microunits=16200,
            ),
            ScriptedModelStep(
                final_text=expected_final_report,
                input_tokens=6400,
                output_tokens=240,
                cost_microunits=18400,
            ),
        )
    )
    baseline_content = InMemoryDurableContentStore()
    baseline_runner = DurableLoopRunner(
        state=InMemoryDurableLoopStateStore(),
        content=baseline_content,
        model=baseline_provider,
        tools=baseline_registry.build_dispatcher(),
    )
    baseline_identity = create_run_identity(
        session_id="sess-baseline",
        request_id="req-baseline",
        request_body={"prompt": prompt},
        owner_hash="a" * 64,
        agent_slug="main",
        agent_hash="b" * 64,
        catalog_hash=baseline_catalog.catalog_hash,
        deployment_hash="c" * 64,
        tool_package_hash=baseline_catalog.package_hash,
        policy_hash=baseline_catalog.policy_hash,
        settings=baseline_settings,
    )
    baseline_result = await baseline_runner.start(
        baseline_identity,
        baseline_plan,
        ({"role": "user", "contents": [{"type": "text", "text": prompt}]},),
    )

    # --- Arm B: Segment Optimizer with Decision-1 + Inline DAG (flag ON) ---
    hybrid_calls: list[tuple[str, dict[str, object]]] = []
    hybrid_registry = _build_triage_registry(hybrid_calls)
    hybrid_settings = DurableLoopSettings(
        decision_routing_enabled=True,
        fast_model="gpt-4.1-mini",
    )
    hybrid_catalog = hybrid_registry.catalog(
        policy_hash="1" * 64,
        package_hash="2" * 64,
    )
    hybrid_plan = DurableLoopPlan(
        instructions="You are the incident triage agent.",
        catalog=hybrid_catalog,
        model_settings={},
        maf_core_version="1.17.0",
        provider="azure_openai_apim",
        model="gpt-6-astra",
        api_version="responses-v1",
        settings=hybrid_settings,
    )
    hybrid_provider = _RecordingOneStepProvider(
        (
            ScriptedModelStep(
                calls=(
                    (
                        "dag-1",
                        COMPILE_SEGMENT_DAG_TOOL_NAME,
                        {
                            "tasks": [
                                {
                                    "id": "lookup",
                                    "tool": "lookup_services",
                                    "args": {"incident_id": "INC-402"},
                                },
                                {
                                    "id": "metrics_primary",
                                    "tool": "fetch_metrics",
                                    "depends_on": ["lookup"],
                                    "args": {"service": "${lookup.result.primary}"},
                                },
                                {
                                    "id": "logs_primary",
                                    "tool": "fetch_logs",
                                    "depends_on": ["lookup"],
                                    "args": {"service": "${lookup.result.primary}"},
                                },
                                {
                                    "id": "metrics_secondary",
                                    "tool": "fetch_metrics",
                                    "depends_on": ["lookup"],
                                    "args": {"service": "${lookup.result.secondary}"},
                                },
                                {
                                    "id": "correlate",
                                    "tool": "correlate_signals",
                                    "depends_on": [
                                        "metrics_primary",
                                        "logs_primary",
                                        "metrics_secondary",
                                    ],
                                    "args": {
                                        "primary_p99": "${metrics_primary.result.p99_latency_ms}",
                                        "secondary_p99": "${metrics_secondary.result.p99_latency_ms}",
                                        "error_signature": "${logs_primary.result.top_error}",
                                    },
                                },
                            ]
                        },
                    ),
                ),
                input_tokens=1350,
                output_tokens=320,
            ),
            ScriptedModelStep(
                final_text=expected_final_report,
                input_tokens=1900,
                output_tokens=240,
            ),
        )
    )
    hybrid_content = InMemoryDurableContentStore()
    hybrid_runner = DurableLoopRunner(
        state=InMemoryDurableLoopStateStore(),
        content=hybrid_content,
        model=hybrid_provider,
        tools=hybrid_registry.build_dispatcher(),
    )
    hybrid_identity = create_run_identity(
        session_id="sess-hybrid",
        request_id="req-hybrid",
        request_body={"prompt": prompt},
        owner_hash="a" * 64,
        agent_slug="main",
        agent_hash="b" * 64,
        catalog_hash=hybrid_catalog.catalog_hash,
        deployment_hash="c" * 64,
        tool_package_hash=hybrid_catalog.package_hash,
        policy_hash=hybrid_catalog.policy_hash,
        settings=hybrid_settings,
    )
    hybrid_result = await hybrid_runner.start(
        hybrid_identity,
        hybrid_plan,
        ({"role": "user", "contents": [{"type": "text", "text": prompt}]},),
    )

    assert baseline_result.status.status is DurableLoopRunStatus.COMPLETED
    assert hybrid_result.status.status is DurableLoopRunStatus.COMPLETED
    assert baseline_result.final_result is not None
    assert hybrid_result.final_result is not None
    assert baseline_result.final_result.response_ref is not None
    assert hybrid_result.final_result.response_ref is not None

    baseline_text = await baseline_content.get_text(
        baseline_result.final_result.response_ref
    )
    hybrid_text = await hybrid_content.get_text(
        hybrid_result.final_result.response_ref
    )
    assert baseline_text == expected_final_report
    assert hybrid_text == expected_final_report

    # Both executed the exact same 5 underlying domain tools with resolved args
    assert len(baseline_calls) == 5
    assert len(hybrid_calls) == 5
    assert dict(hybrid_calls)["correlate_signals"] == {
        "primary_p99": 1840,
        "secondary_p99": 920,
        "error_signature": "ConnectionPoolExhausted: upstream payment-worker timeout",
    }

    # Segment Optimizer routed Step 0 -> Frontier (DAG) and Step 1 -> Fast (Step)
    optimizer = hybrid_runner.segment_optimizer
    assert optimizer is not None
    assert len(optimizer.route_history) == 2
    assert optimizer.route_history[0].mode is SegmentMode.DAG
    assert optimizer.route_history[0].tier is ModelTier.FRONTIER
    assert optimizer.route_history[0].target_model == "gpt-6-astra"
    assert optimizer.route_history[1].mode is SegmentMode.STEP
    assert optimizer.route_history[1].tier is ModelTier.FAST
    assert optimizer.route_history[1].target_model == "gpt-4.1-mini"
    assert hybrid_provider.models_seen == ["gpt-6-astra", "gpt-4.1-mini"]

    # Dramatic reduction in model steps, tokens, and total cost microunits
    assert baseline_result.status.model_steps == 6
    assert hybrid_result.status.model_steps == 2
    assert hybrid_result.status.input_tokens < baseline_result.status.input_tokens
    assert hybrid_result.status.cost_microunits < baseline_result.status.cost_microunits
    _assert_dag_route_guarantees(hybrid_provider, hybrid_runner)


def _assert_dag_route_guarantees(
    hybrid_provider: _RecordingOneStepProvider,
    hybrid_runner: DurableLoopRunner,
) -> None:
    step0_request = hybrid_provider.requests[0]
    assert [t.name for t in step0_request.catalog.tools] == [
        COMPILE_SEGMENT_DAG_TOOL_NAME
    ]
    for tool_name in (
        "lookup_services",
        "fetch_metrics",
        "fetch_logs",
        "correlate_signals",
    ):
        assert tool_name in step0_request.instructions
        assert f"Incident triage tool {tool_name}" in step0_request.instructions
    assert (
        step0_request.model_settings.get("_force_tool_name")
        == COMPILE_SEGMENT_DAG_TOOL_NAME
    )
    step0_options = _durable_model_options(step0_request)
    assert step0_options["tool_choice"] == {
        "type": "function",
        "name": COMPILE_SEGMENT_DAG_TOOL_NAME,
    }

    step1_request = hybrid_provider.requests[1]
    assert COMPILE_SEGMENT_DAG_TOOL_NAME not in step1_request.catalog.by_name()
    assert "_force_tool_name" not in step1_request.model_settings
    assert "tool_choice" not in _durable_model_options(step1_request)

    assert len(hybrid_runner.activity_journal.executions) == 7
    assert all(
        count == 1 for count in hybrid_runner.activity_journal.executions.values()
    )


class _LiveActivityTask:
    def __init__(self, name: str, payload: dict[str, object]) -> None:
        self.kind = "activity"
        self.name = name
        self.payload = payload


class _LiveTaskAll:
    def __init__(self, tasks: list[_LiveActivityTask]) -> None:
        self.kind = "task_all"
        self.tasks = tasks


class _LiveEntityTask:
    def __init__(self, result: dict[str, object]) -> None:
        self.kind = "entity"
        self.result = result


class _LiveOrchestrationContext:
    def __init__(self, input_payload: dict[str, object], now: datetime) -> None:
        self._payload = input_payload
        self.current_utc_datetime = now
        self.activity_calls: list[str] = []
        self.task_all_batch_sizes: list[int] = []

    def get_input(self) -> dict[str, object]:
        return self._payload

    def call_entity(
        self,
        _entity: object,
        operation: str,
        _payload: object,
    ) -> _LiveEntityTask:
        if operation == "admit":
            return _LiveEntityTask({"disposition": "admitted"})
        if operation == "mark_running":
            return _LiveEntityTask({"disposition": "running"})
        if operation == "is_cancelled":
            return _LiveEntityTask({"cancelled": False})
        if operation == "complete":
            return _LiveEntityTask(
                {"committed_generation": 1, "disposition": "completed"}
            )
        return _LiveEntityTask({"disposition": "found"})

    def call_activity(
        self,
        name: str,
        payload: dict[str, object],
    ) -> _LiveActivityTask:
        self.activity_calls.append(name)
        return _LiveActivityTask(name, payload)

    def task_all(
        self,
        tasks: list[_LiveActivityTask],
    ) -> _LiveTaskAll:
        self.task_all_batch_sizes.append(len(tasks))
        return _LiveTaskAll(tasks)

    def set_custom_status(self, _status: object) -> None:
        pass


async def _drive_live_orchestrator(
    orchestrator_fn: Any,
    ctx: _LiveOrchestrationContext,
    activities: Mapping[str, Any],
) -> dict[str, object]:
    gen = orchestrator_fn(ctx)
    yield_val = next(gen)
    while True:
        try:
            if isinstance(yield_val, _LiveEntityTask):
                yield_val = gen.send(yield_val.result)
            elif isinstance(yield_val, _LiveActivityTask):
                act_res = await activities[yield_val.name](yield_val.payload)
                yield_val = gen.send(act_res)
            elif isinstance(yield_val, _LiveTaskAll):
                batch_res = [
                    await activities[t.name](t.payload)
                    for t in yield_val.tasks
                ]
                yield_val = gen.send(batch_res)
            else:
                raise AssertionError(f"Unexpected yielded task: {yield_val!r}")
        except StopIteration as stopped:
            return stopped.value


def test_decision_routing_allows_workflows_enabled_in_frontmatter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(DURABLE_LOOP_ENABLED_ENV, "true")
    monkeypatch.setenv(DURABLE_LOOP_DECISION_ROUTING_ENABLED_ENV, "true")
    (tmp_path / "main.agent.md").write_text(
        (
            "---\n"
            "name: Main\n"
            "description: Durable test with dynamic workflows\n"
            "builtin_endpoints: true\n"
            "workflows:\n"
            "  enabled: true\n"
            "---\n"
            "Test."
        ),
        encoding="utf-8",
    )

    composition = compose_aca_application(tmp_path)

    assert len(composition.resolved_agents) == 1
    assert composition.resolved_agents[0].workflows is not None
    assert composition.resolved_agents[0].workflows.enabled is True


@pytest.mark.asyncio
async def test_orchestrator_schedules_dag_waves_via_task_all_and_wave_activity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(DURABLE_LOOP_ENABLED_ENV, "true")
    monkeypatch.setenv(DURABLE_LOOP_DECISION_ROUTING_ENABLED_ENV, "true")
    (tmp_path / "main.agent.md").write_text(
        (
            "---\n"
            "name: Main\n"
            "description: Durable test\n"
            "builtin_endpoints: true\n"
            "model: gpt-6-astra\n"
            "---\n"
            "Test."
        ),
        encoding="utf-8",
    )
    app = app_module.create_function_app(tmp_path)
    assert isinstance(app, df.DFApp)

    def get_registered(name: str):
        for builder in app._function_builders:
            function = builder._function
            if function._name != name:
                continue
            registered = function._func
            if name == REGISTERED_ORCHESTRATOR_V2_NAME:
                assert registered.__closure__ is not None
                return registered.__closure__[0].cell_contents
            return getattr(registered, "__wrapped__", registered)
        raise AssertionError(f"function {name!r} was not registered")

    prompt = (
        "Triage all services for incident INC-402 in parallel, correlate "
        "their metrics and logs, and then produce the incident summary."
    )
    expected_final_report = "INC-402 Root Cause confirmed via orchestrator waves."
    call_log: list[tuple[str, dict[str, object]]] = []
    registry = _build_triage_registry(call_log)
    settings = DurableLoopSettings(
        decision_routing_enabled=True,
        fast_model="gpt-4.1-mini",
    )
    catalog = registry.catalog(policy_hash="1" * 64, package_hash="2" * 64)
    content = InMemoryDurableContentStore()
    provider = _RecordingOneStepProvider(
        (
            ScriptedModelStep(
                calls=(
                    (
                        "dag-1",
                        COMPILE_SEGMENT_DAG_TOOL_NAME,
                        {
                            "tasks": [
                                {
                                    "id": "lookup",
                                    "tool": "lookup_services",
                                    "args": {"incident_id": "INC-402"},
                                },
                                {
                                    "id": "metrics_primary",
                                    "tool": "fetch_metrics",
                                    "depends_on": ["lookup"],
                                    "args": {"service": "${lookup.result.primary}"},
                                },
                                {
                                    "id": "logs_primary",
                                    "tool": "fetch_logs",
                                    "depends_on": ["lookup"],
                                    "args": {"service": "${lookup.result.primary}"},
                                },
                                {
                                    "id": "metrics_secondary",
                                    "tool": "fetch_metrics",
                                    "depends_on": ["lookup"],
                                    "args": {"service": "${lookup.result.secondary}"},
                                },
                                {
                                    "id": "correlate",
                                    "tool": "correlate_signals",
                                    "depends_on": [
                                        "metrics_primary",
                                        "logs_primary",
                                        "metrics_secondary",
                                    ],
                                    "args": {
                                        "primary_p99": "${metrics_primary.result.p99_latency_ms}",
                                        "secondary_p99": "${metrics_secondary.result.p99_latency_ms}",
                                        "error_signature": "${logs_primary.result.top_error}",
                                    },
                                },
                            ]
                        },
                    ),
                ),
                input_tokens=1350,
                output_tokens=320,
            ),
            ScriptedModelStep(
                final_text=expected_final_report,
                input_tokens=1900,
                output_tokens=240,
            ),
        )
    )
    runtime = DurableLoopActivityRuntime(
        model=DurableSegmentOptimizer(provider, settings=settings),
        tools=registry.build_dispatcher(),
        compactor=DeterministicContextCompactor(),
        content=content,
    )
    set_durable_loop_activity_runtime_factory(lambda: runtime)
    try:
        now = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
        identity = create_run_identity(
            session_id="sess-orch-dag",
            request_id="req-orch-dag",
            request_body={"prompt": prompt},
            owner_hash="a" * 64,
            agent_slug="main",
            agent_hash="b" * 64,
            catalog_hash=catalog.catalog_hash,
            deployment_hash="c" * 64,
            tool_package_hash=catalog.package_hash,
            policy_hash=catalog.policy_hash,
            settings=settings,
            orchestration_version=DURABLE_LOOP_ORCHESTRATOR_V2_NAME,
            now=now,
        )
        bundle = MAFMessageBundleV1.create(
            messages=(
                {"role": "user", "contents": [{"type": "text", "text": prompt}]},
            ),
            maf_core_version="1.17.0",
            provider="azure_openai_apim",
            model="gpt-6-astra",
            api_version="responses-v1",
        )
        working = WorkingContextV1(
            bundle=bundle,
            compaction_generation=0,
            source_audit_hash=bundle.bundle_hash,
            source_start=0,
            source_end=1,
            estimated_tokens=32,
        )
        checkpoint = CheckpointStateV1(
            identity=identity,
            status=DurableLoopRunStatus.PENDING,
            audit_bundle=bundle,
            working_context=working,
            audit_head_hash=bundle.bundle_hash,
            completed_model_steps=0,
            completed_tool_calls=0,
            human_wait_count=0,
            next_model_step=0,
            committed_session_generation=0,
            continue_as_new_generation=0,
            checkpoints_in_generation=0,
        )
        document = DurableRunDocumentV1(
            plan=DurableLoopPlanDocumentV1(
                instructions="You are the incident triage agent.",
                catalog=catalog,
                model_settings={},
                maf_core_version="1.17.0",
                provider="azure_openai_apim",
                model="gpt-6-astra",
                api_version="responses-v1",
                settings=asdict(settings),
            ),
            checkpoint=checkpoint,
        )
        doc_ref = await put_protocol_model(
            content,
            kind="run-document",
            model=document,
        )
        orch_input = DurableOrchestrationInputV1(
            identity=identity,
            run_document_ref=doc_ref,
            session_entity_key=canonical_hash({"session_id": identity.session_id}),
            working_context_bytes=len(canonical_json_bytes(bundle.messages)),
        )

        activities = {
            name: get_registered(name)
            for name in (
                DURABLE_LOOP_MODEL_ACTIVITY_NAME,
                DURABLE_LOOP_DAG_WAVE_ACTIVITY_NAME,
                DURABLE_LOOP_MCP_TOOL_ACTIVITY_NAME,
                DURABLE_LOOP_SANDBOX_TOOL_ACTIVITY_NAME,
                DURABLE_LOOP_APPEND_ACTIVITY_NAME,
            )
        }
        orchestrator_fn = get_registered(REGISTERED_ORCHESTRATOR_V2_NAME)
        ctx = _LiveOrchestrationContext(orch_input.model_dump(mode="json"), now)
        final_output = await _drive_live_orchestrator(
            orchestrator_fn,
            ctx,
            activities,
        )

        assert final_output is not None
        assert final_output["status"] == DurableLoopRunStatus.COMPLETED.value
        assert ctx.activity_calls.count(DURABLE_LOOP_DAG_WAVE_ACTIVITY_NAME) == 4
        assert ctx.activity_calls.count(DURABLE_LOOP_MCP_TOOL_ACTIVITY_NAME) == 5
        assert ctx.task_all_batch_sizes == [1, 3, 1]
        assert len(call_log) == 5
        assert dict(call_log)["correlate_signals"] == {
            "primary_p99": 1840,
            "secondary_p99": 920,
            "error_signature": "ConnectionPoolExhausted: upstream payment-worker timeout",
        }
    finally:
        reset_durable_loop_activity_runtime_factory()

