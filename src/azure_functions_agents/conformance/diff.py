"""Compare normalized traces without relying on model wording or timing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .trace import SemanticTrace, normalize_trace


@dataclass(frozen=True, slots=True)
class SemanticDifference:
    """One durable semantic mismatch between expected and observed behavior."""

    path: str
    expected: Any
    actual: Any


def semantic_diff(
    expected: SemanticTrace,
    actual: SemanticTrace,
) -> tuple[SemanticDifference, ...]:
    """Return event-order, key-field, capability, and terminal-state differences."""

    left = normalize_trace(expected)
    right = normalize_trace(actual)
    differences: list[SemanticDifference] = []
    if left.capabilities != right.capabilities:
        differences.append(
            SemanticDifference("capabilities", left.capabilities, right.capabilities)
        )
    if left.terminal_state != right.terminal_state:
        differences.append(
            SemanticDifference("terminal_state", left.terminal_state, right.terminal_state)
        )
    if len(left.events) != len(right.events):
        differences.append(
            SemanticDifference("events.length", len(left.events), len(right.events))
        )
    for index, (expected_event, actual_event) in enumerate(
        zip(left.events, right.events, strict=False)
    ):
        if expected_event.type != actual_event.type:
            differences.append(
                SemanticDifference(
                    f"events[{index}].type",
                    expected_event.type,
                    actual_event.type,
                )
            )
        if expected_event.data != actual_event.data:
            differences.append(
                SemanticDifference(
                    f"events[{index}].data",
                    expected_event.data,
                    actual_event.data,
                )
            )
    return tuple(differences)
