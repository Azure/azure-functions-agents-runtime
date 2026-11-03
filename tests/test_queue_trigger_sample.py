import base64
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "queue-trigger-agent"


@pytest.mark.parametrize("heading", ["Deploy", "Run Locally"])
def test_queue_sample_readme_sends_base64_utf8_message(heading: str) -> None:
    readme = (SAMPLE / "README.md").read_text(encoding="utf-8")
    section = readme.split(f"## {heading}\n", 1)[1].split("\n## ", 1)[0]
    assignment = re.search(r"message=\$\((python -c .+)\)", section)
    assert assignment is not None, "Encode the message before the Azure CLI sends it."
    command = shlex.split(assignment[1])
    assert command[:2] == ["python", "-c"]
    result = subprocess.run(
        [sys.executable, *command[1:]], check=True, capture_output=True, text=True,
    )
    encoded = result.stdout.strip()
    assert base64.b64decode(encoded, validate=True).decode("utf-8") == (
        "Summarize this: order #42 for 3 widgets"
    )

    send = re.search(r"az storage message put (.+)", section.replace("\\\n", ""))
    assert send is not None
    arguments = shlex.split(send[1])
    assert arguments[arguments.index("--content") + 1] == "$message"


def test_queue_sample_keeps_host_base64_decoding() -> None:
    host = json.loads((SAMPLE / "src" / "host.json").read_text(encoding="utf-8"))
    assert host["extensions"].get("queues", {}).get("messageEncoding", "base64") == "base64"
