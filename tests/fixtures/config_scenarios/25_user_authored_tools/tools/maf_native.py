from agent_framework import FunctionTool


def _lookup(value: str) -> str:
    raise AssertionError("Discovery must not invoke authored functions")


lookup = FunctionTool(name="maf_lookup", description="Native MAF lookup", func=_lookup)
