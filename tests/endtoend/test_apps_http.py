"""End-to-end functional tests that invoke the HTTP endpoints of E2E apps.

Unlike ``test_apps_start.py`` (which only checks a clean ``func start``), these
tests keep a host running, discover its exposed HTTP endpoints via the Functions
admin API, invoke them with real HTTP requests, and assert on the responses.

Most assertions target deterministic product behavior that does not need a live
LLM: endpoint discovery, method handling, the static debug chat UI, missing
``prompt`` handling, and JSON-Schema input validation (which returns 400 before
any model call). A happy-path chat test is included but auto-skips unless an LLM
provider is configured, since the runtime has no built-in fake provider.

Like the other E2E tests these require ``func`` + Azurite and are marked ``e2e``
(excluded from the default unit run; the E2E pipeline runs ``-m e2e``).
"""

from __future__ import annotations

import contextlib
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.endtoend._func_host import running_host
from tests.endtoend._http_probe import (
    HttpClient,
    HttpEndpoint,
    discover_http_endpoints,
    expect_body_contains,
    expect_header,
    expect_json,
    expect_json_keys,
    expect_status,
    find_endpoint,
)

APPS_DIR = Path(__file__).resolve().parent / "apps"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(shutil.which("func") is None, reason="Azure Functions Core Tools not found"),
]

# Served hosts are (client, discovered endpoints).
Served = tuple[HttpClient, list[HttpEndpoint]]


def _provider_configured() -> bool:
    """Whether an LLM provider appears configured (env vars or app settings)."""
    from tests.endtoend._func_host import configured_provider

    return bool(
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("AZURE_OPENAI_ENDPOINT")
        or os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
        or configured_provider(APPS_DIR / "builtin-endpoints") is not None
        or configured_provider(APPS_DIR / "structured-io") is not None
    )


requires_llm = pytest.mark.skipif(
    not _provider_configured(), reason="no LLM provider configured (set OPENAI_API_KEY etc.)"
)


@contextlib.contextmanager
def _serve(app_name: str) -> Iterator[Served]:
    """Start ``app_name`` under ``func start`` and yield a client + endpoints."""
    with running_host(APPS_DIR / app_name) as handle:
        client = HttpClient(handle.base_url)
        try:
            client.wait_until_responsive()
            yield client, discover_http_endpoints(client)
        finally:
            client.close()


@pytest.fixture(scope="module")
def builtin_endpoints_host() -> Iterator[Served]:
    with _serve("builtin-endpoints") as served:
        yield served


@pytest.fixture(scope="module")
def structured_io_host() -> Iterator[Served]:
    with _serve("structured-io") as served:
        yield served


@pytest.fixture(scope="module")
def minimal_http_host() -> Iterator[Served]:
    with _serve("minimal-http") as served:
        yield served


# --------------------------------------------------------------------------- #
# Endpoint discovery
# --------------------------------------------------------------------------- #


def test_admin_api_discovers_http_endpoints(builtin_endpoints_host: Served) -> None:
    """The admin API should expose the builtin chat page and chat routes."""
    _, endpoints = builtin_endpoints_host

    assert endpoints, "expected at least one HTTP endpoint to be discovered"
    assert all(ep.methods for ep in endpoints), "every HTTP endpoint should list its methods"

    chat_page = find_endpoint(endpoints, route_exact="agents/main/", method="GET")
    assert chat_page.auth_level == "anonymous"

    chat = find_endpoint(endpoints, route_exact="agents/main/chat", method="POST")
    assert "POST" in chat.methods


def test_minimal_http_endpoint_discovery(minimal_http_host: Served) -> None:
    """The minimal app should expose a single anonymous POST ``echo`` route."""
    _, endpoints = minimal_http_host

    echo = find_endpoint(endpoints, route_exact="echo")
    assert echo.methods == ("POST",)
    assert echo.auth_level == "anonymous"


# --------------------------------------------------------------------------- #
# Deterministic behavior (no LLM required)
# --------------------------------------------------------------------------- #


def test_debug_chat_ui_returns_html(builtin_endpoints_host: Served) -> None:
    """GET on the debug chat page returns the static HTML UI (no model call)."""
    client, endpoints = builtin_endpoints_host
    page = find_endpoint(endpoints, route_exact="agents/main/", method="GET")

    resp = client.get(page.route)

    expect_status(resp, 200)
    assert "text/html" in resp.headers.get("Content-Type", "").lower()
    expect_body_contains(resp, "<html")


def test_chat_missing_prompt_returns_400(builtin_endpoints_host: Served) -> None:
    """POSTing an empty body to the chat API is rejected before any model call."""
    client, endpoints = builtin_endpoints_host
    chat = find_endpoint(endpoints, route_exact="agents/main/chat", method="POST")

    resp = client.post(chat.route, json={})

    expect_status(resp, 400)
    payload = expect_json(resp)
    assert "prompt" in str(payload).lower()


def test_chat_wrong_method_returns_404(builtin_endpoints_host: Served) -> None:
    """A GET against a POST-only chat route is not routed."""
    client, endpoints = builtin_endpoints_host
    chat = find_endpoint(endpoints, route_exact="agents/main/chat", method="POST")

    resp = client.get(chat.route)

    expect_status(resp, 404)


def test_unknown_route_returns_404(builtin_endpoints_host: Served) -> None:
    """A route the app never registered should not resolve."""
    client, _ = builtin_endpoints_host

    resp = client.post("agents/does-not-exist/chat", json={"prompt": "hi"})

    expect_status(resp, 404)


def test_http_agent_rejects_invalid_input_schema(structured_io_host: Served) -> None:
    """input_schema validation returns 400 (with session header) before the LLM."""
    client, endpoints = structured_io_host
    report = find_endpoint(endpoints, route_exact="structured-report", method="POST")

    # Missing both required properties.
    missing = client.post(report.route, json={})
    expect_status(missing, 400)
    payload = expect_json(missing)
    assert payload.get("error") == "Input validation failed"
    expect_header(missing, "x-ms-session-id")

    # Required present but report_type violates the enum.
    bad_enum = client.post(
        report.route,
        json={"subscription_id": "sub-123", "report_type": "not-a-real-type"},
    )
    expect_status(bad_enum, 400)
    assert expect_json(bad_enum).get("error") == "Input validation failed"


# --------------------------------------------------------------------------- #
# Happy path (requires a configured LLM provider)
# --------------------------------------------------------------------------- #


@requires_llm
def test_structured_report_happy_path(structured_io_host: Served) -> None:
    """A valid request returns 200 with a schema-shaped JSON body and session id."""
    client, endpoints = structured_io_host
    report = find_endpoint(endpoints, route_exact="structured-report", method="POST")

    resp = client.post(
        report.route,
        json={"subscription_id": "sub-123", "report_type": "cost"},
    )

    expect_status(resp, 200)
    expect_header(resp, "x-ms-session-id")
    expect_json_keys(resp, ("status", "summary"))


@requires_llm
def test_chat_happy_path(builtin_endpoints_host: Served) -> None:
    """A valid chat request returns 200 with a response payload and session id."""
    client, endpoints = builtin_endpoints_host
    chat = find_endpoint(endpoints, route_exact="agents/main/chat", method="POST")

    resp = client.post(chat.route, json={"prompt": "Say hello in one word."})

    expect_status(resp, 200)
    expect_header(resp, "x-ms-session-id")
    expect_json_keys(resp, ("session_id", "response"))


# --------------------------------------------------------------------------- #
# Multi-agent delegation (multi-agent-delegation app)
#
# These tests verify the subagents: front-matter field introduced in FRD 0007:
# a coordinator agent declares specialist subagents; each specialist is wired
# as a delegate_<slug> tool at chat time. A key provider-independent behavior
# is that an endpoint-less specialist (no trigger: field) does NOT appear in
# the admin API — it is only reachable through the coordinator's delegation.
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def multi_agent_delegation_host() -> Iterator[Served]:
    with _serve("multi-agent-delegation") as served:
        yield served


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_delegation_coordinator_is_registered(
    multi_agent_delegation_host: Served,
) -> None:
    """The coordinator is indexed as an HTTP trigger at its declared route."""
    _, endpoints = multi_agent_delegation_host

    coordinator = find_endpoint(endpoints, route_exact="delegate", method="POST")
    assert coordinator.auth_level == "anonymous"
    assert coordinator.methods == ("POST",)


def test_delegation_specialist_has_no_direct_endpoint(
    multi_agent_delegation_host: Served,
) -> None:
    """The endpoint-less specialist is NOT registered as a direct HTTP trigger.

    PR #102 (FRD 0007): agents listed only in ``subagents:`` and carrying no
    ``trigger:`` or ``builtin_endpoints:`` field skip Azure Functions trigger
    registration entirely. The specialist is only reachable through the
    coordinator's ``delegate_specialist`` tool — it must not appear in the
    admin API as an independent route.
    """
    _, endpoints = multi_agent_delegation_host

    specialist_routes = [ep for ep in endpoints if "specialist" in ep.route.lower()]
    assert not specialist_routes, (
        f"endpoint-less specialist must not appear in the admin API; "
        f"found: {[ep.route for ep in specialist_routes]}"
    )


# --------------------------------------------------------------------------- #
# Deterministic behavior (no LLM required)
# --------------------------------------------------------------------------- #


def test_delegation_coordinator_rejects_wrong_method(
    multi_agent_delegation_host: Served,
) -> None:
    """GET on the coordinator's POST-only route returns 404 (method not bound)."""
    client, endpoints = multi_agent_delegation_host

    ep = find_endpoint(endpoints, route_exact="delegate", method="POST")
    resp = client.get(ep.url(client.base_url))

    expect_status(resp, 404, 405)


# --------------------------------------------------------------------------- #
# Full-run assertion (requires LLM)
# --------------------------------------------------------------------------- #


@requires_llm
def test_delegation_coordinator_responds(
    multi_agent_delegation_host: Served,
) -> None:
    """The coordinator delegates to the specialist, which emits a deterministic marker.

    The specialist is instructed to always start its response with ``DELEGATION_OK:``.
    The coordinator is instructed to always delegate and pass through the specialist's
    response verbatim. Asserting on the marker confirms that delegation actually
    happened — a coordinator that answered directly would never produce the prefix.
    """
    client, endpoints = multi_agent_delegation_host

    ep = find_endpoint(endpoints, route_exact="delegate", method="POST")
    resp = client.post(
        ep.url(client.base_url),
        json={"prompt": "What is the difference between a process and a thread?"},
    )
    expect_status(resp, 200)
    expect_header(resp, "x-ms-session-id")
    assert "DELEGATION_OK:" in resp.text, (
        "expected the delegation marker 'DELEGATION_OK:' in the response — "
        "if absent the coordinator answered directly without delegating to the specialist. "
        f"Got: {resp.text!r}"
    )


# --------------------------------------------------------------------------- #
# web_request system tool (web-request app)
#
# These tests verify the built-in web_request system tool introduced in
# FRD 0005 (PR #96): a default-on outbound HTTP(S) tool with an always-on
# SSRF safety floor, per-agent opt-out, and an optional host allowlist.
#
# The app exercises two behaviors:
#   fetcher    — web_request enabled, allowlisted to example.com
#   opted_out  — web_request disabled via system_tools.web_request: false
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def web_request_host() -> Iterator[Served]:
    with _serve("web-request") as served:
        yield served


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_web_request_both_endpoints_are_registered(
    web_request_host: Served,
) -> None:
    """Both agents (fetcher and opted_out) register their HTTP routes."""
    _, endpoints = web_request_host

    fetch_ep = find_endpoint(endpoints, route_exact="fetch", method="POST")
    assert fetch_ep.auth_level == "anonymous"

    opted_ep = find_endpoint(endpoints, route_exact="no-fetch", method="POST")
    assert opted_ep.auth_level == "anonymous"


# --------------------------------------------------------------------------- #
# Full-run assertions (require LLM)
# --------------------------------------------------------------------------- #


@requires_llm
def test_web_request_fetcher_completes_outbound_request(
    web_request_host: Served,
) -> None:
    """The fetcher agent uses web_request to hit example.com and responds.

    Confirms the full tool path: LLM decides to call web_request → SSRF
    validation passes (example.com is in the allowlist) → HTTP GET to
    https://example.com → structured response returned to the LLM → agent
    produces a reply. Plain HTTP trigger agents return plain text (not the
    JSON envelope from builtin chat endpoints), so assertions are on status
    and session header only.
    """
    client, endpoints = web_request_host

    ep = find_endpoint(endpoints, route_exact="fetch", method="POST")
    resp = client.post(
        ep.url(client.base_url),
        json={"prompt": "Use web_request to fetch https://example.com and summarize the page."},
    )

    expect_status(resp, 200)
    expect_header(resp, "x-ms-session-id")
    expect_body_contains(resp, "Example Domain")


@requires_llm
def test_web_request_opted_out_agent_responds_without_tool(
    web_request_host: Served,
) -> None:
    """The opted-out agent handles a prompt without the web_request tool.

    Confirms the per-agent opt-out path (system_tools.web_request: false)
    does not break agent registration or response — the agent answers from
    its own knowledge rather than making an outbound call. The assertion is
    on response structure; the opted-out agent either answers directly or
    reports inability to fetch, both of which are valid. Plain HTTP trigger
    agents return plain text so assertions are on status and session header.
    """
    client, endpoints = web_request_host

    ep = find_endpoint(endpoints, route_exact="no-fetch", method="POST")
    resp = client.post(
        ep.url(client.base_url),
        json={
            "prompt": (
                "Try to use web_request to fetch https://example.com. If the tool is unavailable, "
                "reply with exactly 'WEB_REQUEST_DISABLED'."
            )
        },
    )

    expect_status(resp, 200)
    expect_header(resp, "x-ms-session-id")
    expect_body_contains(resp, "WEB_REQUEST_DISABLED")
