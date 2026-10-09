"""MAF-scoped suppression for the known experimental SDK warnings."""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from contextlib import contextmanager

from agent_framework._feature_stage import ExperimentalWarning


@contextmanager
def suppress_experimental_warnings() -> Iterator[None]:
    """Suppress ExperimentalWarning from FileSystemAgentFileStore and FileHistoryProvider."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=ExperimentalWarning)
        yield
