from __future__ import annotations

import logging
import types
from contextlib import nullcontext

import pytest

import azure_functions_agents._observability as obs


def _clear_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for name in (
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "APPLICATIONINSIGHTS_AUTHENTICATION_STRING",
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
    pytest.importorskip("azure.monitor.opentelemetry")
    import logging

    from azure.monitor import opentelemetry as azure_monitor_opentelemetry

    _clear_env(monkeypatch)
    called = {"count": 0}

    def _fake_configure_azure_monitor(*, connection_string: str, **_kwargs) -> None:
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
    pytest.importorskip("azure.monitor.opentelemetry")
    from azure.monitor import opentelemetry as azure_monitor_opentelemetry

    _clear_env(monkeypatch)
    called: dict[str, object] = {}

    def _fake_configure_azure_monitor(*, connection_string: str, **kwargs) -> None:
        called["connection_string"] = connection_string
        called["kwargs"] = kwargs

    monkeypatch.setattr(obs, "_otel_provider_already_configured", lambda: False)
    monkeypatch.setattr(
        azure_monitor_opentelemetry,
        "configure_azure_monitor",
        _fake_configure_azure_monitor,
    )

    obs._configure_azure_monitor("InstrumentationKey=abc")

    assert called["connection_string"] == "InstrumentationKey=abc"
    # No AAD auth string configured: Live Metrics is left at its default (enabled).
    assert "enable_live_metrics" not in called["kwargs"]


def test_configure_azure_monitor_disables_live_metrics_when_aad_auth_configured(  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """QuickPulse/Live Metrics doesn't honor APPLICATIONINSIGHTS_AUTHENTICATION_STRING (upstream
    gap: the exporter only accepts an explicit ``credential=`` kwarg, never falling back to the
    env var like the trace/log/metric exporters do). On an Application Insights resource that
    requires AAD, this makes Live Metrics fail every ~1s with a 401. When AAD auth is configured
    via the env var, the runtime disables Live Metrics rather than emitting non-stop 401 noise.
    """
    pytest.importorskip("azure.monitor.opentelemetry")
    from azure.monitor import opentelemetry as azure_monitor_opentelemetry

    _clear_env(monkeypatch)
    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_AUTHENTICATION_STRING",
        "Authorization=AAD;ClientId=00000000-0000-0000-0000-000000000000",
    )
    called: dict[str, object] = {}

    def _fake_configure_azure_monitor(*, connection_string: str, **kwargs) -> None:
        called["connection_string"] = connection_string
        called["kwargs"] = kwargs

    monkeypatch.setattr(obs, "_otel_provider_already_configured", lambda: False)
    monkeypatch.setattr(
        azure_monitor_opentelemetry,
        "configure_azure_monitor",
        _fake_configure_azure_monitor,
    )

    obs._configure_azure_monitor("InstrumentationKey=abc")

    assert called["connection_string"] == "InstrumentationKey=abc"
    assert called["kwargs"] == {"enable_live_metrics": False}


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


# --- workflow task Activity telemetry (FRD 0004) -----------------------------------------------


def _fake_span() -> tuple[object, dict[str, object]]:
    attributes: dict[str, object] = {}

    class _Span:
        def set_attribute(self, key: str, value: object) -> None:
            attributes[key] = value

        def set_status(self, status: object) -> None:
            return None

    return _Span(), attributes


def _install_workflow_task_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, object]]:
    starts: list[dict[str, object]] = []
    outcomes: list[dict[str, object]] = []
    raw_span, attributes = _fake_span()

    class _Tracer:
        def start_as_current_span(self, name: str) -> object:
            return nullcontext(raw_span)

    monkeypatch.setattr(obs, "_enabled", True)
    monkeypatch.setattr(obs, "_metrics_ready", True)
    monkeypatch.setattr(
        obs,
        "_workflow_task_start_counter",
        types.SimpleNamespace(add=lambda count, attrs: starts.append(attrs)),
    )
    monkeypatch.setattr(
        obs,
        "_workflow_task_completion_counter",
        types.SimpleNamespace(add=lambda count, attrs: outcomes.append(attrs)),
    )
    monkeypatch.setattr(obs, "get_tracer", lambda: _Tracer())
    return starts, outcomes, attributes


def test_workflow_task_telemetry_keeps_high_cardinality_keys_off_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starts, outcomes, attributes = _install_workflow_task_metrics(monkeypatch)

    with obs.workflow_task_activity_telemetry({
        "af.workflow_task.workflow_id": "wf-1",
        "af.workflow_task.node_instance_id": "inspect[3]",
        "af.workflow_task.target_name": "reserve_inventory",
        "af.workflow_task.target_type": "tool",
        "af.workflow_task.max_attempts": 3,
        "af.workflow_task.continue_on_error": True,
        "af.workflow_task.timeout_ms": 5000,
        "af.workflow_task.retry_driver": "durable",
    }) as telemetry:
        telemetry.complete(
            outcome_kind="handler_transient",
            disposition="request_durable_retry",
            error_code="inventory_busy",
            fault_domain=obs.FaultDomain.APP,
        )

    # The span keeps every identifier for per-attempt investigation...
    assert attributes["af.workflow_task.workflow_id"] == "wf-1"
    assert attributes["af.workflow_task.node_instance_id"] == "inspect[3]"
    assert attributes["af.workflow_task.error_code"] == "inventory_busy"
    assert attributes[obs.ATTR_FAULT_DOMAIN] == obs.FaultDomain.APP
    # ...but a counter keyed on them would create one series per task attempt.
    assert starts == [{
        "af.workflow_task.target_type": "tool",
        "af.workflow_task.max_attempts": 3,
        "af.workflow_task.continue_on_error": True,
        "af.workflow_task.retry_driver": "durable",
    }]
    assert outcomes == [{
        "af.workflow_task.target_type": "tool",
        "af.workflow_task.max_attempts": 3,
        "af.workflow_task.continue_on_error": True,
        "af.workflow_task.retry_driver": "durable",
        "af.workflow_task.outcome_kind": "handler_transient",
        "af.workflow_task.disposition": "request_durable_retry",
    }]


def test_workflow_task_telemetry_leaves_a_successful_delivery_unflagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, outcomes, attributes = _install_workflow_task_metrics(monkeypatch)

    with obs.workflow_task_activity_telemetry({
        "af.workflow_task.target_type": "sub_agent",
    }) as telemetry:
        telemetry.complete(outcome_kind="success", disposition="return_result")

    assert obs.ATTR_FAULT_DOMAIN not in attributes
    assert "af.workflow_task.error_code" not in attributes
    assert outcomes[0]["af.workflow_task.outcome_kind"] == "success"


def test_workflow_task_telemetry_sanitizes_attribute_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, attributes = _install_workflow_task_metrics(monkeypatch)

    with obs.workflow_task_activity_telemetry({
        "af.workflow_task.target_name": "bad\nname\x00here",
        "af.workflow_task.workflow_id": "w" * 400,
        "af.workflow_task.timeout_ms": None,
    }) as telemetry:
        telemetry.complete(outcome_kind="success", disposition="return_result")

    assert attributes["af.workflow_task.target_name"] == "bad name here"
    assert len(str(attributes["af.workflow_task.workflow_id"])) == 129
    assert "af.workflow_task.timeout_ms" not in attributes


def test_bounded_attribute_collapses_control_characters_and_caps_length() -> None:
    assert obs.bounded_attribute("  tidy\tvalue  ") == "tidy value"
    assert obs.bounded_attribute("x" * 200).endswith("…")


def test_workflow_counter_creation_failure_does_not_clear_other_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_framework import observability as maf_observability

    created: dict[str, object] = {}

    class _Meter:
        def create_counter(self, name: str, *, description: str) -> object:
            if name == "azure_functions_agents.workflow_task.attempts":
                raise RuntimeError("unsupported instrument")
            counter = object()
            created[name] = counter
            return counter

    monkeypatch.setattr(obs, "_metrics_ready", False)
    monkeypatch.setattr(maf_observability, "get_meter", lambda: _Meter())

    obs._ensure_metrics()

    assert obs._workflow_task_start_counter is None
    assert (
        obs._workflow_task_completion_counter
        is created["azure_functions_agents.workflow_task.outcomes"]
    )
    assert (
        obs._sandbox_execution_counter
        is created["azure_functions_agents.dynamic_session.executions"]
    )
    assert obs._web_request_counter is created["azure_functions_agents.web_request.requests"]
    assert obs._delegate_call_counter is created["azure_functions_agents.delegate.calls"]


def test_record_sandbox_execution_gated_when_disabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []
    monkeypatch.setattr(obs, "_metrics_ready", True)
    monkeypatch.setattr(
        obs,
        "_sandbox_execution_counter",
        types.SimpleNamespace(add=lambda *a, **k: calls.append("x")),
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


# --- delegation (FRD 0007) observability --------------------------------------------------------


def test_fault_domain_delegate_value() -> None:
    assert obs.FaultDomain.DELEGATE == "delegate"


def test_current_span_is_noop_when_disabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(obs, "_enabled", False)
    span = obs.current_span()
    assert span._span is None
    # No-op span must stay safe to call even though it wraps nothing.
    span.set_attribute("af.delegate.specialist", "billing")
    span.set_error("boom", fault_domain=obs.FaultDomain.DELEGATE)


def test_current_span_wraps_active_span_when_enabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(obs, "_enabled", True)
    with obs.start_span("unit.test.parent", lifecycle_stage=obs.LifecycleStage.AGENT_RUN):
        span = obs.current_span()
        assert span._span is not None  # wraps the already-active span, not a new one


def test_record_delegate_call_gated_when_disabled(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []
    monkeypatch.setattr(obs, "_metrics_ready", True)
    monkeypatch.setattr(
        obs, "_delegate_call_counter", types.SimpleNamespace(add=lambda *a, **k: calls.append("c"))
    )
    monkeypatch.setattr(
        obs, "_delegate_error_counter", types.SimpleNamespace(add=lambda *a, **k: calls.append("e"))
    )

    monkeypatch.setattr(obs, "_enabled", False)
    obs.record_delegate_call(error=True)
    assert calls == []  # gated when disabled

    monkeypatch.setattr(obs, "_enabled", True)
    obs.record_delegate_call(error=True)
    assert calls == ["c", "e"]  # emitted when enabled


def test_record_delegate_call_only_increments_error_counter_when_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []
    monkeypatch.setattr(obs, "_metrics_ready", True)
    monkeypatch.setattr(
        obs, "_delegate_call_counter", types.SimpleNamespace(add=lambda *a, **k: calls.append("c"))
    )
    monkeypatch.setattr(
        obs, "_delegate_error_counter", types.SimpleNamespace(add=lambda *a, **k: calls.append("e"))
    )
    monkeypatch.setattr(obs, "_enabled", True)

    obs.record_delegate_call(error=False)

    assert calls == ["c"]  # call counter always increments; error counter only when error=True


def test_record_delegate_call_is_safe() -> None:
    obs.record_delegate_call(error=False)
    obs.record_delegate_call(error=True)


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
