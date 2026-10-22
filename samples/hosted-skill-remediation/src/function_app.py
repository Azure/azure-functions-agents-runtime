"""Ordinary HTTP and queue handlers with opt-in whole-function smart retry."""

import azure.functions as func
from azurefunctions.extensions.http.fastapi import JSONResponse, Request
from queue_work_item import process_work_item_body

from azure_functions_agents import HostedSkillFunctionApp
from azure_functions_agents._logger import logger

app = HostedSkillFunctionApp()


@app.route(route="add/{a}/{b}", methods=["GET"])
@app.smart_retry(agent_name="error_advisor")
async def add_numbers(req: Request) -> JSONResponse:
    """Add the route parameters; let the decorator recover from input errors."""
    a = float(req.path_params["a"])
    b = float(req.path_params["b"])
    return JSONResponse({"sum": a + b})


@app.queue_trigger(arg_name="message",
                   queue_name="recovery-work-items",
                   connection="AzureWebJobsStorage")
@app.smart_retry(agent_name="queue_advisor",
                 max_retries=3)
async def process_work_item(message: func.QueueMessage) -> None:
    result = process_work_item_body(message.get_body())
    logger.info("Queue work item processed: total=%s", result)