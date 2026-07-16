from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

import azure.functions as func
from azure.functions.blob import InputStream

from azure_functions_agents.registration._handlers import serialize_trigger_data


class _TimerRequest(func.TimerRequest):
    @property
    def past_due(self) -> bool:
        return False

    @property
    def schedule_status(self) -> dict[str, str]:
        return {"last": "2025-01-02T00:00:00+00:00"}

    @property
    def schedule(self) -> dict[str, bool]:
        return {"adjust_for_dst": True}


class _NativeContract:
    def to_dict(self) -> dict[str, str]:
        return {"source": "native"}


class _ModelContract:
    def model_dump(self) -> dict[str, str]:
        return {"source": "model"}


def _serialized(binding: object) -> Any:
    serialized = serialize_trigger_data(binding)
    assert " at 0x" not in serialized
    return json.loads(serialized)


def test_blob_input_stream_serializes_metadata_without_reading_content() -> None:
    payload = _serialized(
        InputStream(
            data=b"do not include blob data",
            name="uploads/x.png",
            uri="https://example.test/uploads/x.png",
            length=24,
            blob_properties={"content_type": "image/png"},
            metadata={"source": "upload"},
        )
    )

    assert payload == {
        "name": "uploads/x.png",
        "uri": "https://example.test/uploads/x.png",
        "length": 24,
        "blob_properties": {"content_type": "image/png"},
        "metadata": {"source": "upload"},
    }


def test_queue_message_serializes_body_and_properties() -> None:
    payload = _serialized(
        func.QueueMessage(
            id="queue-id",
            body=b'{"message": "queue payload"}',
            pop_receipt="receipt",
        )
    )

    assert payload["id"] == "queue-id"
    assert payload["body"] == '{"message": "queue payload"}'
    assert payload["body_encoding"] == "utf-8"
    assert payload["body_json"] == {"message": "queue payload"}
    assert payload["pop_receipt"] == "receipt"


def test_queue_message_base64_encodes_non_utf8_body() -> None:
    payload = _serialized(func.QueueMessage(body=b"\xff\x00"))

    assert payload["body"] == base64.b64encode(b"\xff\x00").decode("ascii")
    assert payload["body_encoding"] == "base64"
    assert "body_json" not in payload


def test_service_bus_message_serializes_body() -> None:
    payload = _serialized(func.ServiceBusMessage(body=b"service bus payload"))

    assert payload["body"] == "service bus payload"
    assert payload["body_encoding"] == "utf-8"


def test_event_grid_event_serializes_data() -> None:
    payload = _serialized(
        func.EventGridEvent(
            id="event-1",
            data={"answer": 42},
            topic="/subscriptions/example",
            subject="uploads/x.png",
            event_type="BlobCreated",
            event_time=datetime(2025, 1, 2, tzinfo=UTC),
            data_version="1.0",
        )
    )

    assert payload == {
        "id": "event-1",
        "topic": "/subscriptions/example",
        "subject": "uploads/x.png",
        "event_type": "BlobCreated",
        "event_time": "2025-01-02T00:00:00+00:00",
        "data_version": "1.0",
        "data": {"answer": 42},
    }


def test_event_hub_event_serializes_body_and_metadata() -> None:
    payload = _serialized(
        func.EventHubEvent(
            body=b"event hub payload",
            partition_key="partition-key",
            sequence_number=12,
            offset="34",
            enqueued_time=datetime(2025, 1, 2, tzinfo=UTC),
            iothub_metadata={"device": "sensor-1"},
        )
    )

    assert payload["body"] == "event hub payload"
    assert payload["body_encoding"] == "utf-8"
    assert payload["partition_key"] == "partition-key"
    assert payload["sequence_number"] == 12
    assert payload["offset"] == "34"
    assert payload["enqueued_time"] == "2025-01-02T00:00:00+00:00"
    assert payload["iothub_metadata"] == {"device": "sensor-1"}


def test_kafka_event_serializes_body_and_metadata() -> None:
    payload = _serialized(
        func.KafkaEvent(
            body=b"kafka payload",
            key="key-1",
            topic="orders",
            partition=2,
            offset=34,
            timestamp="2025-01-02T00:00:00Z",
            headers=[{"header": "value"}],
        )
    )

    assert payload["body"] == "kafka payload"
    assert payload["body_encoding"] == "utf-8"
    assert payload["key"] == "key-1"
    assert payload["topic"] == "orders"
    assert payload["partition"] == 2
    assert payload["offset"] == 34
    assert payload["headers"] == [{"header": "value"}]


def test_timer_request_serializes_public_properties() -> None:
    assert _serialized(_TimerRequest()) == {
        "past_due": False,
        "schedule_status": {"last": "2025-01-02T00:00:00+00:00"},
        "schedule": {"adjust_for_dst": True},
    }


def test_cosmos_document_list_serializes_documents_and_none_items() -> None:
    payload = _serialized(func.DocumentList([func.Document({"id": "cosmos"}), None]))

    assert payload == [{"id": "cosmos"}, None]


def test_sql_row_lists_serialize_rows_and_none_items() -> None:
    sql_payload = _serialized(func.SqlRowList([func.SqlRow({"id": "sql"}), None]))
    mysql_payload = _serialized(func.MySqlRowList([func.MySqlRow({"id": "mysql"}), None]))

    assert sql_payload == [{"id": "sql"}, None]
    assert mysql_payload == [{"id": "mysql"}, None]


def test_single_document_and_sql_rows_serialize_without_to_json() -> None:
    assert _serialized(func.Document({"id": "document"})) == {"id": "document"}
    assert _serialized(func.SqlRow({"id": "sql"})) == {"id": "sql"}
    assert _serialized(func.MySqlRow({"id": "mysql"})) == {"id": "mysql"}


def test_native_contracts_are_preferred_before_adapters() -> None:
    assert _serialized(_NativeContract()) == {"source": "native"}
    assert _serialized(_ModelContract()) == {"source": "model"}
    assert serialize_trigger_data({"message": "hello"}) == '{"message": "hello"}'


def test_plain_list_recursively_serializes_message_batches() -> None:
    payload = _serialized(
        [
            func.EventHubEvent(body=b"first"),
            func.KafkaEvent(body=b"second"),
            func.ServiceBusMessage(body=b"third"),
        ]
    )

    assert payload == [
        {"body": "first", "body_encoding": "utf-8"},
        {"body": "second", "body_encoding": "utf-8"},
        {
            "body": "third",
            "body_encoding": "utf-8",
            "message_id": "",
            "application_properties": {},
            "user_properties": {},
        },
    ]
