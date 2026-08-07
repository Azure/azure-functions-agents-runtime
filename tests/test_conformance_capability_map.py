from __future__ import annotations

from pathlib import Path

import pytest

from azure_functions_agents.conformance.capability_map import (
    CapabilityCoverageError,
    CapabilityDescriptor,
    capability_for_feature,
    validate_capability_coverage,
)
from azure_functions_agents.conformance.trace import parse_trace
from azure_functions_agents.harness.sandbox_capabilities import HARNESS_CAPABILITIES

_TRACE_DIRECTORY = Path(__file__).parent / "conformance" / "traces"


def _golden_traces() -> list[object]:
    return [parse_trace(path.read_bytes()) for path in _TRACE_DIRECTORY.glob("*.json")]


def test_exported_harness_capabilities_have_golden_trace_coverage() -> None:
    validate_capability_coverage(HARNESS_CAPABILITIES, _golden_traces())


def test_unknown_feature_fails_closed() -> None:
    with pytest.raises(CapabilityCoverageError):
        capability_for_feature("unknown")


def test_advertised_capability_without_a_trace_fails_closed() -> None:
    with pytest.raises(CapabilityCoverageError, match="lacks"):
        validate_capability_coverage(
            (CapabilityDescriptor(name="delegation_v1", features=("delegation",)),),
            (),
        )


def test_unknown_trace_capability_fails_closed() -> None:
    trace = parse_trace(
        {
            "name": "unknown",
            "capabilities": ["unsupported"],
            "events": [],
            "terminal_state": "succeeded",
        }
    )

    with pytest.raises(CapabilityCoverageError, match="unknown"):
        validate_capability_coverage((), (trace,))


def test_generic_trace_cannot_claim_a_different_capability() -> None:
    trace = parse_trace(
        {
            "name": "bootstrap_ready",
            "capabilities": ["bootstrap_v1", "delegation_v1"],
            "events": [{"type": "session", "data": {"status": "ready"}}],
            "terminal_state": "succeeded",
        }
    )
    delegation = next(
        descriptor for descriptor in HARNESS_CAPABILITIES if descriptor.name == "delegation_v1"
    )

    with pytest.raises(CapabilityCoverageError, match="exercised"):
        validate_capability_coverage((delegation,), (trace,))


def test_capability_trace_requires_its_semantic_event() -> None:
    trace = parse_trace(
        {
            "name": "atomic_commit",
            "capabilities": ["atomic_commit_v1"],
            "events": [{"type": "done", "data": {"state": "succeeded"}}],
            "terminal_state": "succeeded",
        }
    )
    atomic_commit = next(
        descriptor for descriptor in HARNESS_CAPABILITIES if descriptor.name == "atomic_commit_v1"
    )

    with pytest.raises(CapabilityCoverageError, match="exercised"):
        validate_capability_coverage((atomic_commit,), (trace,))
