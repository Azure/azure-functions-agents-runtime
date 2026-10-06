from azurefunctions.extensions.http.fastapi import JSONResponse, Request

from azure_functions_agents import HostedSkill, create_function_app

app = create_function_app()


@app.route(route="summarize", methods=["POST"])
@app.hosted_skill(arg_name="skill", agent_name="summarizer")
async def summarize(req: Request, skill: HostedSkill) -> JSONResponse:
    try:
        body = await req.json()
        prompt = body.get("prompt")
        session_id = body.get("session_id")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError
    except (AttributeError, ValueError):
        return JSONResponse(
            {"error": "Request JSON must include a nonblank prompt"},
            status_code=400,
        )

    result = await skill.run(prompt, session_id=session_id)
    return JSONResponse(
        {"session_id": result.session_id, "summary": result.content},
    )