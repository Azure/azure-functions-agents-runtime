from agent_framework import FunctionTool, tool


@tool
def raw_decorated(value: str) -> str:
    return value


raw_constructed = FunctionTool(name="raw_constructed", func=lambda: "ignored")
