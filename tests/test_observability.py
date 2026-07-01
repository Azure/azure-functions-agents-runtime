from __future__ import annotations

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
    monkeypatch.setattr(obs, "_enable_agent_framework_instrumentation", lambda capture: None)
    monkeypatch.setattr(obs, "_configure_azure_monitor", lambda connection: None)

    resolved = obs.configure_observability(
        GlobalConfig(observability=ObservabilityConfig(capture_sensitive_data=True))
    )

    assert resolved.enabled is True
    assert resolved.capture_sensitive_data is True
    assert obs.capture_sensitive_data() is True


def test_configure_observability_disabled_is_noop(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    called = {"enable": False}

    def _fail_enable(capture: bool) -> None:
        called["enable"] = True

    monkeypatch.setattr(obs, "_enable_agent_framework_instrumentation", _fail_enable)
    resolved = obs.configure_observability(GlobalConfig())
    assert resolved.enabled is False
    assert called["enable"] is False


def test_start_span_is_safe_and_records(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Even with no exporter configured, the helpers must never raise.
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
