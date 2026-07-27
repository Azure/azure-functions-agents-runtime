"""End-to-end functional tests that fire blob- and queue-triggered agents.

Blob and queue triggers have no HTTP route: they fire when data lands in their
bound storage. These tests keep the ``storage-triggers`` app running, write a
blob / enqueue a message to the same Azurite instance the app binds to
(``AzureWebJobsStorage=UseDevelopmentStorage=true``), then assert the host logged
``Executed 'Functions.<name>'`` — the provider-independent signal that the
trigger reached its registered handler and the function ran (whether the agent
run itself succeeds or fails).

Like the other E2E tests these require ``func`` + Azurite and are marked ``e2e``
(excluded from the default unit run; the E2E pipeline runs ``-m e2e``). Azurite
must be started with ``--skipApiVersionCheck`` for storage-trigger apps.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.endtoend._func_host import HostHandle, running_host
from tests.endtoend._http_probe import (
    HttpClient,
    discover_functions,
    find_functions,
)
from tests.endtoend._storage_probe import (
    clear_container,
    clear_queue_messages,
    send_queue_message,
    upload_text_blob,
)

APPS_DIR = Path(__file__).resolve().parent / "apps"


def _provider_configured() -> bool:
    """Whether an LLM provider appears configured."""
    return bool(
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("AZURE_OPENAI_ENDPOINT")
        or os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    )


requires_llm = pytest.mark.skipif(
    not _provider_configured(), reason="no LLM provider configured (set FOUNDRY_PROJECT_ENDPOINT etc.)"
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(shutil.which("func") is None, reason="Azure Functions Core Tools not found"),
]

# The container / queue the storage-triggers app binds to (see its *.agent.md).
BLOB_CONTAINER = "uploads"
QUEUE_NAME = "work-items"

# The queue the queue-trigger-payload app binds to (see queue_processor.agent.md).
QUEUE_PAYLOAD_NAME = "queue-payload-input"

# Served storage hosts are (handle, client): the handle exposes host output so we
# can assert the function executed after data lands in storage.
Served = tuple[HostHandle, HttpClient]


@contextlib.contextmanager
def _serve(app_name: str) -> Iterator[Served]:
    """Start ``app_name`` under ``func start`` and yield its handle + a client."""
    with running_host(APPS_DIR / app_name) as handle:
        client = HttpClient(handle.base_url)
        try:
            client.wait_until_responsive()
            yield handle, client
        finally:
            client.close()


@pytest.fixture(scope="module")
def storage_host() -> Iterator[Served]:
    # Clear any residue from prior runs first: a stale, un-processable queue
    # message or an un-receipted blob would be picked up during host startup and
    # fail, tripping the harness's failure detection. Do this before starting.
    clear_queue_messages(QUEUE_NAME)
    clear_container(BLOB_CONTAINER)
    with _serve("storage-triggers") as served:
        yield served


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def test_admin_api_discovers_storage_triggers(storage_host: Served) -> None:
    """The storage-triggers app registers one blob and one queue trigger."""
    _, client = storage_host
    functions = discover_functions(client)

    blobs = find_functions(functions, trigger_type="blobTrigger")
    queues = find_functions(functions, trigger_type="queueTrigger")

    assert blobs, "expected a blob-triggered function to be indexed"
    assert queues, "expected a queue-triggered function to be indexed"
    for fn in (*blobs, *queues):
        assert fn.route is None, "storage triggers must not expose an HTTP route"
        assert fn.methods == (), "storage triggers must not list HTTP methods"


# --------------------------------------------------------------------------- #
# Blob trigger
# --------------------------------------------------------------------------- #


def test_blob_trigger_fires_on_upload(storage_host: Served) -> None:
    """Uploading a ``.txt`` blob to the bound container runs the blob agent.

    Classic blob triggers poll storage, so allow a generous wait for the host to
    detect the new blob and invoke the function.
    """
    handle, client = storage_host

    blobs = find_functions(discover_functions(client), trigger_type="blobTrigger")
    assert blobs, "expected a blob-triggered function"
    fn = blobs[0]

    blob_name = f"probe-{uuid.uuid4().hex[:8]}.txt"
    upload_text_blob(BLOB_CONTAINER, blob_name, "hello from the blob trigger e2e test")

    executed = handle.wait_for_log(f"Executed 'Functions.{fn.name}'", timeout=240.0)
    assert executed, (
        f"host never logged execution of blob trigger '{fn.name}' after uploading "
        f"{BLOB_CONTAINER}/{blob_name}. Recent output:\n{handle.read_output()[-2000:]}"
    )


# --------------------------------------------------------------------------- #
# Queue trigger
# --------------------------------------------------------------------------- #


def test_queue_trigger_fires_on_message(storage_host: Served) -> None:
    """Enqueuing a message on the bound queue runs the queue agent."""
    handle, client = storage_host

    queues = find_functions(discover_functions(client), trigger_type="queueTrigger")
    assert queues, "expected a queue-triggered function"
    fn = queues[0]

    send_queue_message(QUEUE_NAME, "process order #1234")

    executed = handle.wait_for_log(f"Executed 'Functions.{fn.name}'", timeout=120.0)
    assert executed, (
        f"host never logged execution of queue trigger '{fn.name}' after enqueuing a "
        f"message on '{QUEUE_NAME}'. Recent output:\n{handle.read_output()[-2000:]}"
    )


# --------------------------------------------------------------------------- #
# Queue trigger — payload serialization (queue-trigger-payload app)
#
# These tests verify the trigger-data serialization path introduced in
# PR #105: when a queue message carries a JSON body, the runtime serializes it
# into a structured dict (body, body_encoding, id, dequeue_count, body_json)
# rather than forwarding the raw Python QueueMessage repr to the agent.
# --------------------------------------------------------------------------- #

FUNCTION_NAME = "queue_processor"


@contextlib.contextmanager
def _serve_payload_app() -> Iterator[Served]:
    with running_host(APPS_DIR / "queue-trigger-payload") as handle:
        client = HttpClient(handle.base_url)
        try:
            client.wait_until_responsive()
            yield handle, client
        finally:
            client.close()


@pytest.fixture(scope="module")
def queue_trigger_payload_host() -> Iterator[Served]:
    """Start the queue-trigger-payload app after clearing any residue messages."""
    clear_queue_messages(QUEUE_PAYLOAD_NAME)
    with _serve_payload_app() as served:
        yield served


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def test_queue_trigger_payload_is_indexed(queue_trigger_payload_host: Served) -> None:
    """The queue-trigger-payload app registers exactly one queueTrigger function."""
    _, client = queue_trigger_payload_host
    functions = discover_functions(client)
    queues = find_functions(functions, trigger_type="queueTrigger")
    assert queues, "expected one queueTrigger function to be indexed"
    fn = queues[0]
    assert fn.route is None, "queue trigger must not expose an HTTP route"
    assert fn.methods == (), "queue trigger must not list HTTP methods"


# --------------------------------------------------------------------------- #
# JSON body message — provider-independent
# --------------------------------------------------------------------------- #


def test_queue_trigger_payload_fires_on_json_message(
    queue_trigger_payload_host: Served,
) -> None:
    """Enqueuing a JSON message exercises the body_json serialization path.

    The agent receives a structured payload (body + body_json) rather than a raw
    Python QueueMessage repr. Provider-independent assertions:

    1. The handler log ``"Agent triggered: trigger_type=queue_trigger"`` appears,
       confirming serialize_trigger_data was called inside the handler.
    2. ``Executed 'Functions.queue_processor'`` appears, confirming the function
       ran to completion (serialization did not throw before invocation logged).
    """
    handle, _ = queue_trigger_payload_host

    body = json.dumps({"order": f"e2e-{uuid.uuid4().hex[:8]}", "quantity": 3})
    send_queue_message(QUEUE_PAYLOAD_NAME, body)

    # The handler logs "Agent triggered" immediately before calling
    # serialize_trigger_data — its presence confirms the full handler chain was
    # entered for a queue trigger.
    triggered = handle.wait_for_log("Agent triggered: trigger_type=queue_trigger", timeout=120.0)
    assert triggered, (
        "handler never logged 'Agent triggered: trigger_type=queue_trigger' after "
        f"enqueuing a JSON message. Recent output:\n{handle.read_output()[-2000:]}"
    )

    executed = handle.wait_for_log(f"Executed 'Functions.{FUNCTION_NAME}'", timeout=30.0)
    assert executed, (
        f"host never logged execution of '{FUNCTION_NAME}' after enqueuing a "
        f"JSON message. Recent output:\n{handle.read_output()[-2000:]}"
    )


# --------------------------------------------------------------------------- #
# Full-run assertion (requires LLM)
# --------------------------------------------------------------------------- #


@requires_llm
def test_queue_trigger_payload_full_run_succeeds(
    queue_trigger_payload_host: Served,
) -> None:
    """With an LLM provider, the agent completes and logs a response.

    Confirms the full path: queue message → serialize_trigger_data (body_json
    populated) → agent runner → LLM call → response logged. The assertion
    targets the ``"Agent response: source_file="`` log entry that the handler
    emits on a successful run, not the content of the LLM response itself.
    """
    handle, _ = queue_trigger_payload_host

    body = json.dumps({"order": f"e2e-llm-{uuid.uuid4().hex[:8]}", "quantity": 1})
    send_queue_message(QUEUE_PAYLOAD_NAME, body)

    responded = handle.wait_for_log("Agent response: source_file=", timeout=120.0)
    assert responded, (
        "agent never logged a successful response after enqueuing a JSON message. "
        f"Recent output:\n{handle.read_output()[-2000:]}"
    )


# --------------------------------------------------------------------------- #
# Blob trigger — payload serialization (blob-trigger-payload app)
#
# These tests verify the blob-trigger serialization path introduced in
# PR #105: when a blob is uploaded, the runtime serializes the InputStream
# binding into a structured dict (name, uri, length, blob_properties, metadata)
# rather than forwarding the raw Python InputStream repr to the agent.
# --------------------------------------------------------------------------- #

BLOB_PAYLOAD_CONTAINER = "blob-payload-input"
BLOB_FUNCTION_NAME = "blob_processor"


@contextlib.contextmanager
def _serve_blob_payload_app() -> Iterator[Served]:
    with running_host(APPS_DIR / "blob-trigger-payload") as handle:
        client = HttpClient(handle.base_url)
        try:
            client.wait_until_responsive()
            yield handle, client
        finally:
            client.close()


@pytest.fixture(scope="module")
def blob_trigger_payload_host() -> Iterator[Served]:
    """Start the blob-trigger-payload app after clearing the bound container.

    Classic blob triggers scan the container at startup. Clearing any residue
    blobs before starting prevents stale blobs from tripping the harness's
    startup failure detection.
    """
    clear_container(BLOB_PAYLOAD_CONTAINER)
    with _serve_blob_payload_app() as served:
        yield served


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def test_blob_trigger_payload_is_indexed(blob_trigger_payload_host: Served) -> None:
    """The blob-trigger-payload app registers exactly one blobTrigger function."""
    _, client = blob_trigger_payload_host
    functions = discover_functions(client)
    blobs = find_functions(functions, trigger_type="blobTrigger")
    assert blobs, "expected one blobTrigger function to be indexed"
    fn = blobs[0]
    assert fn.route is None, "blob trigger must not expose an HTTP route"
    assert fn.methods == (), "blob trigger must not list HTTP methods"


# --------------------------------------------------------------------------- #
# Blob upload — provider-independent
# --------------------------------------------------------------------------- #


def test_blob_trigger_payload_fires_on_upload(
    blob_trigger_payload_host: Served,
) -> None:
    """Uploading a blob exercises the InputStream serialization path.

    The agent receives structured blob metadata (name, uri, length,
    blob_properties) rather than a raw Python InputStream repr. Provider-
    independent assertions:

    1. The handler log ``"Agent triggered: trigger_type=blob_trigger"`` appears,
       confirming serialize_trigger_data was called inside the handler.
    2. ``Executed 'Functions.blob_processor'`` appears, confirming the function
       ran to completion (serialization did not throw before invocation logged).
    """
    handle, _ = blob_trigger_payload_host

    blob_name = f"probe-{uuid.uuid4().hex[:8]}.txt"
    upload_text_blob(BLOB_PAYLOAD_CONTAINER, blob_name, "blob trigger serialization e2e probe")

    # Classic blob triggers poll storage, so allow a generous wait.
    triggered = handle.wait_for_log("Agent triggered: trigger_type=blob_trigger", timeout=240.0)
    assert triggered, (
        "handler never logged 'Agent triggered: trigger_type=blob_trigger' after "
        f"uploading '{blob_name}'. Recent output:\n{handle.read_output()[-2000:]}"
    )

    executed = handle.wait_for_log(f"Executed 'Functions.{BLOB_FUNCTION_NAME}'", timeout=30.0)
    assert executed, (
        f"host never logged execution of '{BLOB_FUNCTION_NAME}' after uploading "
        f"'{blob_name}'. Recent output:\n{handle.read_output()[-2000:]}"
    )


# --------------------------------------------------------------------------- #
# Full-run assertion (requires LLM)
# --------------------------------------------------------------------------- #


@requires_llm
def test_blob_trigger_payload_full_run_succeeds(
    blob_trigger_payload_host: Served,
) -> None:
    """With an LLM provider, the blob agent completes and logs a response.

    Confirms the full path: blob upload → serialize_trigger_data (name/uri/
    length populated) → agent runner → LLM call → response logged. The
    assertion targets the ``"Agent response: source_file="`` log entry that the
    handler emits on a successful run, not the content of the LLM response.
    """
    handle, _ = blob_trigger_payload_host

    blob_name = f"probe-llm-{uuid.uuid4().hex[:8]}.txt"
    upload_text_blob(BLOB_PAYLOAD_CONTAINER, blob_name, "blob trigger llm e2e probe")

    responded = handle.wait_for_log("Agent response: source_file=", timeout=240.0)
    assert responded, (
        "agent never logged a successful response after uploading a blob. "
        f"Recent output:\n{handle.read_output()[-2000:]}"
    )
