from azure_functions_agents import tool, workflow_tool


@workflow_tool
@tool
def authored(value: str) -> str:
    return value


def z_fallback() -> str:
    return "must not replace decorated"
