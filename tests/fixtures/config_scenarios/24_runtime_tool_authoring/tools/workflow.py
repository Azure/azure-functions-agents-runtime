from azure_functions_agents import workflow_tool


@workflow_tool
def workflow_only(args: dict[str, object]) -> dict[str, object]:
    return args


@workflow_tool
def start_workflow(args: dict[str, object]) -> dict[str, object]:
    return args
