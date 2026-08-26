"""Replay-safe Durable orchestration helpers for markdown agents."""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

import azure.durable_functions as df
from azure.durable_functions.models.Task import TaskBase  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from azure.durable_functions import DurableOrchestrationContext as _DurableContextBase
else:

    class _DurableContextBase:
        pass


type JSONPrimitive = str | int | float | bool | None
type JSONValue = JSONPrimitive | list[JSONValue] | dict[str, JSONValue]

_INTERNAL_AGENT_ACTIVITY_NAME = "azure_functions_agents_run_markdown_agent"
_ACTIVITY_PAYLOAD_VERSION: Literal[1] = 1


class _DurableAgentActivityInput(TypedDict):
    schema_version: Literal[1]
    agent_name: str
    input: JSONValue
    durable_instance_id: str


def _validate_json_value(value: object) -> None:
    if value is None or isinstance(value, str | bool | int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("call_agent input cannot contain NaN or infinity")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("call_agent input object keys must be strings")
            _validate_json_value(item)
        return
    raise TypeError(
        "call_agent input must contain only JSON values "
        f"(received {type(value).__name__})"
    )


def _canonicalize_json_value(value: object) -> JSONValue:
    _validate_json_value(value)
    encoded = json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return cast(JSONValue, json.loads(encoded))


def _parse_activity_input(value: object) -> _DurableAgentActivityInput:
    if not isinstance(value, dict):
        raise TypeError("Markdown Agent activity input must be a JSON object")
    expected_fields = {
        "schema_version",
        "agent_name",
        "input",
        "durable_instance_id",
    }
    if set(value) != expected_fields:
        raise ValueError(
            "Markdown Agent activity input must contain exactly: "
            + ", ".join(sorted(expected_fields))
        )
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError(
            "Unsupported Markdown Agent activity payload schema_version; expected 1"
        )
    agent_name = value["agent_name"]
    if not isinstance(agent_name, str) or not agent_name.strip():
        raise ValueError("Markdown Agent activity agent_name must be a non-empty string")
    durable_instance_id = value["durable_instance_id"]
    if not isinstance(durable_instance_id, str) or not durable_instance_id:
        raise ValueError(
            "Markdown Agent activity durable_instance_id must be a non-empty string"
        )
    return _DurableAgentActivityInput(
        schema_version=1,
        agent_name=agent_name,
        input=_canonicalize_json_value(value["input"]),
        durable_instance_id=durable_instance_id,
    )


def _normalize_agent_prompt(value: JSONValue) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


class DurableAgentContext(_DurableContextBase):  # type: ignore[misc]
    """Durable context proxy that schedules stateless markdown Agent activities."""

    def __init__(self, context: df.DurableOrchestrationContext) -> None:
        self._context = context

    def __getattr__(self, name: str) -> Any:
        return getattr(self._context, name)

    def call_agent(
        self,
        agent_name: str,
        input_: JSONValue,
        *,
        retry_options: df.RetryOptions | None = None,
    ) -> TaskBase:
        """Schedule the runtime-owned activity for one stateless Agent call."""
        if not isinstance(agent_name, str) or not agent_name.strip():
            raise ValueError("call_agent agent_name must be a non-empty string")
        normalized_input = _canonicalize_json_value(input_)
        payload = _DurableAgentActivityInput(
            schema_version=_ACTIVITY_PAYLOAD_VERSION,
            agent_name=agent_name,
            input=normalized_input,
            durable_instance_id=str(self._context.instance_id),
        )
        if retry_options is None:
            return self._context.call_activity(_INTERNAL_AGENT_ACTIVITY_NAME, payload)
        if not isinstance(retry_options, df.RetryOptions):
            raise TypeError("call_agent retry_options must be RetryOptions or None")
        return self._context.call_activity_with_retry(
            _INTERNAL_AGENT_ACTIVITY_NAME,
            retry_options,
            payload,
        )