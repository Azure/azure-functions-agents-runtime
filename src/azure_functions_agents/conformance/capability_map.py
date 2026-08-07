"""Fail-closed mapping between supported harness features and capabilities."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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


@dataclass(frozen=True, slots=True)
class CapabilityEvidence:
    """The distinct trace and semantic event required for one capability."""

    trace_name: str
    event_type: str
    data: Mapping[str, object]
    requires_delegate_tool: bool = False


FEATURE_CAPABILITY_MAP = MappingProxyType(
    {
        "atomic_commit": "atomic_commit_v1",
        "watchdog": "watchdog_v1",
        "bootstrap": "bootstrap_v1",
        "delegation": "delegation_v1",
    }
)
CAPABILITY_EVIDENCE = MappingProxyType(
    {
        "atomic_commit_v1": CapabilityEvidence(
            trace_name="atomic_commit",
            event_type="atomic_commit",
            data=MappingProxyType({"state": "committed"}),
        ),
        "watchdog_v1": CapabilityEvidence(
            trace_name="watchdog",
            event_type="error",
            data=MappingProxyType({"code": "run_timed_out"}),
        ),
        "bootstrap_v1": CapabilityEvidence(
            trace_name="bootstrap_ready",
            event_type="session",
            data=MappingProxyType({"status": "ready"}),
        ),
        "delegation_v1": CapabilityEvidence(
            trace_name="delegation",
            event_type="tool_start",
            data=MappingProxyType({}),
            requires_delegate_tool=True,
        ),
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
    expected_trace_names: set[str] = set()
    for descriptor in descriptors:
        if not descriptor.name or descriptor.name not in known_capabilities:
            raise CapabilityCoverageError("Harness advertises an unknown capability.")
        if descriptor.name in advertised_names:
            raise CapabilityCoverageError("Harness advertises a duplicate capability.")
        advertised_names.add(descriptor.name)
        evidence = CAPABILITY_EVIDENCE[descriptor.name]
        if evidence.trace_name in expected_trace_names:
            raise CapabilityCoverageError("Harness capabilities must use distinct traces.")
        expected_trace_names.add(evidence.trace_name)
        for feature in descriptor.features:
            if capability_for_feature(feature) != descriptor.name:
                raise CapabilityCoverageError("Harness capability does not cover its declared feature.")
        if not any(
            trace.name == evidence.trace_name
            and descriptor.name in trace.capabilities
            and trace_exercises_capability(trace, descriptor.name)
            for trace in trace_values
        ):
            raise CapabilityCoverageError("Harness capability lacks an exercised conformance trace.")
    if advertised_names - exercised:
        raise CapabilityCoverageError("Harness capability lacks a conformance trace.")


def trace_exercises_capability(trace: SemanticTrace, capability: str) -> bool:
    """Return whether a trace contains the semantic evidence for one capability."""

    try:
        evidence = CAPABILITY_EVIDENCE[capability]
    except KeyError:
        raise CapabilityCoverageError("Harness capability is not recognized.") from None
    for event in trace.events:
        if event.type != evidence.event_type:
            continue
        if any(event.data.get(key) != value for key, value in evidence.data.items()):
            continue
        if evidence.requires_delegate_tool:
            tool_name = event.data.get("tool_name")
            if not isinstance(tool_name, str) or not tool_name.startswith("delegate_"):
                continue
        return True
    return False
