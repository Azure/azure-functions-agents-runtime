"""Deterministic receipt data used by the evaluation sample."""

from __future__ import annotations

from typing import Any


def read_receipt(currency: str) -> dict[str, Any]:
    """Read the sample receipt total in the requested currency.

    Args:
        currency: ISO currency code for the returned total. Use ``USD``.
    """
    if currency.upper() != "USD":
        return {
            "error": "The sample receipt is available only in USD.",
            "currency": currency,
        }
    return {
        "merchant": "Contoso Cafe",
        "total": 42.18,
        "currency": "USD",
    }
