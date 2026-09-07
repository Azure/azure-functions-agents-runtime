import os

if os.environ.get("AZURE_FUNCTIONS_AGENTS_SANDBOX") != "1":
    raise RuntimeError("adaptive_probe.py must only be imported inside ACA Sandbox")

_NEXT_OBSERVATIONS = (
    "baseline-established",
    "initial-plan-observed",
    "direction-change-confirmed",
    "evidence-chain-confirmed",
    "terminal-evidence-ready",
)


def adaptive_probe(step: int, observation: str = "") -> dict[str, object]:
    """Advance one bounded adaptive qualification step."""
    if not isinstance(step, int) or isinstance(step, bool) or not 0 <= step <= 5:
        raise ValueError("step must be an integer between 0 and 5")
    if not isinstance(observation, str) or len(observation) > 128:
        raise ValueError("observation must be a string of at most 128 characters")
    if step > 0 and not observation:
        raise ValueError("observation is required after step 0")
    if step == 5:
        return {
            "protocol": "adaptive-probe-v1",
            "step": step,
            "terminal": True,
            "direction": "evidence-first",
            "evidence_marker": "adaptive-probe-terminal-5",
        }

    direction_changed = step == 2
    direction = "evidence-first" if step >= 2 else "initial"
    return {
        "protocol": "adaptive-probe-v1",
        "step": step,
        "terminal": False,
        "direction": direction,
        "direction_changed": direction_changed,
        "observation_received": observation,
        "next_action": {
            "tool": "adaptive_probe",
            "arguments": {
                "step": step + 1,
                "observation": _NEXT_OBSERVATIONS[step],
            },
        },
    }
