from __future__ import annotations

import asyncio
import logging
import types
from contextlib import nullcontext

import pytest

import azure_functions_agents._observability as obs


def test_agent_run_attributes_preserve_extensible_trigger_values(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("WEBSITE_SITE_NAME", raising=False)

    attributes = obs.agent_run_attributes(
        agent_name="billing",
        display_name="Billing assistant",
        trigger_type="future_connector_trigger",
        session_id="session-123",
        model="model-name",
    )

    assert attributes == {
        obs.ATTR_AGENT_NAME: "billing",
        "af.agent.display_name": "Billing assistant",
        obs.ATTR_AGENT_TRIGGER_TYPE: "future_connector_trigger",
        obs.ATTR_AGENT_SESSION_ID: "session-123",
        obs.ATTR_AGENT_MODEL: "model-name",
        "af.agent.harness": None,
    }


@pytest.mark.parametrize("harness", ["maf", "copilot"])
def test_agent_run_attributes_use_agent_harness_namespace(harness: str) -> None:
    attributes = obs.agent_run_attributes(
        agent_name="billing",
        display_name=None,
        trigger_type="http",
        session_id=None,
        model=None,
        harness=harness,
    )

    assert attributes["af.agent.harness"] == harness
    assert "af.harness" not in attributes


def test_agent_run_span_name_uses_legacy_runtime_name() -> None:
    assert obs.agent_run_span_name(agent_name="billing") == "agent.run billing"


def test_common_span_outcome_and_tool_source_values() -> None:
    assert [outcome.value for outcome in obs.SpanOutcome] == [
        "success",
        "error",
        "cancelled",
    ]
    assert obs.ToolSpanSource.WRAPPER.value == "wrapper"
    assert obs.ToolSpanSource.NATIVE.value == "native"


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

    resolved = obs.configure_observability()

    assert resolved.enabled is True
    assert resolved.capture_sensitive_data is True
    assert obs.capture_sensitive_data() is True
    assert obs.is_observability_enabled() is True


def test_configure_observability_noop_without_provider_or_connection(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _clear_env(monkeypatch)
    _reset_bootstrap(monkeypatch)
    monkeypatch.setattr(obs, "_otel_provider_already_configured", lambda: False)

    resolved = obs.configure_observability()

    assert resolved.enabled is False
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
    with caplog.at_level(logging.WARNING, logger="azure.functions.AgentRuntime"):
        resolved = obs.configure_observability()

    assert resolved.enabled is False
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


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "error", "cancelled"])
async def test_explicit_run_span_binding_is_task_local_and_restored(outcome) -> None:
    from opentelemetry import trace
    from opentelemetry.trace import NonRecordingSpan, SpanContext

    def make_span(number: int):
        return NonRecordingSpan(
            SpanContext(trace_id=number, span_id=number, is_remote=False),
        )

    raw_parent = make_span(1)
    parent = obs.RuntimeSpan(raw_parent)
    assert obs.current_agent_run_span() is None
    with trace.use_span(raw_parent), obs.bind_agent_run_span(parent):
        assert obs.current_agent_run_span() is parent

        async def child(number: int) -> None:
            assert obs.current_agent_run_span() is parent
            raw_child = make_span(number)
            owned_child = obs.RuntimeSpan(raw_child)
            with trace.use_span(raw_child):
                assert obs.current_agent_run_span() is None
                with obs.bind_agent_run_span(owned_child):
                    await asyncio.sleep(0)
                    assert obs.current_agent_run_span() is owned_child
                assert obs.current_agent_run_span() is None
            assert obs.current_agent_run_span() is parent

        await asyncio.gather(child(2), child(3))
        assert obs.current_agent_run_span() is parent
        raw_nested = make_span(4)
        manager = (
            pytest.raises(ValueError if outcome == "error" else asyncio.CancelledError)
            if outcome != "success" else nullcontext()
        )
        with manager, trace.use_span(raw_nested), obs.bind_agent_run_span(obs.RuntimeSpan(raw_nested)):
            if outcome == "error":
                raise ValueError("failure")
            if outcome == "cancelled":
                raise asyncio.CancelledError()
        assert obs.current_agent_run_span() is parent
    assert obs.current_agent_run_span() is None


def test_noop_run_binding_does_not_select_an_unrelated_span() -> None:
    with obs.bind_agent_run_span(obs.RuntimeSpan(None)):
        assert obs.current_agent_run_span() is None


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


def test_record_agent_file_discovery_tags_normalized_suffix(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[int, dict[str, str]]] = []
    monkeypatch.setattr(obs, "_metrics_ready", True)
    monkeypatch.setattr(obs, "_enabled", True)
    monkeypatch.setattr(
        obs,
        "_agent_file_discovery_counter",
        types.SimpleNamespace(add=lambda value, attributes: calls.append((value, attributes))),
    )

    obs.record_agent_file_discovery(suffix=".claude.md")

    assert calls == [(1, {"af.agent.file_suffix": ".claude.md"})]


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


def _install_test_tracer(monkeypatch):  # type: ignore[no-untyped-def]
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(obs, "_enabled", True)
    monkeypatch.setattr(obs, "get_tracer", lambda: provider.get_tracer("observability.tests"))
    return provider, exporter


def test_capture_current_context_requires_valid_active_span(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    provider, _exporter = _install_test_tracer(monkeypatch)
    assert obs.capture_current_context() is None

    with provider.get_tracer("observability.tests").start_as_current_span("active"):
        context = obs.capture_current_context()
        assert context is not None
        from opentelemetry import trace

        assert trace.get_current_span(context).get_span_context().is_valid
    assert obs.capture_current_context() is None


def test_start_span_uses_explicit_parent_context_across_context_switches(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    provider, exporter = _install_test_tracer(monkeypatch)
    tracer = provider.get_tracer("context.switch.tests")
    with tracer.start_as_current_span("captured.parent") as captured_parent:
        captured_context = obs.capture_current_context()
        assert captured_context is not None
        captured_parent_id = captured_parent.get_span_context().span_id

    with tracer.start_as_current_span("ambient.parent"), obs.start_span(
        "explicit.child", parent_context=captured_context
    ) as child:
        assert child._span is not None
        assert child._span.parent is not None
        assert child._span.parent.span_id == captured_parent_id

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["explicit.child"].parent is not None
    assert spans["explicit.child"].parent.span_id == captured_parent_id


def test_record_timed_span_preserves_parent_and_epoch_nanosecond_timing(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    provider, exporter = _install_test_tracer(monkeypatch)
    tracer = provider.get_tracer("timed.span.tests")
    start_time_ns = 1_800_000_000_000_000_000
    end_time_ns = start_time_ns + 250_000_000

    with tracer.start_as_current_span("timed.parent") as parent:
        parent_context = obs.capture_current_context()
        assert parent_context is not None
        parent_span_id = parent.get_span_context().span_id
        assert obs.record_timed_span(
            "timed.child",
            parent_context=parent_context,
            start_time_ns=start_time_ns,
            end_time_ns=end_time_ns,
            lifecycle_stage=obs.LifecycleStage.AGENT_RUN,
            attributes={"kind": "copilot"},
        )

    spans = {span.name: span for span in exporter.get_finished_spans()}
    child = spans["timed.child"]
    assert child.parent is not None
    assert child.parent.span_id == parent_span_id
    assert child.start_time == start_time_ns
    assert child.end_time == end_time_ns
    assert child.attributes["kind"] == "copilot"
    assert child.attributes[obs.ATTR_LIFECYCLE_STAGE] == obs.LifecycleStage.AGENT_RUN


@pytest.mark.parametrize(
    ("start_time_ns", "end_time_ns"),
    [
        (100, 100),
        (200, 100),
        (0.5, 10),
        (True, 10),
        (100, 1.5),
    ],
)
def test_record_timed_span_rejects_invalid_intervals(
    monkeypatch, caplog, start_time_ns, end_time_ns
) -> None:  # type: ignore[no-untyped-def]
    from opentelemetry.context import Context

    _provider, exporter = _install_test_tracer(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="azure.functions.AgentRuntime"):
        result = obs.record_timed_span(
            "invalid.interval",
            parent_context=Context(),
            start_time_ns=start_time_ns,
            end_time_ns=end_time_ns,
        )

    assert result is False
    assert "invalid time interval" in caplog.text
    assert not exporter.get_finished_spans()


def test_record_timed_span_rejects_missing_or_invalid_context(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    from opentelemetry.context import Context

    _provider, exporter = _install_test_tracer(monkeypatch)
    for invalid_context in (None, Context()):
        with caplog.at_level(logging.WARNING, logger="azure.functions.AgentRuntime"):
            result = obs.record_timed_span(
                "invalid.context",
                parent_context=invalid_context,
                start_time_ns=100,
                end_time_ns=200,
            )

        assert result is False
        assert "invalid parent" in caplog.text
    assert not exporter.get_finished_spans()


def test_record_timed_span_logs_content_free_diagnostic_on_otel_failure(
    monkeypatch, caplog
) -> None:  # type: ignore[no-untyped-def]
    provider, _exporter = _install_test_tracer(monkeypatch)
    with provider.get_tracer("failure.tests").start_as_current_span("parent"):
        parent_context = obs.capture_current_context()
    assert parent_context is not None

    class _FailedTracer:
        def start_span(self, *_args, **_kwargs):
            raise RuntimeError("private exception details")

    monkeypatch.setattr(obs, "get_tracer", lambda: _FailedTracer())
    with caplog.at_level(logging.WARNING, logger="azure.functions.AgentRuntime"):
        emitted = obs.record_timed_span(
            "failed.emission",
            parent_context=parent_context,
            start_time_ns=100,
            end_time_ns=200,
        )

    assert emitted is False
    assert "OpenTelemetry span emission failed" in caplog.text
    assert "private exception details" not in caplog.text


def test_start_span_exception_recording_defaults_to_existing_behavior(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _provider, exporter = _install_test_tracer(monkeypatch)
    with pytest.raises(ValueError, match="sensitive details"), obs.start_span(
        "default.exception"
    ):
        raise ValueError("sensitive details")

    span = next(span for span in exporter.get_finished_spans() if span.name == "default.exception")
    exception_events = [event for event in span.events if event.name == "exception"]
    assert exception_events
    assert exception_events[0].attributes["exception.message"] == "sensitive details"
    assert span.status.description == "ValueError: sensitive details"


def test_start_span_can_suppress_raw_exception_recording(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _provider, exporter = _install_test_tracer(monkeypatch)
    with pytest.raises(ValueError, match="sensitive details"), obs.start_span(
        "content.free.exception",
        fault_domain=obs.FaultDomain.RUNTIME,
        record_exception=False,
    ):
        raise ValueError("sensitive details")

    span = next(
        span for span in exporter.get_finished_spans() if span.name == "content.free.exception"
    )
    assert all(event.name != "exception" for event in span.events)
    assert span.status.status_code.name == "ERROR"
    assert span.status.description in (None, "")
    assert span.attributes[obs.ATTR_FAULT_DOMAIN] == obs.FaultDomain.RUNTIME
    assert obs.ATTR_ERROR_TYPE not in span.attributes


def test_record_exception_error_type_is_opt_in(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _provider, exporter = _install_test_tracer(monkeypatch)
    with obs.start_span("default.exception.type") as span:
        span.record_exception(ValueError("default"), fault_domain=obs.FaultDomain.RUNTIME)

    with obs.start_span("explicit.exception.type") as span:
        span.record_exception(
            RuntimeError("opt in"),
            fault_domain=obs.FaultDomain.RUNTIME,
            infer_error_type=True,
        )

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert obs.ATTR_ERROR_TYPE not in spans["default.exception.type"].attributes
    assert spans["explicit.exception.type"].attributes[obs.ATTR_ERROR_TYPE] == "RuntimeError"


def test_start_span_preserves_cancelled_outcome_without_error_status(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _provider, exporter = _install_test_tracer(monkeypatch)
    with pytest.raises(asyncio.CancelledError, match="private cancellation"), obs.start_span(
        "cancelled.span",
        fault_domain=obs.FaultDomain.RUNTIME,
        record_exception=False,
    ) as span:
        span.set_attribute(obs.ATTR_AGENT_OUTCOME, obs.SpanOutcome.CANCELLED)
        raise asyncio.CancelledError("private cancellation")

    span = next(span for span in exporter.get_finished_spans() if span.name == "cancelled.span")
    assert span.attributes[obs.ATTR_AGENT_OUTCOME] == obs.SpanOutcome.CANCELLED
    assert span.status.status_code.name == "UNSET"
    assert obs.ATTR_FAULT_DOMAIN not in span.attributes
    assert all(event.name != "exception" for event in span.events)


def test_start_span_does_not_treat_delegate_outcome_as_cancellation_opt_in(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _provider, exporter = _install_test_tracer(monkeypatch)
    with pytest.raises(asyncio.CancelledError, match="private cancellation"), obs.start_span(
        "delegate.cancelled.span",
        fault_domain=obs.FaultDomain.DELEGATE,
        record_exception=False,
    ) as span:
        span.set_attribute("af.delegate.outcome", obs.SpanOutcome.CANCELLED)
        raise asyncio.CancelledError("private cancellation")

    span = next(
        span for span in exporter.get_finished_spans() if span.name == "delegate.cancelled.span"
    )
    assert span.attributes["af.delegate.outcome"] == obs.SpanOutcome.CANCELLED
    assert span.status.status_code.name == "ERROR"
    assert span.status.description in (None, "")
    assert span.attributes[obs.ATTR_FAULT_DOMAIN] == obs.FaultDomain.DELEGATE
    assert obs.ATTR_ERROR_TYPE not in span.attributes
    assert all(event.name != "exception" for event in span.events)
