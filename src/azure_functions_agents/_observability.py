"""OpenTelemetry bootstrap and telemetry conventions for the Agent Runtime.

Low/no-code observability. The app enables telemetry by installing the optional ``[monitor]``
exporter extra (``azurefunctions-agents-runtime[monitor]``) and setting the standard Azure Functions
``APPLICATIONINSIGHTS_CONNECTION_STRING`` — no app code required. The app's ``function_app.py`` stays
a two-line file; this module owns:

* configuring the optional Azure Monitor exporter;
* the span/attribute conventions the rest of the runtime uses so failures self-classify as
  ``app`` vs ``runtime`` vs ``platform`` (see :data:`ATTR_FAULT_DOMAIN`);
* a single resolved ``capture_sensitive_data`` flag (from ``ENABLE_SENSITIVE_DATA``) that gates
  whether prompts, payloads, tool arguments, code, and model output are attached to telemetry
  (default off).

Everything degrades to a no-op when OpenTelemetry is unavailable or no telemetry provider is active,
so importing this module and calling its helpers is always safe.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, TypedDict

from opentelemetry.util.types import AttributeValue

from ._logger import logger
from .config.env import EnvVar, _to_bool, runtime_env_value

if TYPE_CHECKING:
    from opentelemetry.context import Context

# ---------------------------------------------------------------------------
# Conventions
# ---------------------------------------------------------------------------
#
# Custom runtime attributes use the ``af.`` prefix — short for "Azure Functions agents".
# Standard GenAI attributes are reused when their semantics match the reported value. A few
# ``af.`` sub-namespaces group the details:
#
#   * ``af.agent.*``            — attributes on the per-run ``agent.run {name}`` span.
#   * ``af.dynamic_session.*``  — attributes on the ``dynamic_session.execute`` (sandbox) span.
#   * ``af.delegate.*``         — attributes on the ``execute_tool delegate_<slug>`` span added
#                                 for a chat-time sub-agent delegation (FRD 0007).
#
# Fault domain, lifecycle stage, and operation id are cross-cutting ``af.*`` attributes (below).
# Shared run-attribute keys are defined once here so both harnesses emit the same names.

#: Attribute that classifies which layer a failure belongs to (set only on failing spans).
ATTR_FAULT_DOMAIN = "af.fault_domain"
#: Attribute that records which run-lifecycle stage a span represents.
ATTR_LIFECYCLE_STAGE = "af.lifecycle_stage"
#: Correlation id (the active trace id) propagated to ACA so its telemetry lines up with the run.
ATTR_OPERATION_ID = "af.operation_id"

# Run attributes shared by registration surfaces and both execution harnesses.
ATTR_AGENT_NAME = "af.agent.name"
ATTR_AGENT_DISPLAY_NAME = "af.agent.display_name"
ATTR_AGENT_TRIGGER_TYPE = "af.agent.trigger_type"
ATTR_AGENT_SESSION_ID = "af.agent.session_id"
ATTR_AGENT_MODEL = "af.agent.model"
ATTR_AGENT_HARNESS = "af.agent.harness"
ATTR_AGENT_OUTCOME = "af.agent.outcome"
ATTR_AGENT_TOOL_CALL_COUNT = "af.agent.tool_call_count"
ATTR_AGENT_TOOL_ERROR_COUNT = "af.agent.tool_error_count"
ATTR_AGENT_INPUT_BYTES = "af.agent.input_bytes"
ATTR_AGENT_RESPONSE_BYTES = "af.agent.response_bytes"
ATTR_ERROR_TYPE = "error.type"
ATTR_GEN_AI_AGENT_DESCRIPTION = "gen_ai.agent.description"
ATTR_GEN_AI_AGENT_ID = "gen_ai.agent.id"
ATTR_GEN_AI_AGENT_NAME = "gen_ai.agent.name"
ATTR_GEN_AI_CONVERSATION_ID = "gen_ai.conversation.id"
ATTR_GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
ATTR_GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
ATTR_GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"
ATTR_GEN_AI_TOOL_NAME = "gen_ai.tool.name"
ATTR_TOOL_NAME = "af.tool.name"
ATTR_TOOL_SOURCE = "af.tool.source"
ATTR_TOOL_OUTCOME = "af.tool.outcome"

# Standard GenAI attributes are used only where the reported value has the same meaning.
ATTR_GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
ATTR_GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
ATTR_GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
ATTR_GEN_AI_USAGE_REASONING_OUTPUT_TOKENS = "gen_ai.usage.reasoning.output_tokens"
ATTR_GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS = "gen_ai.usage.cache_read.input_tokens"
ATTR_GEN_AI_USAGE_CACHE_WRITE_INPUT_TOKENS = "gen_ai.usage.cache_write.input_tokens"
ATTR_MCP_METHOD_NAME = "mcp.method.name"
MCP_METHOD_TOOLS_CALL = "tools/call"


class SpanOutcome(StrEnum):
    """Common outcome values used on agent and tool spans."""

    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"


class ToolSpanSource(StrEnum):
    """Origin of a runtime tool execution span."""

    WRAPPER = "wrapper"
    NATIVE = "native"


class GenAiOperationName(StrEnum):
    """Pinned GenAI operation values used when semantics match SDK events."""

    INVOKE_AGENT = "invoke_agent"
    EXECUTE_TOOL = "execute_tool"


_TRACER_NAME = "azure.functions.AgentRuntime"
_TELEMETRY_NAME_PATTERN = re.compile(r"[^A-Za-z0-9_.-]")
TELEMETRY_NAME_MAX_LENGTH = 128

type TelemetryAttributeValue = AttributeValue | None
type TelemetryAttributes = Mapping[str, TelemetryAttributeValue]


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


def gen_ai_agent_name(agent_slug: str | None, *, default: str | None = None) -> str | None:
    """Return the app-qualified GenAI agent name without changing the runtime slug."""
    site_name = runtime_env_value(EnvVar.WEBSITE_SITE_NAME)
    if not site_name:
        return agent_slug
    name = agent_slug or default
    return f"{site_name}/{name}" if name else None


def safe_telemetry_name(value: str | None) -> str | None:
    """Return a bounded telemetry-safe name for tool and skill metadata."""
    if not value:
        return None
    return _TELEMETRY_NAME_PATTERN.sub("_", value)[:TELEMETRY_NAME_MAX_LENGTH]


def agent_run_span_name(*, agent_name: str | None) -> str:
    """Return the harness-neutral runtime invocation span name."""
    return f"agent.run {agent_name or 'agent'}"


def agent_run_attributes(
    *,
    agent_name: str | None,
    display_name: str | None,
    trigger_type: str | None,
    session_id: str | None,
    model: str | None,
    harness: str | None = None,
) -> dict[str, TelemetryAttributeValue]:
    """Build harness-neutral invocation attributes while leaving trigger names extensible."""
    return {
        ATTR_AGENT_NAME: agent_name,
        ATTR_AGENT_DISPLAY_NAME: display_name,
        ATTR_AGENT_TRIGGER_TYPE: trigger_type,
        ATTR_AGENT_SESSION_ID: session_id,
        ATTR_AGENT_MODEL: model,
        ATTR_AGENT_HARNESS: harness,
    }


def set_agent_session_attribute(span: RuntimeSpan, *, session_id: str | None) -> None:
    """Set the shared runtime session attribute on one invocation span."""
    span.set_attribute(ATTR_AGENT_SESSION_ID, session_id)


def error_type_for_exception(exc: BaseException) -> str | None:
    """Return a bounded `error.type` value for runtime-owned telemetry."""
    if isinstance(exc, asyncio.CancelledError):
        return None
    if isinstance(exc, TimeoutError):
        return "timeout"
    return type(exc).__name__ or "_OTHER"


def exception_status_description(exc: BaseException, *, include_exception: bool) -> str:
    """Return the status description emitted for a recorded exception."""
    return repr(exc) if include_exception else ""


def _resolve_capture_sensitive_data() -> bool:
    """Resolve the runtime content-capture flag from ``ENABLE_SENSITIVE_DATA``.

    The setting is shared by runtime-owned and selected-harness telemetry, and defaults off.
    """
    value = runtime_env_value(EnvVar.ENABLE_SENSITIVE_DATA)
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

    connection = runtime_env_value(EnvVar.APPLICATIONINSIGHTS_CONNECTION_STRING)
    if connection:
        _configure_azure_monitor(connection)

    _enabled = _otel_provider_already_configured()
    _configured = True

    if _enabled:
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
            "skipping the runtime's Azure Monitor setup to avoid duplicate export. Runtime "
            "instrumentation will use the existing provider."
        )
        return
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
    except ImportError:
        # The optional [monitor] exporter is not installed. Do not raise or claim a host fallback:
        # host.json telemetryMode exports only *host* telemetry, not the runtime's worker spans. The
        # caller detects that no provider became active and emits an actionable warning.
        return
    kwargs: dict[str, Any] = {"connection_string": connection_string}
    if runtime_env_value(EnvVar.APPLICATIONINSIGHTS_AUTHENTICATION_STRING):
        # Unlike the other exporters, Live Metrics (QuickPulse) doesn't resolve AAD auth from this
        # env var, so it 401s repeatedly when the App Insights resource requires AAD. Disable it
        # here — this only drops the real-time Portal view, not telemetry export. Remove once
        # fixed upstream: https://github.com/Azure/azure-sdk-for-python/issues/48251
        kwargs["enable_live_metrics"] = False
    try:
        configure_azure_monitor(**kwargs)
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
    """Return the runtime's SDK-neutral OpenTelemetry tracer."""
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


def capture_current_context() -> Context | None:
    """Capture the current OTel context only when it contains a valid span."""
    try:
        from opentelemetry import trace
        from opentelemetry.context import get_current

        context = get_current()
        if trace.get_current_span(context).get_span_context().is_valid:
            return context
    except Exception:  # pragma: no cover - defensive
        pass
    return None


def bounded_content(value: str) -> str:
    """Trim content attached to telemetry so cost/PII blast radius stays capped."""
    if len(value) <= _CONTENT_ATTR_MAX_CHARS:
        return value
    return value[:_CONTENT_ATTR_MAX_CHARS] + "…[truncated]"


def current_span() -> RuntimeSpan:
    """Wrap the active OTel span without starting a child span."""
    if not _enabled:
        return RuntimeSpan(None)
    try:
        from opentelemetry import trace

        return RuntimeSpan(trace.get_current_span())
    except Exception:  # pragma: no cover - defensive
        return RuntimeSpan(None)


class RuntimeSpan:
    """Thin wrapper over an OTel span that no-ops when tracing is unavailable."""

    __slots__ = ("_cancelled", "_error_recorded", "_span")

    def __init__(self, span: Any | None) -> None:
        self._span = span
        self._cancelled = False
        self._error_recorded = False

    def set_attribute(self, key: str, value: TelemetryAttributeValue) -> None:
        if self._span is None or value is None:
            return
        if key in (ATTR_AGENT_OUTCOME, ATTR_TOOL_OUTCOME) and value in (
            SpanOutcome.CANCELLED,
            SpanOutcome.CANCELLED.value,
        ):
            self._cancelled = True
        with suppress(Exception):  # pragma: no cover - defensive
            self._span.set_attribute(key, value)

    def set_content(self, key: str, value: str) -> None:
        """Attach content only when ``capture_sensitive_data`` is enabled (bounded)."""
        if not _capture_sensitive_data:
            return
        self.set_attribute(key, bounded_content(value))

    def add_event(self, name: str, attributes: TelemetryAttributes | None = None) -> None:
        """Add a span event (a timestamped milestone) — no-op when tracing is unavailable."""
        if self._span is None:
            return
        with suppress(Exception):  # pragma: no cover - defensive
            attrs = {k: v for k, v in (attributes or {}).items() if v is not None}
            self._span.add_event(name, attributes=attrs or None)

    def record_exception(
        self,
        exc: BaseException,
        *,
        fault_domain: str | None = None,
        include_exception: bool = True,
        error_type: str | None = None,
        infer_error_type: bool = False,
    ) -> None:
        if self._span is None:
            return
        try:
            from opentelemetry.trace import Status, StatusCode

            if include_exception:
                self._span.record_exception(exc)
            self._span.set_status(
                Status(
                    StatusCode.ERROR,
                    exception_status_description(exc, include_exception=include_exception),
                )
            )
            self._span.set_attribute(ATTR_FAULT_DOMAIN, fault_domain or FaultDomain.UNKNOWN)
            resolved_error_type = (
                error_type if error_type is not None
                else error_type_for_exception(exc) if infer_error_type
                else None
            )
            if resolved_error_type:
                self._span.set_attribute(ATTR_ERROR_TYPE, resolved_error_type)
            self._error_recorded = True
        except Exception:  # pragma: no cover - defensive
            pass

    def set_error(
        self,
        message: str,
        *,
        fault_domain: str,
        error_type: str | None = None,
    ) -> None:
        if self._span is None:
            return
        try:
            from opentelemetry.trace import Status, StatusCode

            self._span.set_status(Status(StatusCode.ERROR, message))
            self._span.set_attribute(ATTR_FAULT_DOMAIN, fault_domain)
            if error_type is not None:
                self._span.set_attribute(ATTR_ERROR_TYPE, error_type)
            self._error_recorded = True
        except Exception:  # pragma: no cover - defensive
            pass


class _StartSpanOptions(TypedDict, total=False):
    context: Context
    record_exception: bool
    set_status_on_exception: bool


@contextmanager
def start_span(
    name: str,
    *,
    fault_domain: str | None = None,
    lifecycle_stage: str | None = None,
    attributes: TelemetryAttributes | None = None,
    parent_context: Context | None = None,
    record_exception: bool = True,
) -> Iterator[RuntimeSpan]:
    """Start a current span, degrading to a no-op when no telemetry provider is active or tracing is unavailable.

    ``fault_domain`` is the domain attributed if the span exits with an exception. It is not set on
    success — only failing spans carry :data:`ATTR_FAULT_DOMAIN`. ``parent_context`` overrides the
    active context when provided. ``record_exception=False`` retains error status and fault-domain
    attribution without recording exception details.
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
            span_options: _StartSpanOptions = {}
            if parent_context is not None:
                span_options["context"] = parent_context
            if not record_exception:
                span_options["record_exception"] = False
                span_options["set_status_on_exception"] = False
            manager = tracer.start_as_current_span(name, **span_options)
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
        suppress_cancellation_error = (
            isinstance(exc, asyncio.CancelledError) and span._cancelled
        )
        if not suppress_cancellation_error and not span._error_recorded:
            span.record_exception(
                exc,
                fault_domain=fault_domain,
                include_exception=record_exception,
            )
        if manager is not None:
            with suppress(Exception):  # pragma: no cover - defensive
                if suppress_cancellation_error:
                    manager.__exit__(None, None, None)
                else:
                    manager.__exit__(type(exc), exc, exc.__traceback__)
            manager = None
        raise
    finally:
        if manager is not None:
            with suppress(Exception):  # pragma: no cover - defensive
                manager.__exit__(None, None, None)


def record_timed_span(
    name: str,
    *,
    parent_context: Context | None,
    start_time_ns: int,
    end_time_ns: int,
    lifecycle_stage: str | None = None,
    attributes: TelemetryAttributes | None = None,
    status_description: str | None = None,
    fault_domain: str | None = None,
    error_type: str | None = None,
) -> bool:
    """Emit a span with explicit parent and epoch-nanosecond timing; return whether it was emitted."""

    def diagnose(reason: str) -> None:
        with suppress(Exception):  # pragma: no cover - defensive
            logger.warning("Timed OpenTelemetry span emission skipped: %s", reason)

    if (
        isinstance(start_time_ns, bool)
        or not isinstance(start_time_ns, int)
        or isinstance(end_time_ns, bool)
        or not isinstance(end_time_ns, int)
        or end_time_ns <= start_time_ns
    ):
        diagnose("invalid time interval")
        return False
    if not _enabled:
        diagnose("observability is disabled")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.context import Context as OtelContext

        if not isinstance(parent_context, OtelContext):
            diagnose("missing or invalid parent context")
            return False
        if not trace.get_current_span(parent_context).get_span_context().is_valid:
            diagnose("missing or invalid parent span")
            return False

        tracer = get_tracer()
        if tracer is None:
            diagnose("tracer unavailable")
            return False
        span_attributes: dict[str, TelemetryAttributeValue] = dict(attributes or {})
        if lifecycle_stage is not None:
            span_attributes[ATTR_LIFECYCLE_STAGE] = lifecycle_stage
        span = tracer.start_span(
            name,
            context=parent_context,
            start_time=start_time_ns,
            attributes=span_attributes or None,
        )
        if status_description is not None:
            from opentelemetry.trace import Status, StatusCode

            span.set_status(Status(StatusCode.ERROR, status_description))
            if fault_domain is not None:
                span.set_attribute(ATTR_FAULT_DOMAIN, fault_domain)
            if error_type is not None:
                span.set_attribute(ATTR_ERROR_TYPE, error_type)
        span.end(end_time=end_time_ns)
    except Exception:  # pragma: no cover - defensive
        diagnose("OpenTelemetry span emission failed")
        return False
    return True


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
    recoverable error string for the coordinator (FRD 0007 Decision #12). It
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
