"""Strict JSON-object decoding for untrusted document boundaries."""

from __future__ import annotations

import json


class DuplicateJsonKeyError(ValueError):
    """Raised when a JSON object repeats a key."""


def decode_json_object(payload: bytes | str) -> dict[str, object]:
    """Decode one JSON object while rejecting repeated keys."""
    raw = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    decoded: object = json.loads(raw, object_pairs_hook=_json_object)
    if not isinstance(decoded, dict):
        raise TypeError("JSON document must be an object")
    return decoded


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError
        result[key] = value
    return result
