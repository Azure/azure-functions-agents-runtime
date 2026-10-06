"""Shared structured-response instructions and validation."""

from __future__ import annotations

import json
import re
from typing import Any

import jsonschema

from .config import ResolvedAgent


class HostedSkillResponseError(ValueError):
    """Raised when an agent response violates its configured response contract."""


class InvalidResponseJsonError(HostedSkillResponseError):
    """Raised when a structured agent response is not valid JSON."""


class ResponseSchemaValidationError(HostedSkillResponseError):
    """Raised when agent response JSON does not match its configured schema."""

    def __init__(self, details: str) -> None:
        super().__init__("Agent response validation failed")
        self.details = details


def response_format_instructions(resolved: ResolvedAgent) -> list[str]:
    """Build response-format instructions for a resolved agent."""
    if resolved.response_example:
        return [
            "You MUST respond with ONLY a valid JSON object "
            "(no markdown, no explanation, no code fences). "
            "Your response must match this example format:\n"
            f"```json\n{resolved.response_example}\n```"
        ]
    if resolved.response_schema:
        schema = json.dumps(resolved.response_schema, indent=2)
        return [
            "You MUST respond with ONLY a valid JSON object "
            "(no markdown, no explanation, no code fences). "
            "Your response must conform to this JSON Schema:\n"
            f"```json\n{schema}\n```"
        ]
    return []


def extract_json_from_response(text: str) -> str:
    """Extract JSON from an agent response, stripping code fences when present."""
    stripped = text.strip()
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)```", stripped, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()
    return stripped


def validate_response_contract(
    content: str,
    response_schema: dict[str, Any] | None,
) -> Any:
    """Parse response JSON and enforce an optional JSON Schema."""
    try:
        parsed = json.loads(extract_json_from_response(content))
    except json.JSONDecodeError as exc:
        raise InvalidResponseJsonError("Agent returned invalid JSON") from exc

    if response_schema is not None:
        try:
            jsonschema.validate(instance=parsed, schema=response_schema)
        except jsonschema.ValidationError as exc:
            raise ResponseSchemaValidationError(exc.message) from exc
    return parsed