import os

if os.environ.get("AZURE_FUNCTIONS_AGENTS_SANDBOX") != "1":
    raise RuntimeError("customer_probe.py must only be imported inside ACA Sandbox")


def customer_probe(message: str, repeat: int = 1) -> dict[str, object]:
    """Return a deterministic customer-tool result from the sandbox."""
    if not 1 <= repeat <= 20:
        raise ValueError("repeat must be between 1 and 20")
    return {
        "message": message,
        "repeat": repeat,
        "rendered": "|".join(message for _ in range(repeat)),
        "sandbox_marker": True,
        "process_id": os.getpid(),
    }
