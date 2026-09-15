from __future__ import annotations

import os
from pathlib import Path

import azure.functions as func
from azure.storage.blob.aio import BlobServiceClient
from azurefunctions.extensions.http.fastapi import JSONResponse, Request
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from tests.doubles.durable_chat_test_client import (
    DurableChatTestClientManager,
    release_test_response,
)
from tests.doubles.durable_chat_test_sandbox import (
    TEST_SANDBOX_GROUP,
    DurableChatTestSandboxWorld,
)

from azure_functions_agents import create_function_app
from azure_functions_agents.experimental.durable_chat_journal import (
    DurableChatJournal,
    set_durable_chat_journal_factory,
)
from azure_functions_agents.experimental.durable_chat_protocol import durable_chat_run_correlation
from azure_functions_agents.experimental.durable_loop_activities import (
    BlobDurableContentStore,
    DeterministicContextCompactor,
    MafOneStepModelProvider,
)
from azure_functions_agents.experimental.durable_loop_config import DurableLoopSettings
from azure_functions_agents.experimental.durable_loop_execution import DurableExecutionPlaneRouter
from azure_functions_agents.experimental.durable_loop_mcp import DurableRemoteMcpLane
from azure_functions_agents.experimental.durable_loop_receipts import BlobDurableKeyedDocumentStore
from azure_functions_agents.experimental.durable_loop_registration import (
    DurableLoopActivityRuntime,
    set_durable_loop_activity_runtime_factory,
)
from azure_functions_agents.experimental.durable_loop_sandbox import DurableAcaSandboxLane
from azure_functions_agents.experimental.hybrid_apim import HybridApimClientManager
from azure_functions_agents.experimental.hybrid_config import HybridSandboxSettings

exporter = InMemorySpanExporter()
provider = TracerProvider(sampler=ALWAYS_ON)
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)

service = BlobServiceClient.from_connection_string(
    os.environ["DURABLE_CHAT_E2E_STORAGE_CONNECTION"]
)
container = os.environ["DURABLE_CHAT_E2E_CONTENT_CONTAINER"]
content = BlobDurableContentStore(service, container_name=container)
documents = BlobDurableKeyedDocumentStore(service, container_name=container)
journal = DurableChatJournal(content=content, documents=documents)
sandboxes = DurableChatTestSandboxWorld()
tools = DurableExecutionPlaneRouter(
    remote=DurableRemoteMcpLane(
        manager=HybridApimClientManager(
            base_url="https://unused.invalid",
            audience=None,
            subscription_key=None,
        ),
        base_url="https://unused.invalid",
        configured=(),
        content=content,
        receipts=documents,
    ),
    local=DurableAcaSandboxLane(
        settings=HybridSandboxSettings(
            group_resource_id=TEST_SANDBOX_GROUP,
            region="westus2",
            allowed_hosts=(),
            sandbox_disk="python-3.13",
            create_timeout_seconds=10,
            ready_timeout_seconds=10,
            drain_timeout_seconds=1,
            orphan_age_seconds=1200,
        ),
        loop_settings=DurableLoopSettings.from_environment(),
        content=content,
        receipts=documents,
        provider_factory=sandboxes.open_provider,
        package_factory=sandboxes.package,
    ),
)
runtime = DurableLoopActivityRuntime(
    model=MafOneStepModelProvider(DurableChatTestClientManager()),
    tools=tools,
    compactor=DeterministicContextCompactor(),
    content=content,
)
set_durable_chat_journal_factory(lambda: journal)
set_durable_loop_activity_runtime_factory(lambda: runtime)
app = create_function_app(Path(__file__).resolve().parent)


@app.route(route="_test/release/{nonce}", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
async def release_response(req: Request) -> JSONResponse:
    released = release_test_response(req.path_params["nonce"])
    return JSONResponse({"released": released}, status_code=200 if released else 404)


@app.route(route="_test/sandboxes", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
async def sandbox_summaries(req: Request) -> JSONResponse:
    del req
    return JSONResponse({"sandboxes": sandboxes.summaries()})


@app.route(route="_test/spans/{run_id}", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
async def run_spans(req: Request) -> JSONResponse:
    spans = [
        {
            "name": span.name,
            "run_correlation": span.attributes["af.durable_loop.run_correlation"],
            "trace_id": f"{span.context.trace_id:032x}",
            "attribute_names": list(span.attributes),
        }
        for span in exporter.get_finished_spans()
        if span.context is not None
        and span.attributes is not None
        and span.attributes.get("af.durable_loop.run_correlation")
        == durable_chat_run_correlation(req.path_params["run_id"])
    ]
    return JSONResponse({"spans": spans})
