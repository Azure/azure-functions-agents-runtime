"""OpenTelemetry bootstrap and telemetry conventions for the Agent Runtime.

Low/no-code observability. The app enables telemetry by installing the optional ``[monitor]``
exporter extra (``azurefunctions-agents-runtime[monitor]``) and setting the standard Azure Functions
``APPLICATIONINSIGHTS_CONNECTION_STRING`` — no app code required. The app's ``function_app.py`` stays
a two-line file; this module owns:

* turning on Microsoft Agent Framework (MAF) ``gen_ai`` instrumentation and, when the optional
  ``[monitor]`` exporter is installed, the Azure Monitor exporter;
* the span/attribute conventions the rest of the runtime uses so failures self-classify as
  ``app`` vs ``runtime`` vs ``platform`` (see :data:`ATTR_FAULT_DOMAIN`);
* a single resolved ``capture_sensitive_data`` flag (from MAF's ``ENABLE_SENSITIVE_DATA``) that gates
  whether prompts, payloads, tool arguments, code, and model output are attached to telemetry
  (default off).

Everything degrades to a no-op when OpenTelemetry is unavailable or no telemetry provider is active,
so importing this module and calling its helpers is always safe.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Any

from ._logger import logger
from .config.env import _to_bool, runtime_env_value

# ---------------------------------------------------------------------------
# Conventions
# ---------------------------------------------------------------------------
#
# Attribute naming: every attribute this runtime adds is prefixed ``af.`` — short for
# "Azure Functions agents". The prefix keeps our attributes from colliding with Microsoft Agent
# Framework's ``gen_ai.*`` attributes or OpenTelemetry semantic conventions, and makes them trivial
# to query ("everything we add starts with af."). A few sub-namespaces group the details:
#
#   * ``af.agent.*``            — attributes on the per-run ``agent.run {name}`` span.
#   * ``af.dynamic_session.*``  — attributes on the ``dynamic_session.execute`` (sandbox) span.
#   * ``af.delegate.*``         — attributes on the ``execute_tool delegate_<slug>`` span added
#                                 for a chat-time sub-agent delegation (FRD 0006).
#
# Three ``af.*`` attributes are cross-cutting and can appear on any runtime span: fault domain,
# lifecycle stage, and operation id (below). We reuse standard OTel semantic-convention attributes
# where they already exist (e.g. ``server.address`` for the session-pool host) instead of inventing
# an ``af.`` name.

#: Attribute that classifies which layer a failure belongs to (set only on failing spans).
ATTR_FAULT_DOMAIN = "af.fault_domain"
#: Attribute that records which run-lifecycle stage a span represents.
ATTR_LIFECYCLE_STAGE = "af.lifecycle_stage"
#: Correlation id (the active trace id) propagated to ACA so its telemetry lines up with the run.
ATTR_OPERATION_ID = "af.operation_id"

_TRACER_NAME = "azure.functions.AgentRuntime"


class FaultDomain:
    """Values for :data:`ATTR_FAULT_DOMAIN` — "whose fault is it?"."""

    APP = "app"
    RUNTIME = "runtime"
    PLATFORM = "platform"
    MODEL = "model"
    CONNECTOR = "connector"
    SANDBOX = "sandbox"
    WEB_REQUEST = "web_request"
    DELEGATE = "delegate"
    UNKNOWN = "unknown"


class LifecycleStage:
    """Values for :data:`ATTR_LIFECYCLE_STAGE` — the run-lifecycle stages from the gap map."""

    INDEX = "index"
    CLIENT_BUILD = "client_build"
    DISCOVERY = "discovery"
    HISTORY = "history"
    PROMPT_BUILD = "prompt_build"
    AGENT_RUN = "agent_run"
    TOOL_EXECUTION = "tool_execution"
    RESPONSE_POST = "response_post"
    DELIVERY = "delivery"


# ---------------------------------------------------------------------------
# Resolved configuration + bootstrap state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedObservability:
    """The effective observability settings after bootstrap.

    ``enabled`` reflects whether a real OpenTelemetry provider is active (so the runtime's spans and
    metrics are emitted); ``capture_sensitive_data`` gates attaching content to telemetry.
    """

    enabled: bool
    capture_sensitive_data: bool


_MAF_SENSITIVE_ENV = "ENABLE_SENSITIVE_DATA"
_CONNECTION_ENV = "APPLICATIONINSIGHTS_CONNECTION_STRING"

_CONTENT_ATTR_MAX_CHARS = 2048

_configured = False
_enabled = False
_capture_sensitive_data = False


def is_observability_enabled() -> bool:
    """Return whether runtime-owned telemetry (spans/metrics) should be emitted."""
    return _enabled


def capture_sensitive_data() -> bool:
    """Return whether sensitive content may be attached to telemetry (default False)."""
    return _capture_sensitive_data


def _resolve_capture_sensitive_data() -> bool:
    """Resolve the content-capture flag from Microsoft Agent Framework's ``ENABLE_SENSITIVE_DATA``.

    The runtime deliberately reuses MAF's own switch (default off) so a single environment variable
    governs both MAF ``gen_ai`` content and the runtime's ``af.*`` content, with no divergence and
    nothing to reconcile. See FRD 0003 (sensitive-data exposure decision).
    """
    value = runtime_env_value(_MAF_SENSITIVE_ENV)
    return _to_bool(value, default=False) if value else False


def configure_observability() -> ResolvedObservability:
    """Bootstrap runtime observability once and return the effective settings.

    Called from :func:`azure_functions_agents.app.create_function_app`. Enablement is driven by the
    optional ``[monitor]`` exporter plus the standard ``APPLICATIONINSIGHTS_CONNECTION_STRING`` —
    there is no separate config/enable flag:

    * Third-party log-noise control runs **unconditionally** — it is useful whether or not telemetry
      is exported, and only raises a logger whose own level is unset.
    * When a connection string is present the runtime configures the Azure Monitor exporter, but only
      if the ``[monitor]`` extra is installed and no OpenTelemetry provider is active yet.
    * Runtime spans/metrics are emitted only when a real OpenTelemetry provider is active — the one we
      just configured, or one the Functions worker/host already installed. Otherwise all helpers
      no-op.

    Idempotent: the bootstrap runs at most once per process. Always safe to call.
    """
    global _configured, _enabled, _capture_sensitive_data

    _capture_sensitive_data = _resolve_capture_sensitive_data()

    # Noise control is independent of export, so run it every time (idempotent; never overrides a
    # level set directly on a logger).
    _quiet_noisy_loggers()

    if _configured:
        return ResolvedObservability(
            enabled=_enabled, capture_sensitive_data=_capture_sensitive_data
        )

    connection = runtime_env_value(_CONNECTION_ENV)
    if connection:
        _configure_azure_monitor(connection)

    _enabled = _otel_provider_already_configured()
    _configured = True

    if _enabled:
        _enable_agent_framework_instrumentation(_capture_sensitive_data)
        logger.info("Observability enabled (capture_sensitive_data=%s)", _capture_sensitive_data)
    elif connection:
        logger.warning(
            "APPLICATIONINSIGHTS_CONNECTION_STRING is set but no OpenTelemetry exporter is active "
            "(the Azure Monitor exporter is not installed and no OpenTelemetry provider was found), "
            "so the runtime's spans and metrics will NOT be exported. Install "
            "'azurefunctions-agents-runtime[monitor]' to export telemetry to Application Insights."
        )
    else:
        logger.info("Observability inactive (no OpenTelemetry provider or exporter configured)")

    return ResolvedObservability(enabled=_enabled, capture_sensitive_data=_capture_sensitive_data)


def _enable_agent_framework_instrumentation(capture: bool) -> None:
    try:
        from agent_framework.observability import enable_instrumentation

        enable_instrumentation(enable_sensitive_data=capture)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not enable Agent Framework instrumentation: %s", exc)


def _otel_provider_already_configured() -> bool:
    """True when a real OpenTelemetry SDK TracerProvider is already installed in this process.

    The Functions Python worker configures Azure Monitor during worker init (before our
    create_function_app() runs) when PYTHON_APPLICATIONINSIGHTS_ENABLE_TELEMETRY /
    PYTHON_ENABLE_OPENTELEMETRY is set. In that case the runtime must NOT call
    configure_azure_monitor() again, or traces/metrics/logs double-export. On any error we
    return False so the runtime falls back to its normal setup (status quo).
    """
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
    except Exception:  # pragma: no cover - defensive
        return False
    # A real SDK TracerProvider means OTel is already set up; the opentelemetry-api default
    # before configuration is a ProxyTracerProvider (older versions: DefaultTracerProvider).
    try:
        from opentelemetry.sdk.trace import TracerProvider as _SdkTracerProvider

        if isinstance(provider, _SdkTracerProvider):
            return True
    except Exception:  # pragma: no cover - defensive
        pass
    return type(provider).__name__ not in (
        "ProxyTracerProvider",
        "DefaultTracerProvider",
        "NoOpTracerProvider",
    )


def _configure_azure_monitor(connection_string: str) -> None:
    if _otel_provider_already_configured():
        logger.info(
            "OpenTelemetry is already configured in this worker process (likely the Functions "
            "worker via PYTHON_APPLICATIONINSIGHTS_ENABLE_TELEMETRY / PYTHON_ENABLE_OPENTELEMETRY); "
            "skipping the runtime's Azure Monitor setup to avoid duplicate export. Runtime spans "
            "and Agent Framework gen_ai instrumentation will use the existing provider."
        )
        return
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
    except ImportError:
        # The optional [monitor] exporter is not installed. Do not raise or claim a host fallback:
        # host.json telemetryMode exports only *host* telemetry, not the runtime's worker spans. The
        # caller detects that no provider became active and emits an actionable warning.
        return
    try:
        configure_azure_monitor(connection_string=connection_string)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not configure the Azure Monitor exporter: %s", exc)


# Third-party loggers that flood Application Insights with low-signal HTTP request/response dumps,
# exporter self-logs ("Transmission succeeded…"), and credential chatter once a worker exporter is
# attached. Measured on a real app these dominated ingestion (>60% of trace bytes) while carrying
# almost no business signal. When observability is enabled we raise their threshold, but only when
# a level is not set directly on that logger (its level is NOTSET); a level set directly on the
# logger is preserved. A level set on a parent/root logger is intentionally not consulted.
_NOISY_LOGGERS: dict[str, int] = {
    "azure.core.pipeline.policies.http_logging_policy": logging.WARNING,
    "azure.monitor.opentelemetry.exporter": logging.WARNING,
    "azure.identity": logging.WARNING,
    "httpx": logging.WARNING,
    "urllib3": logging.WARNING,
    "opentelemetry.attributes": logging.ERROR,
    "opentelemetry.trace": logging.ERROR,
    "opentelemetry.metrics._internal": logging.ERROR,
    "opentelemetry._logs._internal": logging.ERROR,
    "opentelemetry.instrumentation.instrumentor": logging.ERROR,
}


def _quiet_noisy_loggers() -> None:
    """Raise the level of known-noisy third-party loggers (only when a level is not set directly on that logger)."""
    for name, level in _NOISY_LOGGERS.items():
        try:
            noisy = logging.getLogger(name)
            if noisy.level == logging.NOTSET:
                noisy.setLevel(level)
        except Exception:  # pragma: no cover - defensive
            pass


# ---------------------------------------------------------------------------
# Span helpers (no-op when OpenTelemetry is unavailable / disabled)
# ---------------------------------------------------------------------------


def get_tracer() -> Any:
    """Return an OpenTelemetry tracer, preferring MAF's shared provider."""
    try:
        from agent_framework.observability import get_tracer as _maf_get_tracer

        return _maf_get_tracer()
    except Exception:
        try:
            from opentelemetry import trace

            return trace.get_tracer(_TRACER_NAME)
        except Exception:  # pragma: no cover - OTel always present via agent-framework-core
            return None


def current_operation_id() -> str | None:
    """Return the active trace id as a 32-char hex operation id, or ``None``."""
    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        if context is not None and context.trace_id:
            return format(context.trace_id, "032x")
    except Exception:  # pragma: no cover - defensive
        return None
    return None


def bounded_content(value: str) -> str:
    """Trim content attached to telemetry so cost/PII blast radius stays capped."""
    if len(value) <= _CONTENT_ATTR_MAX_CHARS:
        return value
    return value[:_CONTENT_ATTR_MAX_CHARS] + "…[truncated]"


def current_span() -> RuntimeSpan:
    """Wrap whatever OTel span is already active, without starting a new one.

    Used by the ``delegate_<slug>`` tool adapter (``runner.build_subagent_tools``)
    to annotate the *existing* ``execute_tool delegate_<slug>`` span (opened by
    MAF's ``FunctionTool.invoke()``) with ``af.delegate.*`` attributes, rather
    than nesting a second span underneath it — see FRD 0006 §4.12, whose span
    diagram shows exactly one ``execute_tool delegate_<slug>`` span per
    delegation. Contrast with :func:`start_span`, which always creates a new
    span. Returns a no-op :class:`RuntimeSpan` when tracing is unavailable or
    disabled.
    """
    if not _enabled:
        return RuntimeSpan(None)
    try:
        from opentelemetry import trace

        return RuntimeSpan(trace.get_current_span())
    except Exception:  # pragma: no cover - defensive
        return RuntimeSpan(None)


class RuntimeSpan:
    """Thin wrapper over an OTel span that no-ops when tracing is unavailable."""

    __slots__ = ("_span",)

    def __init__(self, span: Any | None) -> None:
        self._span = span

    def set_attribute(self, key: str, value: Any) -> None:
        if self._span is None or value is None:
            return
        with suppress(Exception):  # pragma: no cover - defensive
            self._span.set_attribute(key, value)

    def set_content(self, key: str, value: str) -> None:
        """Attach content only when ``capture_sensitive_data`` is enabled (bounded)."""
        if not _capture_sensitive_data:
            return
        self.set_attribute(key, bounded_content(value))

    def add_event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        """Add a span event (a timestamped milestone) — no-op when tracing is unavailable."""
        if self._span is None:
            return
        with suppress(Exception):  # pragma: no cover - defensive
            attrs = {k: v for k, v in (attributes or {}).items() if v is not None}
            self._span.add_event(name, attributes=attrs or None)

    def record_exception(self, exc: BaseException, *, fault_domain: str | None = None) -> None:
        if self._span is None:
            return
        try:
            from opentelemetry.trace import Status, StatusCode

            self._span.record_exception(exc)
            self._span.set_status(Status(StatusCode.ERROR, str(exc)))
            self._span.set_attribute(ATTR_FAULT_DOMAIN, fault_domain or FaultDomain.UNKNOWN)
        except Exception:  # pragma: no cover - defensive
            pass

    def set_error(self, message: str, *, fault_domain: str) -> None:
        if self._span is None:
            return
        try:
            from opentelemetry.trace import Status, StatusCode

            self._span.set_status(Status(StatusCode.ERROR, message))
            self._span.set_attribute(ATTR_FAULT_DOMAIN, fault_domain)
        except Exception:  # pragma: no cover - defensive
            pass


@contextmanager
def start_span(
    name: str,
    *,
    fault_domain: str | None = None,
    lifecycle_stage: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[RuntimeSpan]:
    """Start a current span, degrading to a no-op when no telemetry provider is active or tracing is unavailable.

    ``fault_domain`` is the domain attributed if the span exits with an exception. It is not set on
    success — only failing spans carry :data:`ATTR_FAULT_DOMAIN`.
    """
    # Gate on the runtime's resolved state: `_enabled` is true only when a real OpenTelemetry
    # provider is active (the Azure Monitor exporter we configured, or one the Functions
    # worker/host already installed). When no provider is active, runtime-owned spans are suppressed
    # entirely rather than recorded into a no-op tracer.
    tracer = get_tracer() if _enabled else None
    manager: Any = None
    raw_span: Any = None
    if tracer is not None:
        try:
            manager = tracer.start_as_current_span(name)
            raw_span = manager.__enter__()
        except Exception:  # pragma: no cover - defensive
            manager = None
            raw_span = None

    span = RuntimeSpan(raw_span)
    if lifecycle_stage is not None:
        span.set_attribute(ATTR_LIFECYCLE_STAGE, lifecycle_stage)
    if attributes:
        for key, value in attributes.items():
            span.set_attribute(key, value)

    try:
        yield span
    except BaseException as exc:
        span.record_exception(exc, fault_domain=fault_domain)
        if manager is not None:
            with suppress(Exception):  # pragma: no cover - defensive
                manager.__exit__(type(exc), exc, exc.__traceback__)
            manager = None
        raise
    finally:
        if manager is not None:
            with suppress(Exception):  # pragma: no cover - defensive
                manager.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Metrics (minimal P0 set; broader fleet metrics are a follow-up)
# ---------------------------------------------------------------------------

_meter: Any = None
_sandbox_execution_counter: Any = None
_sandbox_error_counter: Any = None
_web_request_counter: Any = None
_web_request_error_counter: Any = None
_delegate_call_counter: Any = None
_delegate_error_counter: Any = None
_metrics_ready = False


def _ensure_metrics() -> None:
    global _meter, _sandbox_execution_counter, _sandbox_error_counter
    global _web_request_counter, _web_request_error_counter, _metrics_ready
    global _delegate_call_counter, _delegate_error_counter
    if _metrics_ready:
        return
    _metrics_ready = True
    try:
        from agent_framework.observability import get_meter

        _meter = get_meter()
    except Exception:
        try:
            from opentelemetry import metrics

            _meter = metrics.get_meter(_TRACER_NAME)
        except Exception:  # pragma: no cover - defensive
            _meter = None
    if _meter is None:
        return
    try:
        _sandbox_execution_counter = _meter.create_counter(
            "azure_functions_agents.dynamic_session.executions",
            description="ACA dynamic-session code executions.",
        )
        _sandbox_error_counter = _meter.create_counter(
            "azure_functions_agents.dynamic_session.errors",
            description="ACA dynamic-session executions that produced an error or stderr.",
        )
        _web_request_counter = _meter.create_counter(
            "azure_functions_agents.web_request.requests",
            description="web_request system tool invocations.",
        )
        _web_request_error_counter = _meter.create_counter(
            "azure_functions_agents.web_request.errors",
            description="web_request invocations blocked or failed (SSRF, timeout, transport error).",
        )
        _delegate_call_counter = _meter.create_counter(
            "azure_functions_agents.delegate.calls",
            description="delegate_<slug> tool invocations (chat-time sub-agent delegation).",
        )
        _delegate_error_counter = _meter.create_counter(
            "azure_functions_agents.delegate.errors",
            description="delegate_<slug> invocations that failed or timed out (specialist-side; sanitized before reaching the model).",
        )
    except Exception:  # pragma: no cover - defensive
        _sandbox_execution_counter = None
        _sandbox_error_counter = None
        _web_request_counter = None
        _web_request_error_counter = None
        _delegate_call_counter = None
        _delegate_error_counter = None


def record_sandbox_execution(*, error: bool) -> None:
    """Record one dynamic-session execution and, when ``error``, one failure."""
    if not _enabled:
        return
    _ensure_metrics()
    try:
        if _sandbox_execution_counter is not None:
            _sandbox_execution_counter.add(1)
        if error and _sandbox_error_counter is not None:
            _sandbox_error_counter.add(1)
    except Exception:  # pragma: no cover - defensive
        pass


def record_web_request(*, error: bool) -> None:
    """Record one ``web_request`` invocation and, when ``error``, one failure.

    ``error`` covers SSRF-blocked requests, timeouts, and transport failures —
    not application-level HTTP status codes (a 4xx/5xx response is still a
    successful tool invocation; the model sees the status).
    """
    if not _enabled:
        return
    _ensure_metrics()
    try:
        if _web_request_counter is not None:
            _web_request_counter.add(1)
        if error and _web_request_error_counter is not None:
            _web_request_error_counter.add(1)
    except Exception:  # pragma: no cover - defensive
        pass


def record_delegate_call(*, error: bool) -> None:
    """Record one ``delegate_<slug>`` invocation and, when ``error``, one failure.

    ``error`` covers a specialist run that failed, raised, or exceeded the
    effective delegation timeout — any outcome the adapter sanitized into a
    recoverable error string for the coordinator (FRD 0006 Decision #12). It
    does not cover parent/request cancellation, which propagates instead of
    being recorded as a delegate error.
    """
    if not _enabled:
        return
    _ensure_metrics()
    try:
        if _delegate_call_counter is not None:
            _delegate_call_counter.add(1)
        if error and _delegate_error_counter is not None:
            _delegate_error_counter.add(1)
    except Exception:  # pragma: no cover - defensive
        pass
