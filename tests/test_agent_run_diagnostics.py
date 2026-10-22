from __future__ import annotations

from unittest.mock import Mock

import pytest

from azure_functions_agents._agent_run_diagnostics import _set_run_result_attributes
from azure_functions_agents._observability import (
    ATTR_AGENT_RESPONSE_BYTES,
    ATTR_AGENT_TOOL_CALL_COUNT,
    ATTR_AGENT_TOOL_ERROR_COUNT,
    RuntimeSpan,
)
from azure_functions_agents.runner import AgentResult


@pytest.mark.parametrize("content", ["", "ascii", "\u00e9", "\U0001f600"])
@pytest.mark.parametrize("capture_sensitive_data", [False, True])
def test_run_summary_counts_utf8_bytes_without_capturing_content(
    monkeypatch, content, capture_sensitive_data,
):
    monkeypatch.setattr(
        "azure_functions_agents._observability._capture_sensitive_data", capture_sensitive_data,
    )
    raw_span = Mock()
    result = AgentResult(session_id="public", content=content, delegate_error_count=2)

    _set_run_result_attributes(RuntimeSpan(raw_span), result)

    attributes = dict(call.args for call in raw_span.set_attribute.call_args_list)
    assert attributes[ATTR_AGENT_RESPONSE_BYTES] == len(content.encode("utf-8"))
    assert attributes[ATTR_AGENT_TOOL_CALL_COUNT] == 0
    assert attributes[ATTR_AGENT_TOOL_ERROR_COUNT] == 2
    if capture_sensitive_data:
        assert attributes["af.agent.response"] == content
    else:
        assert "af.agent.response" not in attributes
