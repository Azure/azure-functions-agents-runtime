import os
import time

if os.environ.get("AZURE_FUNCTIONS_AGENTS_SANDBOX") != "1":
    raise RuntimeError("delayed_probe.py must only be imported inside ACA Sandbox")


def delayed_probe(seconds: float) -> dict[str, object]:
    """Wait for one bounded cancellation or restart qualification interval."""
    if isinstance(seconds, bool) or not 0 <= seconds <= 30:
        raise ValueError("seconds must be between 0 and 30")
    started = time.monotonic()
    time.sleep(seconds)
    elapsed = time.monotonic() - started
    return {
        "completed": True,
        "requested_seconds": seconds,
        "elapsed_seconds": round(elapsed, 3),
        "control_marker": "delayed-probe-complete",
    }
