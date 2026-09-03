from __future__ import annotations

import json
import math
import time

import pytest
from pydantic import ValidationError

from azure_functions_agents.experimental.hybrid_protocol import (
    HYBRID_TOOL_PROTOCOL_VERSION,
    MAX_HYBRID_ARGUMENT_BYTES,
    HybridInvocationErrorCode,
    HybridInvocationStatus,
    HybridProtocolDocumentError,
    HybridToolDescriptor,
    HybridToolInternalTimings,
    HybridToolInvocationError,
    HybridToolInvocationRequest,
    HybridToolInvocationResult,
    HybridToolManifest,
    HybridToolProvenance,
    canonical_hybrid_json_bytes,
    parse_hybrid_tool_manifest,
    parse_hybrid_tool_request,
    parse_hybrid_tool_result,
)


def _descriptor(name: str = "weather") -> HybridToolDescriptor:
    return HybridToolDescriptor(
        name=name,
        description="Read weather.",
        parameters={
            "additionalProperties": False,
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
            "type": "object",
        },
        provenance=HybridToolProvenance.LOCAL,
    )


def _timings() -> HybridToolInternalTimings:
    return HybridToolInternalTimings(
        queue_wait_ms=1.0,
        execution_ms=2.0,
        serialization_ms=0.1,
    )


def test_manifest_round_trips_through_canonical_json() -> None:
    manifest = HybridToolManifest(
        protocol_version=HYBRID_TOOL_PROTOCOL_VERSION,
        tools=(_descriptor(),),
    )

    payload = canonical_hybrid_json_bytes(manifest)

    assert payload == canonical_hybrid_json_bytes(parse_hybrid_tool_manifest(payload))
    assert b": " not in payload
    assert b", " not in payload


@pytest.mark.parametrize(
    "parser,payload",
    [
        (
            parse_hybrid_tool_manifest,
            '{"protocol_version":"1","tools":[],"tools":[]}',
        ),
        (
            parse_hybrid_tool_request,
            (
                '{"protocol_version":"1","call_id":"c","tool_name":"t",'
                '"arguments":{"x":1,"x":2},"deadline_unix_seconds":1.0,'
                '"traceparent":null,"operation_id":"o"}'
            ),
        ),
    ],
)
def test_protocol_rejects_duplicate_keys(parser: object, payload: str) -> None:
    with pytest.raises(HybridProtocolDocumentError):
        parser(payload)  # type: ignore[operator]


def test_request_rejects_extra_fields_wrong_types_and_oversized_arguments() -> None:
    valid = {
        "arguments": {},
        "call_id": "call-1",
        "deadline_unix_seconds": time.time() + 30,
        "operation_id": "operation-1",
        "protocol_version": "1",
        "tool_name": "weather",
        "traceparent": None,
    }

    with pytest.raises(HybridProtocolDocumentError):
        parse_hybrid_tool_request(json.dumps({**valid, "extra": True}))
    with pytest.raises(HybridProtocolDocumentError):
        parse_hybrid_tool_request(json.dumps({**valid, "call_id": 1}))
    with pytest.raises(ValidationError):
        HybridToolInvocationRequest(
            **{**valid, "arguments": {"x": "a" * MAX_HYBRID_ARGUMENT_BYTES}}
        )


def test_nonfinite_deadlines_timings_and_values_are_rejected() -> None:
    with pytest.raises(ValidationError):
        HybridToolInvocationRequest(
            protocol_version="1",
            call_id="c",
            tool_name="weather",
            arguments={},
            deadline_unix_seconds=math.inf,
            traceparent=None,
            operation_id="o",
        )
    with pytest.raises(ValidationError):
        HybridToolInternalTimings(
            queue_wait_ms=math.nan,
            execution_ms=0.0,
            serialization_ms=0.0,
        )
    with pytest.raises(ValueError):
        canonical_hybrid_json_bytes({"value": math.nan})


def test_unique_tool_names_and_object_schemas_are_required() -> None:
    with pytest.raises(ValidationError):
        HybridToolManifest(protocol_version="1", tools=(_descriptor(), _descriptor()))
    with pytest.raises(ValidationError):
        HybridToolDescriptor(
            name="bad name",
            description="",
            parameters={"type": "array"},
            provenance=HybridToolProvenance.GENERIC,
        )


def test_wire_documents_require_explicit_protocol_and_envelope_fields() -> None:
    with pytest.raises(HybridProtocolDocumentError):
        parse_hybrid_tool_manifest('{"tools":[]}')
    with pytest.raises(HybridProtocolDocumentError):
        parse_hybrid_tool_request(
            '{"arguments":{},"call_id":"c","deadline_unix_seconds":1.0,'
            '"operation_id":"o","protocol_version":"1","tool_name":"weather"}'
        )


def test_result_requires_status_consistent_error_and_is_strictly_parseable() -> None:
    result = HybridToolInvocationResult(
        protocol_version="1",
        call_id="call-1",
        tool_name="weather",
        status=HybridInvocationStatus.ERROR,
        value=None,
        stdout="",
        stderr="",
        exit_code=None,
        error=HybridToolInvocationError(
            code=HybridInvocationErrorCode.TOOL_ERROR,
            message="failed",
            retryable=False,
        ),
        timings=_timings(),
    )

    parsed = parse_hybrid_tool_result(canonical_hybrid_json_bytes(result))

    assert parsed.error is not None
    assert parsed.error.code == "tool_error"
    with pytest.raises(ValidationError):
        HybridToolInvocationResult(
            protocol_version="1",
            call_id="call-1",
            tool_name="weather",
            status=HybridInvocationStatus.SUCCESS,
            value=None,
            stdout="",
            stderr="",
            exit_code=None,
            error=HybridToolInvocationError(
                code=HybridInvocationErrorCode.TOOL_ERROR,
                message="failed",
                retryable=False,
            ),
            timings=_timings(),
        )


def test_traceparent_must_be_canonical_w3c_shape() -> None:
    with pytest.raises(ValidationError):
        HybridToolInvocationRequest(
            call_id="call-1",
            tool_name="weather",
            arguments={},
            deadline_unix_seconds=time.time() + 30,
            traceparent="00-not-a-traceparent",
            operation_id="operation",
        )
