"""Let an agent suggest corrected inputs or remediation; retry an operation once."""

import asyncio
import json
import logging
from collections.abc import Callable

from azurefunctions.extensions.http.fastapi import JSONResponse
from starlette.datastructures import QueryParams

from azure_functions_agents import HostedSkill

FALLBACK_ADVICE = "Review the application's logs and configuration with its operator before retrying."
ADVICE_TIMEOUT_SECONDS = 60


async def recover_operation(
    skill: HostedSkill,
    error: Exception,
    query: QueryParams,
    *,
    operation: Callable[[QueryParams], dict[str, str]],
    context: str,
) -> JSONResponse:
    """Ask the agent for corrected inputs, retry once, or return human remediation guidance."""
    advice = FALLBACK_ADVICE
    try:
        # Ask once, with a deadline; omit the raw exception message and traceback.
        async with asyncio.timeout(ADVICE_TIMEOUT_SECONDS):
            result = await skill.run(json.dumps({
                "exception_type": type(error).__name__,
                "context": context,
                "inputs": list(query.multi_items()),
            }))
        decision = json.loads(result.content)
        if decision["corrected_input"] is not None:
            # The application—not the agent—retries the operation, at most once.
            output = operation(QueryParams(decision["corrected_input"]))
            return JSONResponse({
                **output, "auto_corrected": True, "requires_user_action": False,
                "corrected_input": decision["corrected_input"],
            })
        advice = decision["remediation"] or FALLBACK_ADVICE
    except Exception:
        # Agent failures, invalid responses, and failed retries all use static guidance.
        logging.exception("Recovery failed; returning fallback guidance")

    return JSONResponse({
        "error": "OPERATION_FAILED", "auto_corrected": False,
        "requires_user_action": True, "remediation": advice,
    }, status_code=500)