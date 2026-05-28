"""Static E2E expectations for the sample function apps.

This module is the authoritative invocation matrix for the sample E2E harness.
The harness will cross-check each sample's ``expected_function_names`` against
``azure_functions_agents.create_function_app(sample_path).get_functions()``
before launching ``func``.

Invocation request bodies may contain placeholder values of the form
``"${VAR_NAME}"``. The harness must expand those placeholders from the
current process environment at invocation time before sending the request.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

type InvocationKind = Literal["http", "admin_function", "mcp_webhook", "skip"]


@dataclass(frozen=True)
class Invocation:
    kind: InvocationKind
    function_name: str
    method: str = "POST"
    path: str = ""
    body: dict[str, Any] | None = None
    headers: dict[str, str] | None = None
    expected_status: tuple[int, ...] = (200,)
    requires_log_completion: bool = False
    is_sse: bool = False
    description: str = ""


@dataclass(frozen=True)
class SampleExpectations:
    name: str
    sample_path: str
    expected_function_names: frozenset[str]
    invocations: tuple[Invocation, ...]
    required_env_vars: tuple[str, ...]
    skip_invocation_function_names: frozenset[str] = frozenset()


_JSON_HEADERS = {"Content-Type": "application/json"}
_SSE_HEADERS = {"Accept": "text/event-stream", "Content-Type": "application/json"}
_MCP_TOOLS_LIST = {
    "jsonrpc": "2.0",
    "id": "1",
    "method": "tools/list",
    "params": {},
}


SAMPLES: tuple[SampleExpectations, ...] = (
    SampleExpectations(
        name="basic-chat",
        sample_path="samples/basic-chat/src",
        expected_function_names=frozenset(
            {
                "main_debug_chat_page",
                "main_debug_chat",
                "main_debug_chatstream",
                "main_debug_mcp",
            }
        ),
        invocations=(
            Invocation(
                kind="http",
                function_name="main_debug_chat_page",
                method="GET",
                path="/",
                expected_status=(200,),
                description="Debug chat landing page.",
            ),
            Invocation(
                kind="http",
                function_name="main_debug_chat",
                path="/agent/chat",
                body={"prompt": "Say hello in one sentence."},
                headers=_JSON_HEADERS,
                expected_status=(200,),
                description="JSON chat response from the main debug endpoint.",
            ),
            Invocation(
                kind="http",
                function_name="main_debug_chatstream",
                path="/agent/chatstream",
                body={"prompt": "Tell me three quick facts about Azure Functions."},
                headers=_SSE_HEADERS,
                expected_status=(200,),
                is_sse=True,
                description="SSE chat stream from the main debug endpoint.",
            ),
            Invocation(
                kind="mcp_webhook",
                function_name="main_debug_mcp",
                path="/runtime/webhooks/mcp",
                body=_MCP_TOOLS_LIST,
                headers=_JSON_HEADERS,
                expected_status=(200,),
                description="MCP tools/list against the main debug webhook.",
            ),
        ),
        required_env_vars=(
            "AZURE_TENANT_ID",
            "AZURE_CLIENT_ID",
            "AZURE_CLIENT_SECRET",
            "FOUNDRY_PROJECT_ENDPOINT",
            "FOUNDRY_MODEL",
            "ACA_SESSION_POOL_ENDPOINT",
        ),
    ),
    SampleExpectations(
        name="daily-azure-report",
        sample_path="samples/daily-azure-report/src",
        expected_function_names=frozenset(
            {
                "main_debug_chat_page",
                "main_debug_chat",
                "main_debug_chatstream",
                "main_debug_mcp",
                "daily_azure_report",
                "resource_summary",
            }
        ),
        invocations=(
            Invocation(
                kind="http",
                function_name="main_debug_chat_page",
                method="GET",
                path="/",
                expected_status=(200,),
                description="Debug chat landing page.",
            ),
            Invocation(
                kind="http",
                function_name="main_debug_chat",
                path="/agent/chat",
                body={"prompt": "Summarize the daily Azure report agent in one sentence."},
                headers=_JSON_HEADERS,
                expected_status=(200,),
                description="JSON chat response from the main debug endpoint.",
            ),
            Invocation(
                kind="http",
                function_name="main_debug_chatstream",
                path="/agent/chatstream",
                body={"prompt": "List three quick Azure reporting tasks you can help with."},
                headers=_SSE_HEADERS,
                expected_status=(200,),
                is_sse=True,
                description="SSE chat stream from the main debug endpoint.",
            ),
            Invocation(
                kind="mcp_webhook",
                function_name="main_debug_mcp",
                path="/runtime/webhooks/mcp",
                body=_MCP_TOOLS_LIST,
                headers=_JSON_HEADERS,
                expected_status=(200,),
                description="MCP tools/list against the main debug webhook.",
            ),
            Invocation(
                kind="admin_function",
                function_name="daily_azure_report",
                path="/admin/functions/daily_azure_report",
                body={"input": ""},
                headers=_JSON_HEADERS,
                expected_status=(202,),
                requires_log_completion=True,
                description="Timer-triggered daily Azure report invocation.",
            ),
            Invocation(
                kind="http",
                function_name="resource_summary",
                path="/resource-summary",
                body={"subscription_id": "${SUBSCRIPTION_ID}"},
                headers=_JSON_HEADERS,
                expected_status=(200,),
                description="HTTP resource summary invocation.",
            ),
        ),
        required_env_vars=(
            "AZURE_TENANT_ID",
            "AZURE_CLIENT_ID",
            "AZURE_CLIENT_SECRET",
            "FOUNDRY_PROJECT_ENDPOINT",
            "FOUNDRY_MODEL",
            "SUBSCRIPTION_ID",
            "O365_MCP_SERVER_URL",
            "O365_MCP_CLIENT_ID",
            "TO_EMAIL",
            "MAF_REASONING_EFFORT",
            "MAF_REASONING_SUMMARY",
        ),
    ),
    SampleExpectations(
        name="daily-tech-news-email",
        sample_path="samples/daily-tech-news-email/src",
        expected_function_names=frozenset({"daily_tech_news"}),
        invocations=(
            Invocation(
                kind="admin_function",
                function_name="daily_tech_news",
                path="/admin/functions/daily_tech_news",
                body={"input": ""},
                headers=_JSON_HEADERS,
                expected_status=(202,),
                requires_log_completion=True,
                description="Timer-triggered daily tech news email invocation.",
            ),
        ),
        required_env_vars=(
            "AZURE_TENANT_ID",
            "AZURE_CLIENT_ID",
            "AZURE_CLIENT_SECRET",
            "FOUNDRY_PROJECT_ENDPOINT",
            "FOUNDRY_MODEL",
            "ACA_SESSION_POOL_ENDPOINT",
            "O365_MCP_SERVER_URL",
            "O365_MCP_CLIENT_ID",
            "TO_EMAIL",
            "MAF_REASONING_EFFORT",
            "MAF_REASONING_SUMMARY",
        ),
    ),
    SampleExpectations(
        name="outlook-reply-agent",
        sample_path="samples/outlook-reply-agent/src",
        expected_function_names=frozenset({"OnNewEmail"}),
        invocations=(),
        required_env_vars=(
            "AZURE_TENANT_ID",
            "AZURE_CLIENT_ID",
            "AZURE_CLIENT_SECRET",
            "FOUNDRY_PROJECT_ENDPOINT",
            "FOUNDRY_MODEL",
            "ACA_SESSION_POOL_ENDPOINT",
            "O365_MCP_SERVER_URL",
            "O365_MCP_CLIENT_ID",
            "WATCHED_SENDER_EMAIL",
        ),
        skip_invocation_function_names=frozenset({"OnNewEmail"}),
    ),
)

_SAMPLES_BY_NAME = {sample.name: sample for sample in SAMPLES}


def for_sample(name: str) -> SampleExpectations:
    try:
        return _SAMPLES_BY_NAME[name]
    except KeyError as exc:
        known_samples = ", ".join(sorted(_SAMPLES_BY_NAME))
        raise KeyError(f"Unknown sample {name!r}. Expected one of: {known_samples}") from exc


def list_samples() -> tuple[SampleExpectations, ...]:
    return SAMPLES
