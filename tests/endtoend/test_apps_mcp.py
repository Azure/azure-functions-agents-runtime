"""End-to-end functional tests that invoke MCP-triggered agents.

Agents with ``builtin_endpoints.mcp: true`` are registered as tools on the
Azure Functions MCP extension. There is no HTTP route to hit directly; instead
the tool is reached over the MCP protocol at ``/runtime/webhooks/mcp`` (the
Functions host does not enforce the MCP system key locally). The
``builtin-endpoints`` app's ``ui_and_mcp`` agent registers a single MCP tool
named after its slug (``ui_and_mcp``).

Deterministic, provider-independent assertions:

* the MCP function is indexed (admin API lists a ``mcpToolTrigger`` binding);
* ``tools/list`` advertises the ``ui_and_mcp`` tool with a required ``prompt``
  string property;
* ``tools/call`` with a valid prompt routes to the agent handler, which returns
  structured JSON. Without an LLM provider the handler catches the failure and
  returns ``{"error": ...}`` (still proving the call reached the agent); with a
  provider it returns ``session_id``/``response``.

A happy-path assertion (successful agent response) needs a live LLM and
auto-skips unless a provider is configured. These require ``func`` + Azurite and
are marked ``e2e`` (excluded from the default unit run; the E2E pipeline runs
``-m e2e``).
"""

from __future__ import annotations

import contextlib
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.endtoend._func_host import HostHandle, running_host
from tests.endtoend._http_probe import HttpClient, discover_functions
from tests.endtoend._mcp_probe import call_mcp_tool, list_mcp_tools

APPS_DIR = Path(__file__).resolve().parent / "apps"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(shutil.which("func") is None, reason="Azure Functions Core Tools not found"),
]

# The ``ui_and_mcp`` agent's source file is ``ui_and_mcp.agent.md``, so its slug
# (and therefore its MCP tool name) is ``ui_and_mcp``.
EXPECTED_TOOL_NAME = "ui_and_mcp"

# Served MCP hosts are (handle, client): the handle exposes the base URL used to
# reach the MCP webhook, the client is used to wait for host readiness and to
# enumerate indexed functions.
Served = tuple[HostHandle, HttpClient]


def _provider_configured() -> bool:
    """Whether an LLM provider appears configured (env vars or app settings)."""
    from tests.endtoend._func_host import configured_provider

    return bool(
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("AZURE_OPENAI_ENDPOINT")
        or os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
        or configured_provider(APPS_DIR / "builtin-endpoints") is not None
    )


requires_llm = pytest.mark.skipif(
    not _provider_configured(), reason="no LLM provider configured (set OPENAI_API_KEY etc.)"
)


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
def builtin_endpoints_host() -> Iterator[Served]:
    with _serve("builtin-endpoints") as served:
        yield served


# --------------------------------------------------------------------------- #
# MCP discovery
# --------------------------------------------------------------------------- #


def test_admin_api_discovers_mcp_tool_function(builtin_endpoints_host: Served) -> None:
    """The MCP agent is indexed with an ``mcpToolTrigger`` binding (no HTTP route)."""
    _, client = builtin_endpoints_host

    mcp_functions = [
        fn for fn in discover_functions(client) if "mcp" in fn.trigger_type.lower()
    ]

    assert mcp_functions, "expected at least one mcpToolTrigger function to be indexed"
    for fn in mcp_functions:
        assert fn.route is None, "MCP tool triggers must not expose an HTTP route"
        assert fn.methods == (), "MCP tool triggers must not list HTTP methods"


def test_mcp_endpoint_lists_agent_tool(builtin_endpoints_host: Served) -> None:
    """``tools/list`` advertises the agent tool with a required ``prompt`` string.

    This is fully deterministic: tool discovery never runs the agent, so it does
    not depend on an LLM provider.
    """
    handle, _ = builtin_endpoints_host

    tools = list_mcp_tools(handle.base_url)

    by_name = {tool.name: tool for tool in tools}
    assert EXPECTED_TOOL_NAME in by_name, (
        f"expected MCP tool {EXPECTED_TOOL_NAME!r}, got {sorted(by_name)}"
    )

    tool = by_name[EXPECTED_TOOL_NAME]
    assert "prompt" in tool.property_names(), "the agent tool must expose a 'prompt' property"
    assert "prompt" in tool.required_properties(), "'prompt' must be a required property"


# --------------------------------------------------------------------------- #
# MCP invocation
# --------------------------------------------------------------------------- #


def test_mcp_tool_call_routes_to_agent(builtin_endpoints_host: Served) -> None:
    """Calling the tool with a valid prompt reaches the agent handler.

    The handler always returns structured JSON. Without a provider it returns
    ``{"error": ...}`` (proving the call routed to the agent); with a provider
    it returns ``session_id``/``response``. Either shape proves end-to-end MCP
    routing independent of whether an LLM is configured.
    """
    handle, _ = builtin_endpoints_host

    result = call_mcp_tool(handle.base_url, EXPECTED_TOOL_NAME, {"prompt": "ping"})
    payload = result.json()

    assert isinstance(payload, dict), f"expected a JSON object result, got {payload!r}"
    if "error" in payload:
        # No provider configured: the agent run failed and was reported as JSON.
        assert payload["error"], "error payload must carry a message"
    else:
        assert "session_id" in payload and "response" in payload, (
            f"expected an agent response payload, got {sorted(payload)}"
        )


def test_mcp_tool_call_missing_prompt_is_rejected(builtin_endpoints_host: Served) -> None:
    """Calling the tool without ``prompt`` is rejected before any agent run.

    Whether the MCP extension enforces the required property or the agent
    handler does (returning ``{"error": "Missing 'prompt'"}``), the invocation
    surfaces an error rather than a successful agent response. This is fully
    deterministic and needs no LLM provider.
    """
    handle, _ = builtin_endpoints_host

    result = call_mcp_tool(handle.base_url, EXPECTED_TOOL_NAME, {})

    errored = result.is_error or "prompt" in result.text.lower()
    assert errored, f"expected a missing-prompt error, got: {result.text!r}"


@requires_llm
def test_mcp_tool_call_succeeds_with_provider(builtin_endpoints_host: Served) -> None:
    """With an LLM provider, the tool returns a successful agent response."""
    handle, _ = builtin_endpoints_host

    result = call_mcp_tool(handle.base_url, EXPECTED_TOOL_NAME, {"prompt": "Say hello."})
    payload = result.json()

    assert not result.is_error, f"tool call reported an error: {result.text!r}"
    assert "error" not in payload, f"agent run failed: {payload!r}"
    assert payload.get("response"), "expected a non-empty agent response"
    assert payload.get("session_id"), "expected a session id in the response"
