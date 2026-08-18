"""A checked-in, fixture-only active-run hold for the manual load qualification."""

from __future__ import annotations

import asyncio

QUALIFICATION_HOLD_SECONDS = 300


async def qualification_hold() -> str:
    """Hold this real model turn long enough to observe concurrent durable runs."""
    await asyncio.sleep(QUALIFICATION_HOLD_SECONDS)
    return "hold complete"
