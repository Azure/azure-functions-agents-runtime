"""MAF-scoped warning handling for SDK imports and construction."""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def suppress_experimental_warnings() -> Iterator[None]:
    """Suppress MAF's experimental category only within MAF-owned operations."""
    with warnings.catch_warnings(record=True) as import_warnings:
        warnings.simplefilter("always")
        try:
            from agent_framework._feature_stage import ExperimentalWarning
        except ImportError:
            _restore_warnings(import_warnings)
            yield
            return

    for warning in import_warnings:
        if not issubclass(warning.category, ExperimentalWarning):
            _restore_warning(warning)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=ExperimentalWarning)
        yield


def _restore_warnings(warnings_to_restore: list[warnings.WarningMessage]) -> None:
    for warning in warnings_to_restore:
        _restore_warning(warning)


def _restore_warning(warning: warnings.WarningMessage) -> None:
    message = warning.message
    if isinstance(message, Warning):
        warnings.warn_explicit(message, warning.category, warning.filename, warning.lineno)
    else:
        warnings.warn_explicit(str(message), warning.category, warning.filename, warning.lineno)
