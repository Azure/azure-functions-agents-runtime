"""MAF-specific instrumentation setup owned by the bound MAF harness."""

from __future__ import annotations

from agent_framework.observability import enable_instrumentation

from ..._logger import logger
from .._harness_binding import AppHarness

_configured_harnesses: set[int] = set()


def _capture_sensitive_data() -> bool:
    from ..._observability import capture_sensitive_data

    return capture_sensitive_data()


def configure_maf_instrumentation(harness: AppHarness) -> None:
    """Enable MAF instrumentation once for a selected MAF resource binding."""
    key = id(harness)
    if key in _configured_harnesses:
        return
    try:
        enable_instrumentation(enable_sensitive_data=_capture_sensitive_data())
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not enable Agent Framework instrumentation: %s", exc)
    _configured_harnesses.add(key)
