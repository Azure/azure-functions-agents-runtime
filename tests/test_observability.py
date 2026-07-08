from __future__ import annotations

import logging
import types

import azure_functions_agents._observability as obs


def _clear_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for name in (
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "ENABLE_SENSITIVE_DATA",
    ):
        monkeypatch.delenv(name, raising=False)


def _reset_bootstrap(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(obs, "_configured", False, raising=False)
    monkeypatch.setattr(obs, "_enabled", False, raising=False)
    monkeypatch.setattr(obs, "_capture_sensitive_data", False, raising=False)


# --- capture_sensitive_data resolution (reuses MAF's ENABLE_SENSITIVE_DATA) --------------------


def test_capture_sensitive_data_default_off(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    assert obs._resolve_capture_sensitive_data() is False


def test_capture_sensitive_data_from_enable_sensitive_data_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    monkeypatch.setenv("ENABLE_SENSITIVE_DATA", "true")
    assert obs._resolve_capture_sensitive_data() is True


# --- configure_observability enablement (driven by an active OTel provider) --------------------


def test_configure_observability_enabled_when_provider_active(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=abc")
    monkeypatch.setenv("ENABLE_SENSITIVE_DATA", "true")
    _reset_bootstrap(monkeypatch)
    monkeypatch.setattr(obs, "_otel_provider_already_configured", lambda: True)
    monkeypatch.setattr(obs, "_configure_azure_monitor", lambda connection: None)
    enable_calls: list[bool] = []
    monkeypatch.setattr(
        obs, "_enable_agent_framework_instrumentation", lambda capture: enable_calls.append(capture)
    )

    resolved = obs.configure_observability()

    assert resolved.enabled is True
    assert resolved.capture_sensitive_data is True
    assert obs.capture_sensitive_data() is True
    assert obs.is_observability_enabled() is True
    assert enable_calls == [True]  # MAF instrumentation enabled with the resolved capture flag


def test_configure_observability_noop_without_provider_or_connection(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    _reset_bootstrap(monkeypatch)
    monkeypatch.setattr(obs, "_otel_provider_already_configured", lambda: False)
    enable_calls: list[bool] = []
    monkeypatch.setattr(
        obs, "_enable_agent_framework_instrumentation", lambda capture: enable_calls.append(capture)
    )

    resolved = obs.configure_observability()

    assert resolved.enabled is False
    assert enable_calls == []
    assert obs.is_observability_enabled() is False


def test_configure_observability_rides_existing_worker_provider(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # No connection string, but a provider is already active (e.g. the Functions worker): the runtime
    # still emits, riding the existing provider, without configuring its own exporter.
    _clear_env(monkeypatch)
    _reset_bootstrap(monkeypatch)
    monkeypatch.setattr(obs, "_otel_provider_already_configured", lambda: True)
    configure_calls: list[str] = []
    monkeypatch.setattr(
        obs, "_configure_azure_monitor", lambda connection: configure_calls.append(connection)
    )
    monkeypatch.setattr(obs, "_enable_agent_framework_instrumentation", lambda capture: None)

    resolved = obs.configure_observability()

    assert resolved.enabled is True
    assert configure_calls == []  # no connection string => never attempts its own exporter setup


def test_configure_observability_warns_when_connection_but_no_exporter(  # type: ignore[no-untyped-def]
    monkeypatch, caplog
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=abc")
    _reset_bootstrap(monkeypatch)
    # Exporter missing / no provider becomes active after the configure attempt.
    monkeypatch.setattr(obs, "_configure_azure_monitor", lambda connection: None)
    monkeypatch.setattr(obs, "_otel_provider_already_configured", lambda: False)
    enable_calls: list[bool] = []
    monkeypatch.setattr(
        obs, "_enable_agent_framework_instrumentation", lambda capture: enable_calls.append(capture)
    )

    with caplog.at_level(logging.WARNING, logger="azure.functions.AgentRuntime"):
        resolved = obs.configure_observability()

    assert resolved.enabled is False
    assert enable_calls == []
    assert "azurefunctions-agents-runtime[monitor]" in caplog.text


def test_configure_observability_quiets_loggers_even_when_disabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Noise control runs regardless of whether observability ends up enabled.
    _clear_env(monkeypatch)
    _reset_bootstrap(monkeypatch)
    monkeypatch.setattr(obs, "_otel_provider_already_configured", lambda: False)
    name = "azure.identity"
    logging.getLogger(name).setLevel(logging.NOTSET)

    obs.configure_observability()

    assert logging.getLogger(name).level == logging.WARNING
    logging.getLogger(name).setLevel(logging.NOTSET)  # reset for other tests


def test_otel_provider_already_configured_true_for_sdk_provider(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    monkeypatch.setattr(trace, "get_tracer_provider", lambda: TracerProvider())

    assert obs._otel_provider_already_configured() is True


def test_otel_provider_already_configured_false_for_proxy_provider(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from opentelemetry import trace

    proxy_tracer_provider = type("ProxyTracerProvider", (), {})
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: proxy_tracer_provider())

    assert obs._otel_provider_already_configured() is False


def test_configure_azure_monitor_skips_when_provider_already_configured(  # type: ignore[no-untyped-def]
    monkeypatch, caplog
) -> None:
    import logging

    from azure.monitor import opentelemetry as azure_monitor_opentelemetry

    called = {"count": 0}

    def _fake_configure_azure_monitor(*, connection_string: str) -> None:
        called["count"] += 1

    monkeypatch.setattr(obs, "_otel_provider_already_configured", lambda: True)
    monkeypatch.setattr(
        azure_monitor_opentelemetry,
        "configure_azure_monitor",
        _fake_configure_azure_monitor,
    )

    with caplog.at_level(logging.INFO, logger="azure.functions.AgentRuntime"):
        obs._configure_azure_monitor("InstrumentationKey=abc")

    assert called["count"] == 0
    assert "skipping the runtime's Azure Monitor setup to avoid duplicate export" in caplog.text


def test_configure_azure_monitor_calls_when_provider_not_configured(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from azure.monitor import opentelemetry as azure_monitor_opentelemetry

    called = {"connection_string": None}

    def _fake_configure_azure_monitor(*, connection_string: str) -> None:
        called["connection_string"] = connection_string

    monkeypatch.setattr(obs, "_otel_provider_already_configured", lambda: False)
    monkeypatch.setattr(
        azure_monitor_opentelemetry,
        "configure_azure_monitor",
        _fake_configure_azure_monitor,
    )

    obs._configure_azure_monitor("InstrumentationKey=abc")

    assert called["connection_string"] == "InstrumentationKey=abc"


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
