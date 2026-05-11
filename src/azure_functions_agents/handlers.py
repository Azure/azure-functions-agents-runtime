"""
Agent handler factories for triggered and HTTP-triggered agents.

This module produces the async handler callables that are passed to Azure
Functions trigger decorators. It deliberately has **no** dependency on
``app.py`` so it can be imported by both the translator and the app factory
without creating circular imports.

Dependency graph::

    app.py ─┬─> app_analyzer.py ─> translator.py ─> handlers.py
            └──────────────────────────────────────> handlers.py
"""

import json
import logging
import re
from typing import Any, Dict, Optional

from .runner import run_agent
from .sandbox import create_sandbox_tools


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

def build_sandbox_tools_for_session(
    sandbox_config: Optional[Dict[str, Any]],
    session_id: Optional[str],
) -> Optional[list]:
    """Build per-request sandbox tools using the resolved session id."""
    if not isinstance(sandbox_config, dict):
        return None
    fallback = session_id or "default"
    return create_sandbox_tools(sandbox_config, fallback_session_id=fallback)


def serialize_trigger_data(trigger_data) -> str:
    """Serialize trigger binding data to a JSON string."""
    if trigger_data is None:
        return "{}"
    if hasattr(trigger_data, "to_dict"):
        payload = trigger_data.to_dict()
    elif hasattr(trigger_data, "model_dump"):
        payload = trigger_data.model_dump()
    elif isinstance(trigger_data, dict):
        payload = trigger_data
    elif isinstance(trigger_data, str):
        return trigger_data
    else:
        payload = str(trigger_data)

    if isinstance(payload, dict):
        return json.dumps(payload, ensure_ascii=False, default=str)
    return str(payload)


def extract_json_from_response(text: str) -> str:
    """Extract JSON from an agent response, stripping markdown code fences if present."""
    stripped = text.strip()
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)```", stripped, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()
    return stripped


# ---------------------------------------------------------------------------
# Handler factories
# ---------------------------------------------------------------------------

def make_agent_handler(
    function_name: str,
    agent_name: str,
    trigger_type: str,
    should_log: bool,
    sandbox_config: Optional[Dict[str, Any]] = None,
    agent_instructions: Optional[str] = None,
):
    """Create an async handler function for a triggered agent."""

    async def _handler(trigger_data):
        logging.info(f"Agent '{function_name}' triggered")

        try:
            data_json = serialize_trigger_data(trigger_data)
            parts = []
            if agent_instructions:
                parts.append(agent_instructions)
            parts.append(
                f"Triggered by: {trigger_type}\n\nTrigger data:\n```json\n{data_json}\n```"
            )
            prompt = "\n\n".join(parts)

            sandbox_tools = build_sandbox_tools_for_session(sandbox_config, None)

            result = await run_agent(
                prompt,
                instructions=agent_instructions,
                sandbox_tools=sandbox_tools,
            )

            if should_log:
                logging.info(
                    "Agent '%s' response: %s",
                    function_name,
                    json.dumps(
                        {
                            "session_id": result.session_id,
                            "response": result.content,
                            "tool_calls": result.tool_calls,
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                )
        except Exception as exc:
            logging.exception(f"Agent '{function_name}' failed: {exc}")

    _handler.__name__ = f"handler_{function_name}"
    return _handler


def make_http_agent_handler(
    function_name: str,
    agent_name: str,
    should_log: bool,
    sandbox_config: Optional[Dict[str, Any]] = None,
    agent_instructions: Optional[str] = None,
    response_example: Optional[str] = None,
    response_schema: Optional[dict] = None,
):
    """Create an async handler for an HTTP-triggered agent that returns structured JSON."""
    from azurefunctions.extensions.http.fastapi import Request, Response

    async def _handler(req: Request) -> Response:
        logging.info(f"HTTP agent '{function_name}' triggered")

        try:
            # Parse request body
            try:
                body = await req.json()
                body_json = json.dumps(body, ensure_ascii=False, default=str)
            except Exception:
                body_bytes = await req.body()
                body_json = (
                    body_bytes.decode("utf-8", errors="replace") if body_bytes else "{}"
                )

            # Build prompt
            parts = []
            if agent_instructions:
                parts.append(agent_instructions)

            # Add response format instructions
            if response_example:
                parts.append(
                    "You MUST respond with ONLY a valid JSON object "
                    "(no markdown, no explanation, no code fences). "
                    f"Your response must match this example format:\n```json\n{response_example}\n```"
                )
            elif response_schema:
                schema_str = json.dumps(response_schema, indent=2)
                parts.append(
                    "You MUST respond with ONLY a valid JSON object "
                    "(no markdown, no explanation, no code fences). "
                    f"Your response must conform to this JSON Schema:\n```json\n{schema_str}\n```"
                )

            parts.append(f"HTTP request data:\n```json\n{body_json}\n```")
            prompt = "\n\n".join(parts)

            sandbox_tools = build_sandbox_tools_for_session(sandbox_config, None)

            result = await run_agent(
                prompt,
                instructions=agent_instructions,
                sandbox_tools=sandbox_tools,
            )

            if should_log:
                logging.info(
                    "HTTP agent '%s' response: %s",
                    function_name,
                    json.dumps(
                        {
                            "session_id": result.session_id,
                            "response": result.content[:500],
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                )

            # If a response format was specified, parse as JSON
            if response_example or response_schema:
                extracted = extract_json_from_response(result.content)
                try:
                    parsed = json.loads(extracted)
                    return Response(
                        content=json.dumps(parsed, ensure_ascii=False),
                        status_code=200,
                        media_type="application/json",
                    )
                except json.JSONDecodeError as je:
                    logging.warning(
                        f"HTTP agent '{function_name}' returned invalid JSON: {je}"
                    )
                    return Response(
                        content=json.dumps(
                            {
                                "error": "Agent returned invalid JSON",
                                "raw_response": result.content,
                            }
                        ),
                        status_code=500,
                        media_type="application/json",
                    )
            else:
                return Response(
                    content=result.content,
                    status_code=200,
                    media_type="text/plain",
                )

        except Exception as exc:
            logging.exception(f"HTTP agent '{function_name}' failed: {exc}")
            return Response(
                content=json.dumps({"error": str(exc)}),
                status_code=500,
                media_type="application/json",
            )

    _handler.__name__ = f"handler_{function_name}"
    return _handler
