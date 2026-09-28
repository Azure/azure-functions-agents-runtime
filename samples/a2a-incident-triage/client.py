from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping
from typing import Any

import httpx
from a2a.client import A2ACardResolver
from agent_framework import AgentSession
from agent_framework.a2a import A2AAgent, A2AServiceSessionId
from google.protobuf.json_format import MessageToDict

CARD_PATH = "/agents/main/.well-known/agent-card.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Call the incident-triage A2A server through Microsoft Agent Framework."
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        default=(
            "checkout-api latency doubled after deployment deploy-1842. "
            "The error rate is steady, but CPU rose from 45% to 92%."
        ),
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:7071",
        help="Functions host origin (default: %(default)s)",
    )
    parser.add_argument(
        "--context-id",
        default="incident-checkout-1842",
        help="Conversation context to continue (default: %(default)s)",
    )
    parser.add_argument(
        "--function-key",
        help="Optional x-functions-key value when http_auth.mode is function.",
    )
    parser.add_argument(
        "--json-output",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def _headers(function_key: str | None) -> dict[str, str]:
    headers = {"A2A-Version": "1.0"}
    if function_key:
        headers["x-functions-key"] = function_key
    return headers


def _select_jsonrpc_1_0(card: dict[str, Any]) -> dict[str, Any]:
    try:
        return next(
            item
            for item in card["supportedInterfaces"]
            if item["protocolBinding"] == "JSONRPC"
            and item["protocolVersion"] == "1.0"
        )
    except (KeyError, StopIteration) as exc:
        raise RuntimeError("The Agent Card does not advertise a JSONRPC 1.0 interface.") from exc


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    base_url = args.base_url.rstrip("/")
    card_url = f"{base_url}{CARD_PATH}"
    headers = _headers(args.function_key)

    async with httpx.AsyncClient(timeout=120, headers=headers) as http_client:
        resolver = A2ACardResolver(
            http_client,
            base_url,
            agent_card_path=CARD_PATH,
        )
        agent_card = await resolver.get_agent_card()
        card = MessageToDict(agent_card)
        interface = _select_jsonrpc_1_0(card)

        agent = A2AAgent(
            agent_card=agent_card,
            http_client=http_client,
            supported_protocol_bindings=["JSONRPC"],
        )
        session = AgentSession(
            service_session_id=A2AServiceSessionId(
                context_id=args.context_id,
                task_id=None,
                task_state=None,
            )
        )
        response = await agent.run(args.prompt, session=session)

    service_session_id = session.service_session_id
    response_context_id = (
        service_session_id.get("context_id")
        if isinstance(service_session_id, Mapping)
        else None
    )
    if response_context_id != args.context_id:
        raise RuntimeError(
            f"The A2A response changed contextId from {args.context_id!r} "
            f"to {response_context_id!r}."
        )

    return {
        "cardUrl": card_url,
        "interface": interface,
        "contextId": response_context_id,
        "responseText": response.text,
    }


def main() -> None:
    args = _parser().parse_args()
    result = asyncio.run(_run(args))
    if args.json_output:
        print(json.dumps(result))
        return

    print(f"Agent Card: {result['cardUrl']}")
    print(
        "Selected interface: "
        f"{result['interface']['protocolBinding']} "
        f"{result['interface']['protocolVersion']} "
        f"{result['interface']['url']}"
    )
    print(f"\nIncident brief ({result['contextId']}):")
    print(result["responseText"])


if __name__ == "__main__":
    main()
