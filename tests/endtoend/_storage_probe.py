"""Azurite storage helpers to fire blob- and queue-triggered agents in E2E tests.

Blob and queue triggers have no HTTP route and are not invoked through the admin
API; they fire when new data lands in their bound storage. The E2E apps bind to
``AzureWebJobsStorage`` which locally resolves to Azurite via
``UseDevelopmentStorage=true``. These helpers write a blob / enqueue a message to
that same Azurite instance so the running host picks it up and runs the agent.

``azure-storage-blob`` is a core runtime dependency; ``azure-storage-queue`` is a
dev/test-only dependency (see ``pyproject.toml`` ``[project.optional-dependencies]``).
"""

from __future__ import annotations

import contextlib

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient
from azure.storage.queue import QueueClient, TextBase64EncodePolicy

# Explicit Azurite dev-storage connection string. Equivalent to
# ``UseDevelopmentStorage=true`` (which the E2E apps bind to), but spelled out so
# both the blob and queue SDKs resolve it — the queue SDK's
# ``from_connection_string`` does not expand the ``UseDevelopmentStorage`` alias.
# The account name/key are Azurite's fixed, well-known dev credentials; the ports
# match the Azurite instance the E2E harness expects (blob 10000 / queue 10001).
DEV_CONNECTION_STRING = (
    "DefaultEndpointsProtocol=http;"
    "AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/"
    "K1SZFPTOtr/KBHBeksoGMGw==;"
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
    "QueueEndpoint=http://127.0.0.1:10001/devstoreaccount1;"
    "TableEndpoint=http://127.0.0.1:10002/devstoreaccount1;"
)


def upload_text_blob(
    container: str,
    blob_name: str,
    content: str,
    *,
    connection_string: str = DEV_CONNECTION_STRING,
) -> None:
    """Upload ``content`` as ``container/blob_name`` (creating the container).

    Overwrites any existing blob so tests are repeatable within a host session.
    """
    service = BlobServiceClient.from_connection_string(connection_string)
    try:
        container_client = service.get_container_client(container)
        with contextlib.suppress(ResourceExistsError):
            container_client.create_container()
        container_client.upload_blob(name=blob_name, data=content, overwrite=True)
    finally:
        service.close()


def clear_container(
    container: str,
    *,
    connection_string: str = DEV_CONNECTION_STRING,
) -> None:
    """Delete every blob in ``container`` (creating it if absent).

    A blob trigger only writes a processing receipt on **success**. A blob whose
    agent failed (e.g. for lack of an LLM provider on a prior run) leaves no
    receipt, so the next host re-detects it during its startup container scan and
    fails again, tripping the harness's startup failure detection. Emptying the
    container before starting the host makes each run deterministic.
    """
    service = BlobServiceClient.from_connection_string(connection_string)
    try:
        container_client = service.get_container_client(container)
        with contextlib.suppress(ResourceExistsError):
            container_client.create_container()
        for blob in container_client.list_blobs():
            with contextlib.suppress(ResourceNotFoundError):
                container_client.delete_blob(blob.name)
    finally:
        service.close()


def send_queue_message(
    queue_name: str,
    content: str,
    *,
    connection_string: str = DEV_CONNECTION_STRING,
) -> None:
    """Enqueue ``content`` on ``queue_name`` (creating the queue).

    Messages are base64-encoded to match the Functions queue trigger's default
    encoding so the host dequeues and delivers them cleanly.
    """
    client = QueueClient.from_connection_string(
        connection_string,
        queue_name,
        message_encode_policy=TextBase64EncodePolicy(),
    )
    try:
        with contextlib.suppress(ResourceExistsError):
            client.create_queue()
        client.send_message(content)
    finally:
        client.close()


def clear_queue_messages(
    queue_name: str,
    *,
    connection_string: str = DEV_CONNECTION_STRING,
) -> None:
    """Remove all messages from ``queue_name`` (creating it if absent).

    Storage-trigger tests run against a shared Azurite instance. A leftover,
    un-processable message (e.g. one that failed for lack of an LLM provider on a
    prior run) would be dequeued the instant a new host starts and fail again,
    tripping the harness's startup failure detection. Clearing the queue before
    starting the host makes each run deterministic. (Blobs need the same
    treatment via :func:`clear_container`, since failed blobs leave no receipt.)
    """
    client = QueueClient.from_connection_string(connection_string, queue_name)
    try:
        with contextlib.suppress(ResourceExistsError):
            client.create_queue()
        client.clear_messages()
    finally:
        client.close()
