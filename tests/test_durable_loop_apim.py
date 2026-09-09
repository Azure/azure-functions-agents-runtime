from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from agent_framework.exceptions import ChatClientException

from azure_functions_agents.experimental.durable_loop_activities import (
    DurableLoopModelError,
    InMemoryDurableContentStore,
    OneStepModelRequest,
    ScriptedModelStep,
)
from azure_functions_agents.experimental.durable_loop_apim import (
    ApimMafResponsesProvider,
    ApimResponsesError,
)
from azure_functions_agents.experimental.durable_loop_config import DurableLoopSettings
from azure_functions_agents.experimental.durable_loop_protocol import (
    DURABLE_LOOP_ORCHESTRATOR_V1_NAME,
    DURABLE_LOOP_ORCHESTRATOR_V3_NAME,
    BackgroundStartDisposition,
    DurableFaultProfile,
    DurableLoopBudgetV1,
    DurableRunIdentityV1,
    FrozenToolCatalogV1,
    MAFMessageBundleV1,
    ModelOperationStatus,
    SandboxExecutionProfile,
    WorkingContextV1,
    canonical_hash,
)
from azure_functions_agents.experimental.durable_loop_receipts import (
    DurableOneShotFaults,
    InMemoryDurableKeyedDocumentStore,
)
from azure_functions_agents.experimental.hybrid_apim import HybridApimClientManager


def _request(
    *,
    fault_profile: DurableFaultProfile = DurableFaultProfile.NONE,
    orchestration_version: str = DURABLE_LOOP_ORCHESTRATOR_V1_NAME,
) -> OneStepModelRequest:
    now = datetime.now(UTC)
    identity = DurableRunIdentityV1(
        run_id="run-1",
        session_id="session-1",
        request_id_hash="a" * 64,
        request_hash="b" * 64,
        owner_hash="c" * 64,
        agent_slug="main",
        agent_hash="d" * 64,
        catalog_hash="e" * 64,
        deployment_hash=canonical_hash(
            {
                "api_version": "responses-v1",
                "endpoint": "https://gateway.test/model/openai/v1",
                "model": "deployment",
                "provider": "azure_openai_apim",
            }
        ),
        tool_package_hash="f" * 64,
        policy_hash="1" * 64,
        orchestration_version=orchestration_version,
        created_at=now,
        active_deadline=now + timedelta(hours=1),
        absolute_deadline=now + timedelta(days=1),
        budget=DurableLoopBudgetV1(
            max_model_steps=48,
            max_tool_calls=128,
            max_elapsed_seconds=3600,
            max_human_waits=8,
            human_wait_seconds=3600,
            max_argument_bytes=262144,
            max_result_bytes=1048576,
            context_max_bytes=4194304,
            context_compaction_percent=75,
            max_parallel_reads=4,
            continue_as_new_checkpoints=20,
        ),
    )
    bundle = MAFMessageBundleV1.create(
        messages=(
            {"role": "user", "contents": [{"type": "text", "text": "secret"}]},
        ),
        maf_core_version="1.17.0",
        provider="azure_openai_apim",
        model="deployment",
        api_version="responses-v1",
    )
    return OneStepModelRequest(
        identity=identity,
        step_index=0,
        instructions="Respond.",
        working_context=WorkingContextV1(
            bundle=bundle,
            compaction_generation=0,
            source_audit_hash=bundle.bundle_hash,
            source_start=0,
            source_end=1,
            estimated_tokens=1,
        ),
        catalog=FrozenToolCatalogV1.create(
            tools=(),
            policy_hash=identity.policy_hash,
            package_hash=identity.tool_package_hash,
        ),
        model_settings={"background": True},
        fault_profile=fault_profile,
    )


class _Foreground:
    def __init__(self, request: OneStepModelRequest) -> None:
        self.calls = 0
        self.client_kwargs: list[object] = []
        self.decision = ScriptedModelStep(final_text="done").build(request)

    async def run_one_step(
        self,
        _request: OneStepModelRequest,
        *,
        client_kwargs=None,
    ):
        self.calls += 1
        self.client_kwargs.append(client_kwargs)
        _raise_demo_429(client_kwargs)
        return self.decision

    async def run_agent_response(
        self,
        _request: OneStepModelRequest,
        *,
        background: bool = False,
        client_kwargs=None,
    ):
        assert background is True
        self.calls += 1
        self.client_kwargs.append(client_kwargs)
        _raise_demo_429(client_kwargs)
        return SimpleNamespace(
            continuation_token={"response_id": "resp_1"},
            raw_representation={"id": "resp_1", "status": "queued"},
        )

    def parse_agent_response(self, _request: OneStepModelRequest, _response: object):
        return self.decision


def _raise_demo_429(client_kwargs: object) -> None:
    if (
        isinstance(client_kwargs, dict)
        and isinstance(client_kwargs.get("extra_headers"), dict)
        and client_kwargs["extra_headers"].get("x-af-demo-fault")
        == "model-429-once"
    ):
        raise ApimResponsesError(
            "model_throttled",
            status_code=429,
            retry_after_seconds=0.01,
        )


class _Gateway429Error(Exception):
    def __init__(self) -> None:
        super().__init__("gateway throttled")
        self.status_code = 429
        self.response = SimpleNamespace(headers={"Retry-After": "0.01"})


def _wrapped_gateway_429_error() -> ChatClientException:
    inner = _Gateway429Error()
    outer = ChatClientException("wrapped")
    outer.__cause__ = inner
    return outer


class _Transport:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        headers,
        timeout_seconds: float,
    ):
        assert timeout_seconds > 0
        self.requests.append((method, url, dict(headers)))
        return 200, {}, self.responses.pop(0)


def _manager() -> HybridApimClientManager:
    return HybridApimClientManager(
        base_url="https://gateway.test/model/openai/v1",
        audience=None,
        subscription_key="private",
        environment={"AZURE_FUNCTIONS_AGENTS_MODEL": "deployment"},
    )


@pytest.mark.asyncio
async def test_background_start_receipt_prevents_second_provider_response() -> None:
    request = _request()
    content = InMemoryDurableContentStore()
    receipts = InMemoryDurableKeyedDocumentStore()
    foreground = _Foreground(request)
    provider = ApimMafResponsesProvider(
        _manager(),
        content=content,
        receipts=receipts,
        settings=DurableLoopSettings(background_model_enabled=True),
        control_base_url="https://gateway.test/model-control",
        foreground=foreground,
    )

    first = await provider.start(request)
    replay = await provider.start(request)

    assert first.disposition is BackgroundStartDisposition.ACCEPTED
    assert replay.disposition is BackgroundStartDisposition.ACCEPTED
    assert replay.operation == first.operation
    assert foreground.calls == 1
    headers = foreground.client_kwargs[0]["extra_headers"]
    assert "x-af-demo-fault" not in headers
    assert headers["x-af-operation-id"].startswith("op-")
    assert "resp_1" not in first.model_dump_json()


@pytest.mark.asyncio
async def test_ambiguous_background_start_never_reissues_provider_request() -> None:
    request = _request()
    content = InMemoryDurableContentStore()
    receipts = InMemoryDurableKeyedDocumentStore()

    class LostAcknowledgement(_Foreground):
        async def run_agent_response(
            self,
            _request: OneStepModelRequest,
            *,
            background: bool = False,
            client_kwargs=None,
        ):
            del client_kwargs
            assert background is True
            self.calls += 1
            raise ApimResponsesError(
                "model_transport_timeout",
                ambiguous=True,
            )

    foreground = LostAcknowledgement(request)
    provider = ApimMafResponsesProvider(
        _manager(),
        content=content,
        receipts=receipts,
        settings=DurableLoopSettings(background_model_enabled=True),
        control_base_url="https://gateway.test/model-control",
        foreground=foreground,
    )

    first = await provider.start(request)
    replay = await provider.start(request)

    assert (
        first.disposition
        is BackgroundStartDisposition.LOST_ACKNOWLEDGEMENT
    )
    assert replay.disposition is first.disposition
    assert replay.error is not None
    assert first.error is not None
    assert replay.error.code == first.error.code
    assert first.error is not None
    assert first.error.possibly_committed is True
    assert foreground.calls == 1


@pytest.mark.asyncio
async def test_background_start_retries_only_injected_429() -> None:
    request = _request(
        fault_profile=DurableFaultProfile.MODEL_APIM_429_ONCE
    )
    receipts = InMemoryDurableKeyedDocumentStore()
    foreground = _Foreground(request)
    provider = ApimMafResponsesProvider(
        _manager(),
        content=InMemoryDurableContentStore(),
        receipts=receipts,
        settings=DurableLoopSettings(
            background_model_enabled=True,
            fault_injection_enabled=True,
        ),
        control_base_url="https://gateway.test/model-control",
        faults=DurableOneShotFaults(receipts, enabled=True),
        foreground=foreground,
    )

    result = await provider.start(request)

    assert result.disposition is BackgroundStartDisposition.ACCEPTED
    assert foreground.calls == 2
    first_headers = foreground.client_kwargs[0]["extra_headers"]
    second_headers = foreground.client_kwargs[1]["extra_headers"]
    assert first_headers["x-af-demo-fault"] == "model-429-once"
    assert "x-af-demo-fault" not in second_headers
    assert first_headers["x-af-operation-id"] == second_headers["x-af-operation-id"]


@pytest.mark.asyncio
async def test_background_poll_and_cancel_use_fixed_control_routes_and_header() -> None:
    request = _request()
    content = InMemoryDurableContentStore()
    transport = _Transport(
        [
            {"id": "resp_1", "status": "in_progress", "output": []},
            {
                "id": "resp_1",
                "status": "completed",
                "output": [
                    {
                        "id": "reasoning_1",
                        "type": "reasoning",
                        "encrypted_content": "cipher",
                        "summary": [{"type": "summary_text", "text": "plan"}],
                    },
                    {
                        "id": "message_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    },
                ],
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 3,
                    "output_tokens_details": {"reasoning_tokens": 1},
                },
            },
            {"id": "resp_1", "status": "cancelled", "output": []},
        ]
    )
    provider = ApimMafResponsesProvider(
        _manager(),
        content=content,
        receipts=InMemoryDurableKeyedDocumentStore(),
        settings=DurableLoopSettings(background_model_enabled=True),
        control_base_url="https://gateway.test/model-control",
        transport=transport,
        foreground=_Foreground(request),
    )
    started = await provider.start(request)
    assert started.operation is not None

    pending = await provider.poll(started.operation)
    assert pending.operation is not None
    assert pending.operation.status is ModelOperationStatus.IN_PROGRESS
    terminal = await provider.poll(pending.operation)
    assert terminal.decision is not None
    reasoning = terminal.decision.assistant_message["contents"][0]  # type: ignore[index]
    assert reasoning["protected_data"] == "cipher"  # type: ignore[index]
    cancelled = await provider.cancel(pending.operation)

    assert cancelled.status is ModelOperationStatus.CANCELLED
    assert [(method, url) for method, url, _headers in transport.requests] == [
        ("GET", "https://gateway.test/model-control/responses"),
        ("GET", "https://gateway.test/model-control/responses"),
        ("POST", "https://gateway.test/model-control/responses/cancel"),
    ]
    for _method, _url, headers in transport.requests:
        assert headers["x-af-response-id"] == "resp_1"
        assert headers["api-key"] == "private"


@pytest.mark.asyncio
async def test_terminal_poll_and_cancel_replay_without_second_control_call() -> None:
    request = _request()
    content = InMemoryDurableContentStore()
    transport = _Transport(
        [
            {
                "id": "resp_1",
                "status": "completed",
                "output": [
                    {
                        "id": "message_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
            },
            {"id": "resp_1", "status": "cancelled", "output": []},
        ]
    )
    provider = ApimMafResponsesProvider(
        _manager(),
        content=content,
        receipts=InMemoryDurableKeyedDocumentStore(),
        settings=DurableLoopSettings(background_model_enabled=True),
        control_base_url="https://gateway.test/model-control",
        transport=transport,
        foreground=_Foreground(request),
    )
    started = await provider.start(request)
    assert started.operation is not None

    terminal = await provider.poll(started.operation)
    replayed_terminal = await provider.poll(started.operation)
    cancelled = await provider.cancel(started.operation)
    replayed_cancel = await provider.cancel(started.operation)

    assert replayed_terminal.decision == terminal.decision
    assert replayed_cancel == cancelled
    assert [(method, url) for method, url, _headers in transport.requests] == [
        ("GET", "https://gateway.test/model-control/responses"),
        ("POST", "https://gateway.test/model-control/responses/cancel"),
    ]


@pytest.mark.asyncio
async def test_synchronous_apim_429_fault_retries_once_without_content() -> None:
    request = _request(fault_profile=DurableFaultProfile.MODEL_APIM_429_ONCE)
    receipts = InMemoryDurableKeyedDocumentStore()
    foreground = _Foreground(request)
    provider = ApimMafResponsesProvider(
        _manager(),
        content=InMemoryDurableContentStore(),
        receipts=receipts,
        settings=DurableLoopSettings(fault_injection_enabled=True),
        control_base_url="https://gateway.test/model-control",
        faults=DurableOneShotFaults(receipts, enabled=True),
        foreground=foreground,
    )

    decision = await provider.run_one_step(request)

    assert decision.final_text == "done"
    assert decision.attempts == 2
    assert foreground.calls == 2
    first_headers = foreground.client_kwargs[0]["extra_headers"]
    second_headers = foreground.client_kwargs[1]["extra_headers"]
    assert first_headers["x-af-demo-fault"] == "model-429-once"
    assert "x-af-demo-fault" not in second_headers
    assert first_headers["x-af-operation-id"] == second_headers["x-af-operation-id"]
    assert request.fault_profile is DurableFaultProfile.MODEL_APIM_429_ONCE
    assert SandboxExecutionProfile.PER_CALL.value not in decision.model_dump_json()


@pytest.mark.asyncio
async def test_v3_apim_429_escapes_then_next_activity_attempt_succeeds() -> None:
    request = _request(
        fault_profile=DurableFaultProfile.MODEL_APIM_429_ONCE,
        orchestration_version=DURABLE_LOOP_ORCHESTRATOR_V3_NAME,
    )
    receipts = InMemoryDurableKeyedDocumentStore()

    class RecoveryForeground(_Foreground):
        async def run_one_step(
            self,
            _request: OneStepModelRequest,
            *,
            client_kwargs=None,
        ):
            self.calls += 1
            self.client_kwargs.append(client_kwargs)
            _raise_demo_429(client_kwargs)
            if self.calls == 2:
                raise ApimResponsesError(
                    "model_request_failed",
                    status_code=503,
                    retry_after_seconds=0.01,
                )
            return self.decision

    foreground = RecoveryForeground(request)
    provider = ApimMafResponsesProvider(
        _manager(),
        content=InMemoryDurableContentStore(),
        receipts=receipts,
        settings=DurableLoopSettings(fault_injection_enabled=True),
        control_base_url="https://gateway.test/model-control",
        faults=DurableOneShotFaults(receipts, enabled=True),
        foreground=foreground,
    )

    with pytest.raises(ApimResponsesError, match="model_throttled"):
        await provider.run_one_step(request)
    decision = await provider.run_one_step(request)

    assert decision.final_text == "done"
    assert decision.attempts == 2
    assert foreground.calls == 3
    first_headers = foreground.client_kwargs[0]["extra_headers"]
    second_headers = foreground.client_kwargs[1]["extra_headers"]
    third_headers = foreground.client_kwargs[2]["extra_headers"]
    assert first_headers["x-af-demo-fault"] == "model-429-once"
    assert "x-af-demo-fault" not in second_headers
    assert "x-af-demo-fault" not in third_headers
    assert first_headers["x-af-operation-id"] == second_headers["x-af-operation-id"]
    assert first_headers["x-af-operation-id"] == third_headers["x-af-operation-id"]


@pytest.mark.asyncio
async def test_wrapped_gateway_429_is_normalized_and_retried() -> None:
    request = _request(fault_profile=DurableFaultProfile.MODEL_APIM_429_ONCE)
    receipts = InMemoryDurableKeyedDocumentStore()

    class WrappedForeground(_Foreground):
        async def run_one_step(
            self,
            _request: OneStepModelRequest,
            *,
            client_kwargs=None,
        ):
            self.calls += 1
            self.client_kwargs.append(client_kwargs)
            headers = client_kwargs["extra_headers"]
            if headers.get("x-af-demo-fault") == "model-429-once":
                raise _wrapped_gateway_429_error()
            return self.decision

    foreground = WrappedForeground(request)
    provider = ApimMafResponsesProvider(
        _manager(),
        content=InMemoryDurableContentStore(),
        receipts=receipts,
        settings=DurableLoopSettings(fault_injection_enabled=True),
        control_base_url="https://gateway.test/model-control",
        faults=DurableOneShotFaults(receipts, enabled=True),
        foreground=foreground,
    )

    decision = await provider.run_one_step(request)

    assert decision.attempts == 2
    assert foreground.calls == 2
    assert foreground.client_kwargs[0]["extra_headers"][
        "x-af-demo-fault"
    ] == "model-429-once"
    assert "x-af-demo-fault" not in foreground.client_kwargs[1][
        "extra_headers"
    ]


@pytest.mark.asyncio
async def test_background_control_rejects_unvalidated_response_id() -> None:
    request = _request()
    content = InMemoryDurableContentStore()
    provider = ApimMafResponsesProvider(
        _manager(),
        content=content,
        receipts=InMemoryDurableKeyedDocumentStore(),
        settings=DurableLoopSettings(background_model_enabled=True),
        control_base_url="https://gateway.test/model-control",
        transport=_Transport([]),
        foreground=_Foreground(request),
    )
    started = await provider.start(request)
    assert started.operation is not None
    invalid_ref = await content.put_bytes(
        kind="provider-response-id",
        payload=b"invalid/id",
        media_type="text/plain",
        retention_class="run",
    )

    with pytest.raises(DurableLoopModelError, match="response ID"):
        await provider.poll(
            started.operation.model_copy(
                update={"provider_response_ref": invalid_ref}
            )
        )
