"""Private handler factories for trigger registration."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from importlib import import_module
from typing import Any, cast

import azure.functions as func
import jsonschema
from azurefunctions.extensions.http.fastapi import Request, Response

from .._logger import logger
from ..config import ResolvedAgent, _to_bool
from .capabilities import AgentCapabilities

AUTH_LEVEL_MAP = {
    "anonymous": func.AuthLevel.ANONYMOUS,
    "function": func.AuthLevel.FUNCTION,
    "admin": func.AuthLevel.ADMIN,
}


def serialize_trigger_data(trigger_data: Any) -> str:
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


def normalize_timer_schedule(schedule: str) -> str:
    """Accept 5-part cron by prepending seconds; keep 6-part schedules unchanged."""
    schedule_parts = schedule.strip().split()
    if len(schedule_parts) == 5:
        return f"0 {schedule.strip()}"
    return schedule.strip()


def build_sandbox_tools_for_session(
    resolved: ResolvedAgent, session_id: str | None
) -> list[Any] | None:
    """Build per-request sandbox tools using the resolved session id."""
    if resolved.tools_disabled:
        return None
    if resolved.sandbox_config is None:
        return None
    fallback = session_id or "default"
    sandbox_module = import_module("azure_functions_agents.system_tools.sandbox")
    create_sandbox_tools = sandbox_module.create_sandbox_tools
    return cast(
        list[Any],
        create_sandbox_tools(
            resolved.sandbox_config.model_dump(),
            fallback_session_id=fallback,
        ),
    )


def validate_request_body(body: Any, input_schema: dict[str, Any] | None) -> Response | None:
    """Validate body against JSON Schema, returning an HTTP error response on failure."""
    if input_schema is None:
        return None

    try:
        jsonschema.validate(instance=body, schema=input_schema)
    except jsonschema.ValidationError as exc:
        return Response(
            content=json.dumps(
                {
                    "error": "Input validation failed",
                    "details": exc.message,
                }
            ),
            status_code=400,
            media_type="application/json",
        )
    except jsonschema.SchemaError as exc:
        return Response(
            content=json.dumps(
                {
                    "error": "Invalid input schema",
                    "details": exc.message,
                }
            ),
            status_code=500,
            media_type="application/json",
        )

    return None


def _should_log(resolved: ResolvedAgent) -> bool:
    return _to_bool(resolved.metadata.get("logger", True), default=True)


def _response_format_instructions(resolved: ResolvedAgent) -> list[str]:
    if resolved.response_example:
        return [
            "You MUST respond with ONLY a valid JSON object "
            "(no markdown, no explanation, no code fences). "
            "Your response must match this example format:\n"
            f"```json\n{resolved.response_example}\n```"
        ]
    if resolved.response_schema:
        schema_str = json.dumps(resolved.response_schema, indent=2)
        return [
            "You MUST respond with ONLY a valid JSON object "
            "(no markdown, no explanation, no code fences). "
            "Your response must conform to this JSON Schema:\n"
            f"```json\n{schema_str}\n```"
        ]
    return []


async def _run_agent(*args: Any, **kwargs: Any) -> Any:
    runner_module = import_module("azure_functions_agents.runner")
    return await runner_module.run_agent(*args, **kwargs)


def _request_header_value(req: Request, header_name: str) -> str | None:
    headers = getattr(req, "headers", None)
    if headers is None:
        return None

    value = headers.get(header_name) if hasattr(headers, "get") else None
    if isinstance(value, str) and value.strip():
        return value.strip()

    if hasattr(headers, "items"):
        for key, item in headers.items():
            if key.lower() == header_name.lower() and isinstance(item, str) and item.strip():
                return item.strip()

    return None


def _new_session_id() -> str:
    return uuid.uuid4().hex


def make_agent_handler(
    resolved: ResolvedAgent,
    trigger_type: str,
    capabilities: AgentCapabilities,
) -> Callable[..., Any]:
    """Create an async handler function for a non-HTTP triggered agent."""

    # NOTE: deliberately omit a type annotation on `trigger_data`. The Azure
    # Functions Python worker validates annotations against the binding's
    # expected type (e.g. ``func.TimerRequest``) and rejects ``Any``. Leaving
    # the parameter unannotated tells the worker to skip that type check, so
    # this single handler can be reused across all non-HTTP trigger types.
    async def _handler(trigger_data) -> None:
        logger.info("Agent '%s' triggered", resolved.name)

        try:
            session_id = _new_session_id()
            data_json = serialize_trigger_data(trigger_data)
            parts: list[str] = [
                f"Triggered by: {trigger_type}\n\nTrigger data:\n```json\n{data_json}\n```"
            ]
            prompt = "\n\n".join(parts)

            result = await _run_agent(
                prompt,
                instructions=resolved.instructions,
                timeout=resolved.timeout,
                model=resolved.model,
                session_id=session_id,
                sandbox_tools=build_sandbox_tools_for_session(resolved, session_id),
                tools=capabilities.filtered_user_tools,
                mcp_tools=capabilities.filtered_mcp_tools,
                skills_text=capabilities.skills_text,
                use_connector_tools=capabilities.use_connector_tools,
            )

            if _should_log(resolved):
                logger.info(
                    "Agent '%s' response: %s",
                    resolved.name,
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
            logger.exception("Agent '%s' failed: %s", resolved.name, exc)
            raise

    _handler.__name__ = f"handler_{re.sub(r'[^a-zA-Z0-9_]', '_', resolved.name)}"
    return _handler


def make_http_agent_handler(
    resolved: ResolvedAgent,
    capabilities: AgentCapabilities,
) -> Callable[[Request], Any]:
    """Create an async handler for an HTTP-triggered agent."""

    async def _handler(req: Request) -> Response:
        logger.info("HTTP agent '%s' triggered", resolved.name)

        try:
            session_id = _request_header_value(req, "x-ms-session-id") or _new_session_id()
            try:
                body = await req.json()
                body_json = json.dumps(body, ensure_ascii=False, default=str)
            except Exception:
                body_bytes = await req.body()
                body = body_bytes.decode("utf-8", errors="replace") if body_bytes else {}
                body_json = body if isinstance(body, str) else json.dumps(body)

            validation_error = validate_request_body(body, resolved.input_schema)
            if validation_error is not None:
                if validation_error.status_code == 500:
                    logger.error(
                        "HTTP agent '%s' has invalid input schema: %s",
                        resolved.name,
                        validation_error.body.decode("utf-8"),
                    )
                validation_error.headers["x-ms-session-id"] = session_id
                return validation_error

            parts: list[str] = []
            parts.extend(_response_format_instructions(resolved))
            parts.append(f"HTTP request data:\n```json\n{body_json}\n```")
            prompt = "\n\n".join(parts)

            result = await _run_agent(
                prompt,
                instructions=resolved.instructions,
                timeout=resolved.timeout,
                model=resolved.model,
                session_id=session_id,
                sandbox_tools=build_sandbox_tools_for_session(resolved, session_id),
                tools=capabilities.filtered_user_tools,
                mcp_tools=capabilities.filtered_mcp_tools,
                skills_text=capabilities.skills_text,
                use_connector_tools=capabilities.use_connector_tools,
            )

            if _should_log(resolved):
                logger.info(
                    "HTTP agent '%s' response: %s",
                    resolved.name,
                    json.dumps(
                        {
                            "session_id": result.session_id,
                            "response": result.content[:500],
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                )

            if resolved.response_example or resolved.response_schema:
                extracted = extract_json_from_response(result.content)
                try:
                    parsed = json.loads(extracted)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "HTTP agent '%s' returned invalid JSON: %s",
                        resolved.name,
                        exc,
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
                        headers={"x-ms-session-id": session_id},
                    )
                if resolved.response_schema:
                    try:
                        jsonschema.validate(
                            instance=parsed,
                            schema=resolved.response_schema,
                        )
                    except jsonschema.ValidationError as exc:
                        logger.warning(
                            "HTTP agent '%s' returned JSON that failed schema validation: %s",
                            resolved.name,
                            exc,
                        )
                        return Response(
                            content=json.dumps(
                                {
                                    "error": "Agent response validation failed",
                                    "details": exc.message,
                                }
                            ),
                            status_code=500,
                            media_type="application/json",
                            headers={"x-ms-session-id": session_id},
                        )
                return Response(
                    content=json.dumps(parsed, ensure_ascii=False),
                    status_code=200,
                    media_type="application/json",
                    headers={"x-ms-session-id": session_id},
                )

            return Response(
                content=result.content,
                status_code=200,
                media_type="text/plain",
                headers={"x-ms-session-id": session_id},
            )
        except Exception as exc:
            logger.exception("HTTP agent '%s' failed: %s", resolved.name, exc)
            return Response(
                content=json.dumps({"error": str(exc)}),
                status_code=500,
                media_type="application/json",
                headers={"x-ms-session-id": session_id},
            )

    _handler.__name__ = f"handler_{re.sub(r'[^a-zA-Z0-9_]', '_', resolved.name)}"
    return _handler
