"""Fail-closed mapping between supported harness features and capabilities."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType

from .trace import SemanticTrace


class CapabilityCoverageError(Exception):
    """A harness capability is unknown or lacks an exercised semantic trace."""


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """One capability a harness can advertise after it has trace coverage."""

    name: str
    features: tuple[str, ...]


FEATURE_CAPABILITY_MAP = MappingProxyType(
    {
        "atomic_commit": "atomic-commit-v1",
        "watchdog": "watchdog-v1",
        "bootstrap": "bootstrap-v1",
        "delegation": "delegation-v1",
    }
)


def capability_for_feature(feature: str) -> str:
    """Resolve one recognized feature or fail closed for an unrecognized feature."""

    try:
        return FEATURE_CAPABILITY_MAP[feature]
    except KeyError:
        raise CapabilityCoverageError("Sandbox feature is not recognized.") from None


def validate_capability_coverage(
    advertised: Iterable[CapabilityDescriptor],
    traces: Iterable[SemanticTrace],
) -> None:
    """Require every advertised capability to be exercised by a known trace."""

    descriptors = tuple(advertised)
    trace_values = tuple(traces)
    known_capabilities = set(FEATURE_CAPABILITY_MAP.values())
    exercised = {
        capability
        for trace in trace_values
        for capability in trace.capabilities
    }
    unknown_trace_capabilities = exercised - known_capabilities
    if unknown_trace_capabilities:
        raise CapabilityCoverageError("Conformance trace declares an unknown capability.")
    advertised_names: set[str] = set()
    for descriptor in descriptors:
        if not descriptor.name or descriptor.name not in known_capabilities:
            raise CapabilityCoverageError("Harness advertises an unknown capability.")
        if descriptor.name in advertised_names:
            raise CapabilityCoverageError("Harness advertises a duplicate capability.")
        advertised_names.add(descriptor.name)
        for feature in descriptor.features:
            if capability_for_feature(feature) != descriptor.name:
                raise CapabilityCoverageError("Harness capability does not cover its declared feature.")
    missing = advertised_names - exercised
    if missing:
        raise CapabilityCoverageError("Harness capability lacks a conformance trace.")
