"""Strict JSON-object decoding for untrusted document boundaries."""

from __future__ import annotations

import json
import math

from pydantic import BaseModel


class DuplicateJsonKeyError(ValueError):
    """Raised when a JSON object repeats a key."""


def decode_json_object(payload: bytes | str) -> dict[str, object]:
    """Decode one JSON object while rejecting repeated keys."""
    raw = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    decoded: object = json.loads(raw, object_pairs_hook=_json_object)
    if not isinstance(decoded, dict):
        raise TypeError("JSON document must be an object")
    return decoded


def canonical_json_bytes(value: object) -> bytes:
    """Serialize one JSON-safe value deterministically."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    assert_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def assert_json_value(value: object, *, depth: int = 0) -> None:
    """Reject non-JSON values, non-string keys, and excessive nesting."""
    if depth > 64:
        raise ValueError("JSON value is nested too deeply")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON value contains a non-finite number")
        return
    if isinstance(value, list | tuple):
        for item in value:
            assert_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON value contains a non-string object key")
            assert_json_value(item, depth=depth + 1)
        return
    raise ValueError("JSON value contains a non-JSON value")


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError
        result[key] = value
    return result
