import os

if os.environ.get("AZURE_FUNCTIONS_AGENTS_SANDBOX") != "1":
    raise RuntimeError("chain_probe.py must only be imported inside ACA Sandbox")


def chain_probe(step: int) -> dict[str, object]:
    """Advance one compact deterministic chain step."""
    if not isinstance(step, int) or isinstance(step, bool) or not 0 <= step <= 16:
        raise ValueError("step must be an integer between 0 and 16")
    if step == 16:
        return {
            "protocol": "chain-probe-v1",
            "step": step,
            "terminal": True,
            "evidence_marker": "chain-probe-terminal-16",
        }
    return {
        "protocol": "chain-probe-v1",
        "step": step,
        "terminal": False,
        "next_action": {
            "tool": "chain_probe",
            "arguments": {"step": step + 1},
        },
    }
