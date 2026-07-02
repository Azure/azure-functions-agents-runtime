from __future__ import annotations

import types

import azure_functions_agents._observability as obs
from azure_functions_agents.config.schema import GlobalConfig, ObservabilityConfig


def _clear_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for name in (
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "AZURE_FUNCTIONS_AGENTS_OBSERVABILITY_ENABLED",
        "AZURE_FUNCTIONS_AGENTS_CAPTURE_SENSITIVE_DATA",
        "ENABLE_SENSITIVE_DATA",
    ):
        monkeypatch.delenv(name, raising=False)


def test_resolve_disabled_without_connection_string(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    resolved = obs._resolve(GlobalConfig())
    assert resolved.enabled is False
    assert resolved.capture_sensitive_data is False


def test_resolve_enabled_when_connection_present(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=abc")
    resolved = obs._resolve(GlobalConfig())
    assert resolved.enabled is True


def test_resolve_explicit_disable_wins_over_connection(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=abc")
    config = GlobalConfig(observability=ObservabilityConfig(enabled=False))
    assert obs._resolve(config).enabled is False


def test_capture_sensitive_data_from_config(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    config = GlobalConfig(observability=ObservabilityConfig(capture_sensitive_data=True))
    assert obs._resolve(config).capture_sensitive_data is True


def test_capture_sensitive_data_env_overrides_config(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    monkeypatch.setenv("AZURE_FUNCTIONS_AGENTS_CAPTURE_SENSITIVE_DATA", "true")
    config = GlobalConfig(observability=ObservabilityConfig(capture_sensitive_data=False))
    assert obs._resolve(config).capture_sensitive_data is True


def test_configure_observability_sets_capture_flag(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=abc")
    monkeypatch.setattr(obs, "_configured", False, raising=False)
    monkeypatch.setattr(obs, "_enabled", False, raising=False)
    monkeypatch.setattr(obs, "_enable_agent_framework_instrumentation", lambda capture: None)
    monkeypatch.setattr(obs, "_configure_azure_monitor", lambda connection: None)

    resolved = obs.configure_observability(
        GlobalConfig(observability=ObservabilityConfig(capture_sensitive_data=True))
    )

    assert resolved.enabled is True
    assert resolved.capture_sensitive_data is True
    assert obs.capture_sensitive_data() is True
    assert obs.is_observability_enabled() is True


def test_configure_observability_disabled_is_noop(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    monkeypatch.setattr(obs, "_enabled", False, raising=False)
    called = {"enable": False}

    def _fail_enable(capture: bool) -> None:
        called["enable"] = True

    monkeypatch.setattr(obs, "_enable_agent_framework_instrumentation", _fail_enable)
    resolved = obs.configure_observability(GlobalConfig())
    assert resolved.enabled is False
    assert called["enable"] is False
    assert obs.is_observability_enabled() is False


def test_start_span_is_safe_and_records(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # With observability enabled, the helpers exercise the real tracer path and must never raise.
    monkeypatch.setattr(obs, "_enabled", True)
    with obs.start_span(
        "unit.test.span",
        fault_domain=obs.FaultDomain.RUNTIME,
        lifecycle_stage=obs.LifecycleStage.AGENT_RUN,
        attributes={"k": "v", "none_is_skipped": None},
    ) as span:
        span.set_attribute("x", 1)
        span.set_content("secret", "value")
        span.set_error("boom", fault_domain=obs.FaultDomain.APP)
        span.record_exception(ValueError("nope"), fault_domain=obs.FaultDomain.SANDBOX)


def test_start_span_gated_when_observability_disabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # When disabled, no underlying OTel span is created even if a provider exists.
    monkeypatch.setattr(obs, "_enabled", False)
    with obs.start_span("gated.span", lifecycle_stage=obs.LifecycleStage.AGENT_RUN) as span:
        assert span._span is None


def test_runtime_span_add_event_noops_without_span() -> None:
    span = obs.RuntimeSpan(None)
    span.add_event("unit.test.event", {"ignored": "value"})


def test_runtime_span_add_event_forwards_name_and_non_none_attributes() -> None:
    events: list[tuple[str, dict[str, object] | None]] = []

    class _FakeSpan:
        def add_event(self, name: str, attributes: dict[str, object] | None = None) -> None:
            events.append((name, attributes))

    span = obs.RuntimeSpan(_FakeSpan())
    span.add_event("unit.test.event", {"kept": "value", "count": 2, "dropped": None})

    assert events == [("unit.test.event", {"kept": "value", "count": 2})]


def test_record_sandbox_execution_gated_when_disabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []
    monkeypatch.setattr(obs, "_metrics_ready", True)
    monkeypatch.setattr(
        obs, "_sandbox_execution_counter", types.SimpleNamespace(add=lambda *a, **k: calls.append("x"))
    )
    monkeypatch.setattr(
        obs, "_sandbox_error_counter", types.SimpleNamespace(add=lambda *a, **k: calls.append("e"))
    )

    monkeypatch.setattr(obs, "_enabled", False)
    obs.record_sandbox_execution(error=True)
    assert calls == []  # gated when disabled

    monkeypatch.setattr(obs, "_enabled", True)
    obs.record_sandbox_execution(error=True)
    assert calls == ["x", "e"]  # emitted when enabled


def test_bounded_content_truncates() -> None:
    long = "a" * (obs._CONTENT_ATTR_MAX_CHARS + 100)
    trimmed = obs.bounded_content(long)
    assert trimmed.endswith("…[truncated]")
    assert len(trimmed) < len(long)


def test_record_sandbox_execution_is_safe() -> None:
    obs.record_sandbox_execution(error=False)
    obs.record_sandbox_execution(error=True)


def test_quiet_noisy_loggers_raises_unset_levels() -> None:
    import logging

    name = "azure.core.pipeline.policies.http_logging_policy"
    logging.getLogger(name).setLevel(logging.NOTSET)

    obs._quiet_noisy_loggers()

    assert logging.getLogger(name).level == logging.WARNING


def test_quiet_noisy_loggers_respects_explicit_level() -> None:
    import logging

    # Pick a noisy logger and set it explicitly to DEBUG; quieting must not override it.
    name = "httpx"
    logging.getLogger(name).setLevel(logging.DEBUG)

    obs._quiet_noisy_loggers()

    assert logging.getLogger(name).level == logging.DEBUG
    logging.getLogger(name).setLevel(logging.NOTSET)  # reset for other tests
