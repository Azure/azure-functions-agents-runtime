from __future__ import annotations

from contextlib import nullcontext

from azure_functions_agents.harness._harness_binding import AppHarness, HarnessKind
from azure_functions_agents.harness.agent_framework import _maf_observability as maf_obs


def test_maf_instrumentation_is_configured_once_per_bound_harness(
    monkeypatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    import agent_framework.observability as sdk_observability

    calls: list[bool] = []
    monkeypatch.setattr(maf_obs, "suppress_experimental_warnings", nullcontext)
    monkeypatch.setattr(maf_obs, "capture_sensitive_data", lambda: True)
    monkeypatch.setattr(
        sdk_observability,
        "enable_instrumentation",
        lambda *, enable_sensitive_data: calls.append(enable_sensitive_data),
    )
    harness = AppHarness(HarnessKind.MAF, tmp_path)

    maf_obs.configure_maf_instrumentation(harness)
    maf_obs.configure_maf_instrumentation(harness)

    assert calls == [True]
