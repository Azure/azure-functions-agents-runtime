import os
import subprocess
import sys
import urllib.request
from pathlib import Path

if os.environ.get("AZURE_FUNCTIONS_AGENTS_SANDBOX") != "1":
    raise RuntimeError(
        "prepare_demo_workspace.py must only be imported inside ACA Sandbox"
    )

_FILE_NAME = "demo-context.txt"
_MARKER = "durable-demo-context-v1"
_ALLOWED_URL = f"{'https'}://www.example.com"
_WORKSPACE_ENV = "AZURE_FUNCTIONS_AGENTS_SANDBOX_WORKSPACE_ROOT"


def prepare_demo_workspace(content: str) -> dict[str, object]:
    """Create the deterministic demo file and one observable tool subprocess."""
    if not isinstance(content, str) or not 1 <= len(content) <= 512:
        raise ValueError("content must contain between 1 and 512 characters")
    workspace = os.environ.get(_WORKSPACE_ENV)
    if not workspace:
        raise RuntimeError("sandbox workspace root is unavailable")
    path = Path(workspace) / _FILE_NAME
    encoded = content.encode("utf-8")
    path.write_bytes(encoded)
    with urllib.request.urlopen(_ALLOWED_URL, timeout=10) as response:
        response.read(64)
        status = response.status
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(180)",
            "durable-demo-tool-process",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {
        "bytes_written": len(encoded),
        "egress_host": "www.example.com",
        "egress_status": status,
        "file_name": _FILE_NAME,
        "marker": _MARKER,
        "process_id": process.pid,
        "process_observable_seconds": 180,
    }
