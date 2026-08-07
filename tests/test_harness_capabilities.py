from __future__ import annotations

import pytest

from azure_functions_agents.harness import HarnessCapabilityRegistry


def test_base_capabilities_are_available_from_a_frozen_snapshot() -> None:
    registry = HarnessCapabilityRegistry()

    snapshot = registry.freeze()

    assert dict(snapshot) == {
        "atomic_commit": "atomic-commit-v1",
        "watchdog": "watchdog-v1",
    }
    with pytest.raises(TypeError):
        snapshot["new"] = "value"  # type: ignore[index]


def test_capability_provider_is_closed_on_duplicates_unknowns_and_late_registration() -> None:
    registry = HarnessCapabilityRegistry()
    registry.register("extension", {"extra": "extra-v1"})

    with pytest.raises(ValueError):
        registry.register("duplicate-capability", {"other": "extra-v1"})
    with pytest.raises(ValueError):
        registry.register("extension", {"other": "other-v1"})

    assert registry.capability_for_feature("extra") == "extra-v1"
    with pytest.raises(ValueError):
        registry.capability_for_feature("missing")
    registry.freeze()
    with pytest.raises(RuntimeError):
        registry.register("late", {"late": "late-v1"})
