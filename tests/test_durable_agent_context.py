from __future__ import annotations

import math
from typing import Any
from unittest.mock import MagicMock

import azure.durable_functions as df
import pytest

from azure_functions_agents import DurableAgentContext
from azure_functions_agents.durable import (
    _INTERNAL_AGENT_ACTIVITY_NAME,
    _normalize_agent_prompt,
    _parse_activity_input,
)


def _context() -> MagicMock:
    context = MagicMock(spec=df.DurableOrchestrationContext)
    context.instance_id = "durable-instance"
    return context


def test_context_delegates_standard_durable_api() -> None:
    context = _context()
    context.get_input.return_value = {"order": 42}
    proxy = DurableAgentContext(context)

    assert proxy.instance_id == "durable-instance"
    assert proxy.get_input() == {"order": 42}
    context.get_input.assert_called_once_with()


def test_call_agent_schedules_deterministic_activity_payload() -> None:
    context = _context()
    task = MagicMock()
    context.call_activity.return_value = task

    result = DurableAgentContext(context).call_agent(
        "order-fulfillment",
        {"z": [3, True, None], "a": {"value": 1}},
    )

    assert result is task
    context.call_activity.assert_called_once_with(
        _INTERNAL_AGENT_ACTIVITY_NAME,
        {
            "schema_version": 1,
            "agent_name": "order-fulfillment",
            "input": {"a": {"value": 1}, "z": [3, True, None]},
            "durable_instance_id": "durable-instance",
        },
    )
    context.call_activity_with_retry.assert_not_called()


def test_call_agent_routes_retry_options() -> None:
    context = _context()
    task = MagicMock()
    context.call_activity_with_retry.return_value = task
    retry_options = df.RetryOptions(
        first_retry_interval_in_milliseconds=1_000,
        max_number_of_attempts=3,
    )

    result = DurableAgentContext(context).call_agent(
        "orders",
        "Assess this order",
        retry_options=retry_options,
    )

    assert result is task
    context.call_activity_with_retry.assert_called_once_with(
        _INTERNAL_AGENT_ACTIVITY_NAME,
        retry_options,
        {
            "schema_version": 1,
            "agent_name": "orders",
            "input": "Assess this order",
            "durable_instance_id": "durable-instance",
        },
    )
    context.call_activity.assert_not_called()


@pytest.mark.parametrize("agent_name", ["", "   ", None, 42])
def test_call_agent_rejects_invalid_names(agent_name: Any) -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        DurableAgentContext(_context()).call_agent(agent_name, "prompt")


@pytest.mark.parametrize(
    ("input_value", "error", "message"),
    [
        ({"invalid": object()}, TypeError, "only JSON values"),
        ({1: "invalid"}, TypeError, "keys must be strings"),
        (("tuple",), TypeError, "only JSON values"),
        (math.nan, ValueError, "NaN or infinity"),
        (math.inf, ValueError, "NaN or infinity"),
    ],
)
def test_call_agent_rejects_non_json_input(
    input_value: Any,
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        DurableAgentContext(_context()).call_agent("orders", input_value)


def test_call_agent_rejects_invalid_retry_options() -> None:
    with pytest.raises(TypeError, match="RetryOptions or None"):
        DurableAgentContext(_context()).call_agent(
            "orders",
            "prompt",
            retry_options=object(),  # type: ignore[arg-type]
        )


def test_activity_payload_parser_and_prompt_normalization() -> None:
    parsed = _parse_activity_input(
        {
            "schema_version": 1,
            "agent_name": "orders",
            "input": {"z": 2, "a": 1},
            "durable_instance_id": "instance-1",
        }
    )

    assert parsed["input"] == {"a": 1, "z": 2}
    assert _normalize_agent_prompt(parsed["input"]) == '{"a":1,"z":2}'
    assert _normalize_agent_prompt("unchanged") == "unchanged"


@pytest.mark.parametrize(
    ("payload", "error", "message"),
    [
        (None, TypeError, "must be a JSON object"),
        ({}, ValueError, "must contain exactly"),
        (
            {
                "schema_version": 2,
                "agent_name": "orders",
                "input": "prompt",
                "durable_instance_id": "instance-1",
            },
            ValueError,
            "schema_version",
        ),
        (
            {
                "schema_version": 1,
                "agent_name": " ",
                "input": "prompt",
                "durable_instance_id": "instance-1",
            },
            ValueError,
            "agent_name",
        ),
    ],
)
def test_activity_payload_parser_rejects_malformed_input(
    payload: object,
    error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(error, match=message):
        _parse_activity_input(payload)