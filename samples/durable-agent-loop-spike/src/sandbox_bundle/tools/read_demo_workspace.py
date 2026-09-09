import os
from pathlib import Path

if os.environ.get("AZURE_FUNCTIONS_AGENTS_SANDBOX") != "1":
    raise RuntimeError(
        "read_demo_workspace.py must only be imported inside ACA Sandbox"
    )

_FILE_NAME = "demo-context.txt"
_MARKER = "durable-demo-context-v1"
_WORKSPACE_ENV = "AZURE_FUNCTIONS_AGENTS_SANDBOX_WORKSPACE_ROOT"


def read_demo_workspace() -> dict[str, object]:
    """Read the exact workspace file produced by the focused demo."""
    workspace = os.environ.get(_WORKSPACE_ENV)
    if not workspace:
        raise RuntimeError("sandbox workspace root is unavailable")
    content = (Path(workspace) / _FILE_NAME).read_text(encoding="utf-8")
    return {
        "content": content,
        "file_name": _FILE_NAME,
        "marker": _MARKER,
    }
