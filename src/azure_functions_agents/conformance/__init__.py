"""Semantic conformance contracts for sandbox harness implementations."""

from .capability_map import (
    CapabilityDescriptor,
    capability_for_feature,
    validate_capability_coverage,
)
from .diff import SemanticDifference, semantic_diff
from .trace import SemanticTrace, TraceEvent, normalize_trace, parse_trace

__all__ = [
    "CapabilityDescriptor",
    "SemanticDifference",
    "SemanticTrace",
    "TraceEvent",
    "capability_for_feature",
    "normalize_trace",
    "parse_trace",
    "semantic_diff",
    "validate_capability_coverage",
]
