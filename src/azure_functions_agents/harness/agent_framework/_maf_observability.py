"""MAF-specific instrumentation setup owned by the bound MAF harness."""

from __future__ import annotations

from ..._logger import logger
from ..._observability import capture_sensitive_data
from .._harness_binding import AppHarness
from ._maf_warnings import suppress_experimental_warnings

_configured_harnesses: set[int] = set()


def configure_maf_instrumentation(harness: AppHarness) -> None:
    """Enable MAF instrumentation once for a selected MAF resource binding."""
    key = id(harness)
    if key in _configured_harnesses:
        return
    try:
        with suppress_experimental_warnings():
            from agent_framework.observability import enable_instrumentation

            enable_instrumentation(enable_sensitive_data=capture_sensitive_data())
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not enable Agent Framework instrumentation: %s", exc)
    _configured_harnesses.add(key)
