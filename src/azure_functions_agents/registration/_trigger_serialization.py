"""Serialization adapters for Azure Functions non-HTTP trigger bindings."""

from __future__ import annotations

import base64
import json
from collections import UserList
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Protocol, cast

import azure.functions as func
import azure.functions.blob as blob

type BindingType = type[object]
type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type TriggerPayload = dict[str, JsonValue] | list[JsonValue]
type EncodedBytes = tuple[str, str]


class TriggerBindingSerializer(Protocol):
    """Adapter for one Azure Functions trigger binding family."""

    def matches(self, binding: object) -> bool:
        """Return whether this adapter supports the binding."""

    def serialize(self, binding: object) -> TriggerPayload:
        """Build a JSON-safe payload for the binding."""


class _Missing:
    pass


_MISSING = _Missing()


def _binding_type(module: object, name: str) -> BindingType | None:
    candidate = getattr(module, name, None)
    return cast(BindingType, candidate) if isinstance(candidate, type) else None


_INPUT_STREAM_TYPE = _binding_type(blob, "InputStream")
_QUEUE_MESSAGE_TYPE = _binding_type(func, "QueueMessage")
_SERVICE_BUS_MESSAGE_TYPE = _binding_type(func, "ServiceBusMessage")
_EVENT_GRID_EVENT_TYPE = _binding_type(func, "EventGridEvent")
_EVENT_HUB_EVENT_TYPE = _binding_type(func, "EventHubEvent")
_KAFKA_EVENT_TYPE = _binding_type(func, "KafkaEvent")
_TIMER_REQUEST_TYPE = _binding_type(func, "TimerRequest")
_DOCUMENT_LIST_TYPE = _binding_type(func, "DocumentList")
_SQL_ROW_LIST_TYPE = _binding_type(func, "SqlRowList")
_SQL_ROW_TYPE = _binding_type(func, "SqlRow")
_MYSQL_ROW_LIST_TYPE = _binding_type(func, "MySqlRowList")
_MYSQL_ROW_TYPE = _binding_type(func, "MySqlRow")


def _matches_binding_type(binding: object, binding_type: BindingType | None) -> bool:
    return binding_type is not None and isinstance(binding, binding_type)


def _encode_bytes(value: bytes) -> EncodedBytes:
    """Return body text and an encoding marker without losing binary payloads."""
    try:
        return value.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return base64.b64encode(value).decode("ascii"), "base64"


def _iso(value: datetime) -> str:
    """Serialize datetimes in the Azure Functions binding metadata."""
    return value.isoformat()


def _mapping_json_safe(value: Mapping[Any, Any]) -> dict[str, JsonValue]:
    return {str(key): _json_safe(item) for key, item in value.items()}


def _json_safe(value: object) -> JsonValue:
    """Convert binding metadata into values JSON can represent."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, (bytes, bytearray)):
        encoded, encoding = _encode_bytes(bytes(value))
        return {"data": encoded, "data_encoding": encoding}
    if isinstance(value, Mapping):
        return _mapping_json_safe(value)
    if isinstance(value, (list, tuple, UserList)):
        return [_json_safe(item) for item in value]
    return str(value)


def _safe_getattr(binding: object, field: str) -> object | _Missing:
    try:
        return cast(object, getattr(binding, field))
    except Exception:
        return _MISSING


def _safe_call(binding: object, method_name: str) -> object | _Missing:
    method = _safe_getattr(binding, method_name)
    if method is _MISSING or not callable(method):
        return _MISSING
    try:
        return cast(object, method())
    except Exception:
        return _MISSING


def _add_value(payload: dict[str, JsonValue], field: str, value: object | _Missing) -> None:
    if value is _MISSING or value is None:
        return
    payload[field] = _json_safe(value)


def _add_fields(payload: dict[str, JsonValue], binding: object, fields: tuple[str, ...]) -> None:
    for field in fields:
        _add_value(payload, field, _safe_getattr(binding, field))


def _add_body(payload: dict[str, JsonValue], binding: object) -> None:
    body = _safe_call(binding, "get_body")
    if body is _MISSING or body is None:
        return
    if isinstance(body, str):
        payload["body"] = body
        payload["body_encoding"] = "utf-8"
        return
    if isinstance(body, (bytes, bytearray)):
        encoded, encoding = _encode_bytes(bytes(body))
        payload["body"] = encoded
        payload["body_encoding"] = encoding
        return
    payload["body"] = _json_safe(body)


def _to_mapping(value: Any) -> dict[Any, Any] | _Missing:
    try:
        return dict(value)
    except Exception:
        return _MISSING


def _serialize_document(document: object) -> JsonValue:
    if document is None:
        return None
    payload = _safe_call(document, "to_dict")
    if isinstance(payload, dict):
        return _mapping_json_safe(payload)
    mapping = _to_mapping(document)
    if isinstance(mapping, dict):
        return _mapping_json_safe(mapping)
    return str(document)


def _row_payload(row: object) -> dict[str, JsonValue]:
    mapping = _to_mapping(row)
    if isinstance(mapping, dict):
        return _mapping_json_safe(mapping)
    return {"value": _json_safe(row)}


def _serialize_row(row: object) -> JsonValue:
    if row is None:
        return None
    return _row_payload(row)


def _serialize_rows(
    rows: Any,
    serialize_item: Callable[[object], JsonValue],
) -> list[JsonValue]:
    return [serialize_item(row) for row in rows]


class _InputStreamSerializer(TriggerBindingSerializer):
    def matches(self, binding: object) -> bool:
        return _matches_binding_type(binding, _INPUT_STREAM_TYPE)

    def serialize(self, binding: object) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {}
        _add_fields(payload, binding, ("name", "uri", "length", "blob_properties", "metadata"))
        return payload


class _QueueMessageSerializer(TriggerBindingSerializer):
    def matches(self, binding: object) -> bool:
        return _matches_binding_type(binding, _QUEUE_MESSAGE_TYPE)

    def serialize(self, binding: object) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {}
        _add_body(payload, binding)
        _add_fields(
            payload,
            binding,
            (
                "id",
                "dequeue_count",
                "insertion_time",
                "expiration_time",
                "time_next_visible",
                "pop_receipt",
            ),
        )
        body_json = _safe_call(binding, "get_json")
        if body_json is not _MISSING:
            payload["body_json"] = _json_safe(body_json)
        return payload


class _ServiceBusMessageSerializer(TriggerBindingSerializer):
    def matches(self, binding: object) -> bool:
        return _matches_binding_type(binding, _SERVICE_BUS_MESSAGE_TYPE)

    def serialize(self, binding: object) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {}
        _add_body(payload, binding)
        _add_fields(
            payload,
            binding,
            (
                "message_id",
                "correlation_id",
                "content_type",
                "subject",
                "session_id",
                "partition_key",
                "reply_to",
                "to",
                "delivery_count",
                "enqueued_time_utc",
                "expires_at_utc",
                "sequence_number",
                "application_properties",
                "user_properties",
                "metadata",
            ),
        )
        return payload


class _EventGridEventSerializer(TriggerBindingSerializer):
    def matches(self, binding: object) -> bool:
        return _matches_binding_type(binding, _EVENT_GRID_EVENT_TYPE)

    def serialize(self, binding: object) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {}
        _add_fields(
            payload,
            binding,
            ("id", "topic", "subject", "event_type", "event_time", "data_version"),
        )
        data = _safe_call(binding, "get_json")
        if data is not _MISSING:
            payload["data"] = _json_safe(data)
        return payload


class _EventHubEventSerializer(TriggerBindingSerializer):
    def matches(self, binding: object) -> bool:
        return _matches_binding_type(binding, _EVENT_HUB_EVENT_TYPE)

    def serialize(self, binding: object) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {}
        _add_body(payload, binding)
        _add_fields(
            payload,
            binding,
            (
                "partition_key",
                "sequence_number",
                "offset",
                "enqueued_time",
                "iothub_metadata",
                "metadata",
            ),
        )
        return payload


class _KafkaEventSerializer(TriggerBindingSerializer):
    def matches(self, binding: object) -> bool:
        return _matches_binding_type(binding, _KAFKA_EVENT_TYPE)

    def serialize(self, binding: object) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {}
        _add_body(payload, binding)
        _add_fields(
            payload,
            binding,
            ("key", "topic", "partition", "offset", "timestamp", "headers", "metadata"),
        )
        return payload


class _TimerRequestSerializer(TriggerBindingSerializer):
    def matches(self, binding: object) -> bool:
        return _matches_binding_type(binding, _TIMER_REQUEST_TYPE)

    def serialize(self, binding: object) -> dict[str, JsonValue]:
        payload: dict[str, JsonValue] = {}
        _add_fields(payload, binding, ("past_due", "schedule_status", "schedule"))
        return payload


class _DocumentListSerializer(TriggerBindingSerializer):
    def matches(self, binding: object) -> bool:
        return _matches_binding_type(binding, _DOCUMENT_LIST_TYPE)

    def serialize(self, binding: object) -> list[JsonValue]:
        return _serialize_rows(binding, _serialize_document)


class _SqlSerializer(TriggerBindingSerializer):
    def matches(self, binding: object) -> bool:
        return _matches_binding_type(binding, _SQL_ROW_LIST_TYPE) or _matches_binding_type(
            binding, _SQL_ROW_TYPE
        )

    def serialize(self, binding: object) -> TriggerPayload:
        if _matches_binding_type(binding, _SQL_ROW_LIST_TYPE):
            return _serialize_rows(binding, _serialize_row)
        return _row_payload(binding)


class _MySqlSerializer(TriggerBindingSerializer):
    def matches(self, binding: object) -> bool:
        return _matches_binding_type(binding, _MYSQL_ROW_LIST_TYPE) or _matches_binding_type(
            binding, _MYSQL_ROW_TYPE
        )

    def serialize(self, binding: object) -> TriggerPayload:
        if _matches_binding_type(binding, _MYSQL_ROW_LIST_TYPE):
            return _serialize_rows(binding, _serialize_row)
        return _row_payload(binding)


_TRIGGER_SERIALIZERS: tuple[TriggerBindingSerializer, ...] = (
    _InputStreamSerializer(),
    _QueueMessageSerializer(),
    _ServiceBusMessageSerializer(),
    _EventGridEventSerializer(),
    _EventHubEventSerializer(),
    _KafkaEventSerializer(),
    _TimerRequestSerializer(),
    _DocumentListSerializer(),
    _SqlSerializer(),
    _MySqlSerializer(),
)


def _native_contract_payload(binding: object) -> dict[Any, Any] | _Missing:
    for method_name in ("to_dict", "model_dump"):
        payload = _safe_call(binding, method_name)
        if isinstance(payload, dict):
            return payload
    return _MISSING


def _adapter_payload(binding: object) -> TriggerPayload | _Missing:
    for serializer in _TRIGGER_SERIALIZERS:
        if serializer.matches(binding):
            return serializer.serialize(binding)
    return _MISSING


def _serialize_item(binding: object) -> JsonValue:
    if binding is None or isinstance(binding, (str, int, float, bool)):
        return binding
    if isinstance(binding, dict):
        return _json_safe(binding)

    native_payload = _native_contract_payload(binding)
    if isinstance(native_payload, dict):
        return _mapping_json_safe(native_payload)

    adapter_payload = _adapter_payload(binding)
    if isinstance(adapter_payload, (dict, list)):
        return adapter_payload

    if isinstance(binding, (list, tuple, UserList)):
        return [_serialize_item(item) for item in binding]

    if isinstance(binding, (bytes, bytearray)):
        encoded, encoding = _encode_bytes(bytes(binding))
        return {"data": encoded, "data_encoding": encoding}

    return str(binding)


def serialize_trigger_data(trigger_data: object) -> str:
    """Serialize non-HTTP trigger data without leaking Azure binding reprs."""
    if trigger_data is None:
        return "{}"
    if isinstance(trigger_data, str):
        return trigger_data
    if isinstance(trigger_data, dict):
        return json.dumps(_json_safe(trigger_data), ensure_ascii=False, default=str)

    native_payload = _native_contract_payload(trigger_data)
    if isinstance(native_payload, dict):
        return json.dumps(_mapping_json_safe(native_payload), ensure_ascii=False, default=str)

    adapter_payload = _adapter_payload(trigger_data)
    if isinstance(adapter_payload, (dict, list)):
        return json.dumps(adapter_payload, ensure_ascii=False, default=str)

    if isinstance(trigger_data, (list, tuple, UserList)):
        return json.dumps(
            [_serialize_item(item) for item in trigger_data],
            ensure_ascii=False,
            default=str,
        )

    if isinstance(trigger_data, (bytes, bytearray)):
        encoded, encoding = _encode_bytes(bytes(trigger_data))
        return json.dumps(
            {"data": encoded, "data_encoding": encoding},
            ensure_ascii=False,
            default=str,
        )

    return str(trigger_data)
