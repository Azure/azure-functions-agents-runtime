from __future__ import annotations

from azure_functions_agents.harness._harness_binding import AppHarness, HarnessKind
from azure_functions_agents.harness.agent_framework import _maf_observability as maf_obs


def test_maf_instrumentation_is_configured_once_per_bound_harness(
    monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    calls: list[bool] = []
    monkeypatch.setattr(maf_obs, "_capture_sensitive_data", lambda: True)
    monkeypatch.setattr(
        maf_obs,
        "enable_instrumentation",
        lambda *, enable_sensitive_data: calls.append(enable_sensitive_data),
    )
    harness = AppHarness(HarnessKind.MAF, tmp_path)

    maf_obs.configure_maf_instrumentation(harness)
    maf_obs.configure_maf_instrumentation(harness)

    assert calls == [True]


def test_maf_observability_module_imports_without_cycle() -> None:
    import importlib
    import sys

    sys.modules.pop("azure_functions_agents.harness.agent_framework._maf_observability", None)
    sys.modules.pop("azure_functions_agents._observability", None)

    module = importlib.import_module(
        "azure_functions_agents.harness.agent_framework._maf_observability"
    )

    assert hasattr(module, "configure_maf_instrumentation")
