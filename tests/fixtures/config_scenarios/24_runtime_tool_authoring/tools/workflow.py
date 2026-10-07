from azure_functions_agents import workflow_tool


@workflow_tool
def workflow_only(args: dict[str, object]) -> dict[str, object]:
    return args
