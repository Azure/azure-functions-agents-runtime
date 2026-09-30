import hashlib

from azure_functions_agents import tool


@tool
def make_receipt(tag: str) -> str:
    """Return a deterministic receipt for a harmless demonstration tag."""
    return "receipt-" + hashlib.sha256(tag.encode("utf-8")).hexdigest()[:24]
