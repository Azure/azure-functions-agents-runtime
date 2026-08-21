from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import azure.functions as func
import pytest

from azure_functions_agents.config.schema import (
    BuiltinEndpointsConfig,
    ResolvedAgent,
    ToolsFilter,
    TriggerSpec,
)
from azure_functions_agents.execution.backend import RunHandle, RunStatus
from azure_functions_agents.execution.foundry_application_content import (
    build_application_content_manifest,
    compute_application_content_digest,
)
from azure_functions_agents.execution.foundry_responses_binding import (
    FoundryResponsesRuntimeBinding,
    compute_foundry_responses_binding_fingerprint,
)
from azure_functions_agents.execution.foundry_responses_runtime import FoundryResponsesRuntime
from azure_functions_agents.registration import _handlers
from azure_functions_agents.registration._handlers import make_agent_handler
from azure_functions_agents.registration.capabilities import AgentCapabilities
from azure_functions_agents.session_state import AppIdentity

_APP = AppIdentity.create(
    subscription_id="11111111-2222-3333-4444-555555555555",
    site_name="agent-app",
)
_PROJECT_RESOURCE_ID = (
    "/subscriptions/11111111-2222-3333-4444-555555555555"
    "/resourceGroups/agents-rg/providers/Microsoft.CognitiveServices/accounts/project/projects/demo"
)


def _runtime(tmp_path: Path) -> FoundryResponsesRuntime:
    (tmp_path / "worker.agent.md").write_text("---\nname: Worker\n---\n", encoding="utf-8")
    manifest = build_application_content_manifest(tmp_path)
    digest = compute_application_content_digest(tmp_path, manifest)
    binding = FoundryResponsesRuntimeBinding.create(
        project_endpoint="https://project.services.ai.azure.com/api/projects/demo",
        project_resource_id=_PROJECT_RESOURCE_ID,
        managed_agent_name="hosted-agent",
        managed_agent_version="v1",
        application_content_manifest=manifest,
        application_content_digest=digest,
        wrapper_digest="sha256:" + ("a" * 64),
        binding_fingerprint="fha1-" + ("a" * 52),
    )
    return FoundryResponsesRuntime.create(
        binding=FoundryResponsesRuntimeBinding.create(
            project_endpoint=binding.project_endpoint,
            project_resource_id=binding.project_resource_id,
            managed_agent_name=binding.managed_agent_name,
            managed_agent_version=binding.managed_agent_version,
            application_content_manifest=binding.application_content_manifest,
            application_content_digest=binding.application_content_digest,
            wrapper_digest=binding.wrapper_digest,
            binding_fingerprint=compute_foundry_responses_binding_fingerprint(
                app_identity=_APP,
                project_endpoint=binding.project_endpoint,
                project_resource_id=binding.project_resource_id,
                managed_agent_name=binding.managed_agent_name,
                managed_agent_version=binding.managed_agent_version,
                application_content_manifest=binding.application_content_manifest,
                application_content_digest=binding.application_content_digest,
                wrapper_digest=binding.wrapper_digest,
            ),
        ),
        app_identity=_APP,
    )


def _resolved(*, timeout: float = 30.0) -> ResolvedAgent:
    return ResolvedAgent(
        name="Worker",
        slug="worker",
        description="Model-only Service Bus worker.",
        trigger=TriggerSpec(
            type="service_bus_queue_trigger",
            args={"connection": "ServiceBus", "queue_name": "jobs"},
        ),
        instructions="Process the message.",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(),
        model=None,
        timeout=timeout,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        tools_disabled=True,
        skills_disabled=True,
        mcp_disabled=True,
        sandbox_config=None,
        web_request_config=None,
        input_schema=None,
        response_schema=None,
        response_example=None,
    )


class _ServiceBusMessage(func.ServiceBusMessage):
    def __init__(self, *, sequence_number: int, delivery_count: int) -> None:
        super().__init__(body=b'{"work":"item"}')
        self._sequence_number = sequence_number
        self._delivery_count = delivery_count

    @property
    def sequence_number(self) -> int:
        return self._sequence_number

    @property
    def delivery_count(self) -> int:
        return self._delivery_count

    @property
    def message_id(self) -> str:
        return "not-authoritative"

    @property
    def metadata(self) -> dict[str, int]:
        return {"DeliveryCount": self._delivery_count}


def test_service_bus_handler_uses_stable_delivery_identity_without_connection_leak(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ServiceBus__fullyQualifiedNamespace", "namespace.servicebus.windows.net")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_FHA_SERVICE_BUS_LOCK_BUDGET_SECONDS", "60")
    requests = []
    provider_creates = 0
    handles: dict[str, RunHandle] = {}

    class Backend:
        async def start_run(self, request):
            nonlocal provider_creates
            requests.append(request)
            assert request.idempotency_key is not None
            if request.idempotency_key in handles:
                return handles[request.idempotency_key]
            provider_creates += 1
            handle = RunHandle(
                run_id="run-1",
                session_id=request.session_id or "missing",
                state="accepted",
                created_at=datetime.now(UTC),
            )
            handles[request.idempotency_key] = handle
            return handle

        async def get_run(self, context):
            return RunStatus(
                run_id=context.run_id,
                session_id=context.session_id,
                state="succeeded",
                last_sequence=0,
                result_available=True,
            )

    monkeypatch.setattr(_handlers, "create_execution_backend", lambda **_kwargs: Backend())
    handler = make_agent_handler(
        _resolved(),
        "service_bus_queue_trigger",
        AgentCapabilities(),
        session_runtime=_runtime(tmp_path),
    )

    asyncio.run(handler(_ServiceBusMessage(sequence_number=42, delivery_count=1)))
    asyncio.run(handler(_ServiceBusMessage(sequence_number=42, delivery_count=2)))

    assert len(requests) == 2
    assert provider_creates == 1
    assert requests[0].session_id == requests[1].session_id
    assert requests[0].idempotency_key == requests[1].idempotency_key
    assert requests[0].prompt == requests[1].prompt
    assert "delivery_count" not in requests[0].prompt
    assert "DeliveryCount" not in requests[0].prompt
    assert requests[0].idempotency_key is not None
    assert requests[0].idempotency_key.startswith("sbd1-")
    assert "namespace.servicebus.windows.net" not in requests[0].idempotency_key


def test_service_bus_handler_rejects_missing_broker_sequence_before_provider_call(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ServiceBus__fullyQualifiedNamespace", "namespace.servicebus.windows.net")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_FHA_SERVICE_BUS_LOCK_BUDGET_SECONDS", "60")
    backend_calls = 0

    def backend_factory(**_kwargs):
        nonlocal backend_calls
        backend_calls += 1
        raise AssertionError("provider backend must not be constructed")

    monkeypatch.setattr(_handlers, "create_execution_backend", backend_factory)
    handler = make_agent_handler(
        _resolved(),
        "service_bus_queue_trigger",
        AgentCapabilities(),
        session_runtime=_runtime(tmp_path),
    )

    with pytest.raises(RuntimeError, match="sequence number"):
        asyncio.run(handler(func.ServiceBusMessage(body=b"missing sequence")))

    assert backend_calls == 0


def test_service_bus_handler_bounds_poll_and_cancels_before_lock_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ServiceBus__fullyQualifiedNamespace", "namespace.servicebus.windows.net")
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_FHA_SERVICE_BUS_LOCK_BUDGET_SECONDS", "3.05")
    cancel_calls = 0

    class Backend:
        async def start_run(self, request):
            return RunHandle(
                run_id="run-1",
                session_id=request.session_id or "missing",
                state="accepted",
                created_at=datetime.now(UTC),
            )

        async def get_run(self, _context):
            await asyncio.sleep(0.1)
            raise AssertionError("poll should be bounded before completion")

        async def cancel_run(self, context):
            nonlocal cancel_calls
            cancel_calls += 1
            return RunStatus(
                run_id=context.run_id,
                session_id=context.session_id,
                state="canceled",
                last_sequence=0,
                result_available=False,
            )

    monkeypatch.setattr(_handlers, "create_execution_backend", lambda **_kwargs: Backend())
    handler = make_agent_handler(
        _resolved(timeout=0.01),
        "service_bus_queue_trigger",
        AgentCapabilities(),
        session_runtime=_runtime(tmp_path),
    )

    with pytest.raises(RuntimeError, match="lock budget"):
        asyncio.run(handler(_ServiceBusMessage(sequence_number=42, delivery_count=1)))

    assert cancel_calls == 1
