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

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(shutil.which("func") is None, reason="Azure Functions Core Tools not found"),
]

# The container / queue the storage-triggers app binds to (see its *.agent.md).
BLOB_CONTAINER = "uploads"
QUEUE_NAME = "work-items"

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
