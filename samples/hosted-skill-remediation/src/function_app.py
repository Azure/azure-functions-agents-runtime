import logging

from azurefunctions.extensions.http.fastapi import JSONResponse, Request
from hosted_report import CONTEXT, render_report
from report_recovery import recover_operation

from azure_functions_agents import HostedSkill, HostedSkillFunctionApp

app = HostedSkillFunctionApp()


@app.route(route="reports/weekly", methods=["GET"])
@app.hosted_skill(arg_name="skill", agent_name="error_advisor")
async def weekly_report(req: Request, skill: HostedSkill) -> JSONResponse:
    try:
        return JSONResponse({
            **render_report(req.query_params),
            "auto_corrected": False,
            "requires_user_action": False,
        })
    except Exception as exc:
        logging.exception("Weekly report failed")
        return await recover_operation(
            skill, exc, req.query_params, operation=render_report, context=CONTEXT
        )