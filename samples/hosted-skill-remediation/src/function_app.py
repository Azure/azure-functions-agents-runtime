"""Queue handlers with opt-in whole-function smart retry."""

import azure.functions as func
from queue_work_item import process_work_item_body, CustomError

from azure_functions_agents import HostedSkillFunctionApp
from azure_functions_agents._logger import logger

app = HostedSkillFunctionApp()


@app.queue_trigger(arg_name="message",
                   queue_name="recovery-work-items",
                   connection="AzureWebJobsStorage")
@app.smart_retry(max_retries=3)
async def process_work_item(message: func.QueueMessage) -> None:
    logger.info("Processing queue work item: body=%s", message.get_body())
    try:
        # Expect JSON input and compute the sum
        result = process_work_item_body(message.get_body())
        logger.info("Queue work item processed: total=%s", result)
    except CustomError as e:
        # Known error - apply deterministic handling
        logger.error("Failed to process queue work item: %s", e)


@app.queue_trigger(arg_name="message",
                   queue_name="bad-package",
                   connection="AzureWebJobsStorage")
@app.smart_retry(audit_only=True)
async def module_not_found(message: func.QueueMessage) -> None:
    logger.info("Processing queue work item: body=%s", message.get_body())

    import queueprocessingpackage
    result = queueprocessingpackage.process_message(message.get_body())
    logger.info("Queue work item processed: total=%s", result)
