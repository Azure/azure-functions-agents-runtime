"""Synthetic release evidence with controlled completion order."""

import asyncio
from typing import Any

from azure_functions_agents import workflow_tool
from azure_functions_agents.workflows.schema import WorkflowTerminalError


@workflow_tool
def list_release_checks(args: dict[str, Any]) -> dict[str, Any]:
    """Return three release checks in report order."""
    return {
        "checks": [
            {"name": "tests", "delay_seconds": 6},
            {"name": "security", "delay_seconds": 2},
            {"name": "deployment", "delay_seconds": 4},
        ]
    }


@workflow_tool
async def check_release(args: dict[str, Any]) -> dict[str, Any]:
    """Return synthetic evidence, or report a controlled failed check."""
    check = args["check"]
    delay = args.get("delay_seconds", 2)
    if not isinstance(delay, (int, float)) or not 0 <= delay <= 30:
        raise ValueError("delay_seconds must be between 0 and 30")
    await asyncio.sleep(delay)
    if args.get("fail"):
        raise WorkflowTerminalError("release_check_failed", f"Release check {check} failed.")
    return {"release": "REL-4821", "check": check, "passed": True}


@workflow_tool
def summarize_release(args: dict[str, Any]) -> dict[str, Any]:
    """Combine release evidence without another model call."""
    return {"marker": "RELEASE_REVIEW_READY", "evidence": args["evidence"]}
