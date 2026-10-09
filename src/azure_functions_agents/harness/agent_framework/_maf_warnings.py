"""MAF-scoped warning handling for SDK imports and construction."""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def suppress_experimental_warnings() -> Iterator[None]:
    """Suppress current MAF ExperimentalWarning noise only within MAF-owned operations."""
    experimental_warning, import_warnings = _load_experimental_warning()
    if experimental_warning is None:
        _restore_warnings(import_warnings)
        yield
        return

    for warning in import_warnings:
        if not issubclass(warning.category, experimental_warning):
            _restore_warning(warning)

    with warnings.catch_warnings():
        # agent-framework-core 1.13.0 still emits ExperimentalWarning when constructing
        # create_harness_agent() (HARNESS/FileSystemAgentFileStore) and FileHistoryProvider.
        warnings.simplefilter("ignore", category=experimental_warning)
        yield


def _load_experimental_warning() -> tuple[type[Warning] | None, list[warnings.WarningMessage]]:
    with warnings.catch_warnings(record=True) as import_warnings:
        warnings.simplefilter("always")
        try:
            from agent_framework._feature_stage import ExperimentalWarning
        except ImportError:
            return None, list(import_warnings)
    return ExperimentalWarning, list(import_warnings)


def _restore_warnings(warnings_to_restore: list[warnings.WarningMessage]) -> None:
    for warning in warnings_to_restore:
        _restore_warning(warning)


def _restore_warning(warning: warnings.WarningMessage) -> None:
    message = warning.message
    if isinstance(message, Warning):
        warnings.warn_explicit(message, warning.category, warning.filename, warning.lineno)
    else:
        warnings.warn_explicit(str(message), warning.category, warning.filename, warning.lineno)
