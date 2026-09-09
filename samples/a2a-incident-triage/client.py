from __future__ import annotations

import argparse
import json
import uuid
from typing import Any

import httpx


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch the incident-triage Agent Card and send one A2A 1.0 Message."
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
    return parser


def _headers(function_key: str | None) -> dict[str, str]:
    headers = {"A2A-Version": "1.0"}
    if function_key:
        headers["x-functions-key"] = function_key
    return headers


def main() -> None:
    args = _parser().parse_args()
    base_url = args.base_url.rstrip("/")
    card_url = f"{base_url}/agents/main/.well-known/agent-card.json"
    request_id = f"review-{uuid.uuid4().hex[:8]}"
    headers = _headers(args.function_key)

    with httpx.Client(timeout=120) as client:
        card_response = client.get(card_url, headers=headers)
        card_response.raise_for_status()
        card: dict[str, Any] = card_response.json()
        interface = next(
            item
            for item in card["supportedInterfaces"]
            if item["protocolBinding"] == "JSONRPC"
            and item["protocolVersion"] == "1.0"
        )

        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "SendMessage",
            "params": {
                "message": {
                    "messageId": uuid.uuid4().hex,
                    "contextId": args.context_id,
                    "role": "ROLE_USER",
                    "parts": [{"text": args.prompt}],
                },
                "configuration": {
                    "acceptedOutputModes": ["text/plain"],
                    "returnImmediately": False,
                },
            },
        }
        rpc_response = client.post(interface["url"], headers=headers, json=payload)
        rpc_response.raise_for_status()
        envelope: dict[str, Any] = rpc_response.json()

    print("Agent Card:")
    print(json.dumps(card, indent=2))
    print("\nJSON-RPC response:")
    print(json.dumps(envelope, indent=2))
    if envelope.get("id") != request_id:
        raise RuntimeError("The JSON-RPC response did not preserve the request id.")
    if "error" in envelope:
        raise RuntimeError(f"A2A request failed: {envelope['error']}")

    message = envelope["result"]["message"]
    print(f"\nIncident brief ({message['contextId']}):")
    print(" ".join(part["text"] for part in message["parts"]))


if __name__ == "__main__":
    main()
