from copilot.tools import Tool, ToolInvocation, ToolResult


def _lookup(invocation: ToolInvocation) -> ToolResult:
    raise AssertionError("Discovery must not invoke authored handlers")


lookup = Tool(
    name="copilot_lookup",
    description="Native Copilot lookup",
    parameters={"type": "object", "properties": {"value": {"type": "string"}}},
    handler=_lookup,
    skip_permission=True,
    defer="never",
)
