import hashlib
import json
import os
import re
from pathlib import Path

if os.environ.get("AZURE_FUNCTIONS_AGENTS_SANDBOX") != "1":
    raise RuntimeError("unsafe_write_probe.py must only be imported inside ACA Sandbox")

_MARKER_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SIDE_EFFECT_PATH = Path(
    "/tmp/azure-functions-agents-runtime/durable-loop/unsafe-write-probe.json"
)


def _load_write_count() -> int:
    if not _SIDE_EFFECT_PATH.exists():
        return 0
    data = json.loads(_SIDE_EFFECT_PATH.read_text(encoding="utf-8"))
    count = data.get("write_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise RuntimeError("unsafe write marker is invalid")
    return count


def unsafe_write_probe(marker: str) -> dict[str, object]:
    """Record one intentionally unsafe sandbox-local side effect."""
    if not isinstance(marker, str) or _MARKER_PATTERN.fullmatch(marker) is None:
        raise ValueError("marker must contain 1-64 safe characters")

    write_count = _load_write_count() + 1
    payload = {
        "marker_sha256": hashlib.sha256(marker.encode("utf-8")).hexdigest(),
        "write_count": write_count,
    }
    _SIDE_EFFECT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = _SIDE_EFFECT_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(_SIDE_EFFECT_PATH)
    return {
        "acknowledged": True,
        "write_count": write_count,
        "control_marker": "unsafe-write-recorded",
    }
