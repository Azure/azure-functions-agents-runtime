"""Bounded workflow Activities for the timer-trigger sample."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from azure_functions_agents import workflow_tool

logger = logging.getLogger(__name__)


@workflow_tool(description="Capture the timer payload and timestamp for the workflow.")
def capture_timer_event(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "event": args.get("event", {}),
    }


@workflow_tool(
    description="Publish the timer workflow result to the sample's structured log sink."
)
def publish_timer_result(args: dict[str, Any]) -> dict[str, Any]:
    result = {
        "event": "timer_workflow_completed",
        "capture": args.get("capture"),
        "pause": args.get("pause"),
    }
    logger.warning("TIMER_WORKFLOW_COMPLETED %s", json.dumps(result, default=str))
    return result
