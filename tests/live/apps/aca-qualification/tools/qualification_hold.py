"""Fixture-only active-run hold for deployed ACA load qualification."""

from __future__ import annotations

import asyncio

QUALIFICATION_HOLD_SECONDS = 300


async def qualification_hold() -> str:
    """Hold long enough for the live suites to observe and disrupt an active run."""
    await asyncio.sleep(QUALIFICATION_HOLD_SECONDS)
    return "hold complete"
