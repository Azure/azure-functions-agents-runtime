"""Validation helpers for configuration translation."""

from __future__ import annotations

import platform
import sys
from pathlib import Path
from typing import Any

from azure_functions_agents._logger import logger as _logger
from azure_functions_agents.sandbox_runtime_limits import (
    DEFAULT_RECONCILER_CADENCE_SECONDS,
    RECLAIM_SAFETY_GRACE_SECONDS,
    reclaim_exceeds_auto_delete_backstop,
)

from .http_auth import resolve_aca_submission_auth, resolve_http_trigger_auth
from .schema import (
    GlobalConfig,
    ResolvedAgent,
    SubagentRef,
    WorkflowConfig,
    WorkflowSubagentRef,
)

_SPEC_LINK_DEFAULT = "docs/front-matter-spec.md"

_UNSUPPORTED_TRIGGER_TYPES: dict[str, str] = {
    "activity_trigger": "Durable Functions triggers are not supported as agent triggers.",
    "assistant_skill_trigger": "Assistant skill triggers are not supported as agent triggers; use agent tools or MCP surfaces instead.",
    "entity_trigger": "Durable Functions triggers are not supported as agent triggers.",
    "mcp_prompt_trigger": "MCP prompt triggers are registered by built-in endpoints, not agent trigger front matter.",
    "mcp_resource_trigger": "MCP resource triggers are registered by built-in endpoints, not agent trigger front matter.",
    "mcp_tool_trigger": "MCP tool triggers are registered by built-in endpoints, not agent trigger front matter.",
    "orchestration_trigger": "Durable Functions triggers are not supported as agent triggers.",
    "route": "Use `http_trigger` instead of the Azure Functions `route` decorator name.",
    "schedule": "Use `timer_trigger` instead of the Azure Functions `schedule` decorator alias.",
    "warm_up_trigger": "Warm-up triggers are host lifecycle hooks and are not supported as agent triggers.",
}


def _format_error(
    source_file: str | Path,
    field: str,
    message: str,
    spec_anchor: str = "",
) -> str:
    spec_link = f"{_SPEC_LINK_DEFAULT}{spec_anchor}"
    normalized_message = message if message.endswith(".") else f"{message}."
    suffix = "" if "See " in normalized_message else f" See {spec_link}."
    return f"{Path(source_file)}: field `{field}`: {normalized_message}{suffix}"


def validate_resolved_agent(
    resolved: ResolvedAgent,
    *,
    discovered_mcp_names: list[str],
    discovered_skills: list[str],
    is_referenced_as_subagent: bool = False,
) -> None:
    """Run post-merge sanity checks for a resolved agent.

    ``is_referenced_as_subagent`` relaxes the trigger/``builtin_endpoints``
    requirement below: an agent reachable only as another agent's
    delegation target (via that agent's ``subagents:``) doesn't need its
    own external entry point.
    """
    source_file = resolved.source_file or "<unknown>"

    builtin_endpoints = resolved.builtin_endpoints
    has_builtin_endpoints = bool(
        builtin_endpoints.debug_chat_ui or builtin_endpoints.chat_api or builtin_endpoints.mcp
    )
    if (
        resolved.trigger is None
        and not has_builtin_endpoints
        and not is_referenced_as_subagent
    ):
        raise ValueError(
            _format_error(
                source_file,
                "trigger",
                "Required when no builtin_endpoints are enabled.",
                "#trigger",
            )
        )

    if resolved.trigger is not None:
        trigger_type = str(resolved.trigger.type or "").strip()
        unsupported_message = _UNSUPPORTED_TRIGGER_TYPES.get(trigger_type)
        if unsupported_message:
            raise ValueError(
                _format_error(
                    source_file,
                    "trigger.type",
                    unsupported_message,
                    "#trigger",
                )
            )
        if "." in trigger_type:
            raise ValueError(
                _format_error(
                    source_file,
                    "trigger.type",
                    "Dotted connector trigger types are not supported. Use `connector_trigger` instead.",
                    "#trigger",
                )
            )

    known_mcp = set(discovered_mcp_names)
    for name in resolved.mcp_exclude_names:
        if name not in known_mcp:
            raise ValueError(
                _format_error(
                    source_file,
                    "mcp.exclude",
                    f"Unknown MCP server reference `{name}`.",
                    "#mcp",
                )
            )

    known_skills = set(discovered_skills)
    for name in resolved.skills_exclude_names:
        if name not in known_skills:
            _logger.warning(
                "%s: field `skills.exclude`: Unknown skill reference `%s`. See docs/front-matter-spec.md#skills",
                source_file,
                name,
            )

    for name in resolved.tool_exclude_names:
        _logger.warning(
            "%s: field `tools.exclude`: Could not verify tool reference `%s` during config validation. See docs/front-matter-spec.md#tools",
            source_file,
            name,
        )


def validate_subagent_references(
    resolved: ResolvedAgent,
    *,
    known_slugs: set[str],
) -> None:
    """Reject self, unknown, and duplicate ``subagents:`` references.

    Must run only after the app-wide identity-slug index (``known_slugs``)
    is built and de-duplicated (see ``app.py``'s two-pass composition
    root) — these are fail-fast configuration errors, never silently
    dropped.
    """
    _validate_references(
        resolved,
        refs=resolved.subagents,
        known_slugs=known_slugs,
        field="subagents",
        self_message="An agent cannot delegate to itself",
        duplicate_message="Duplicate reference to agent",
        spec_anchor="#subagents",
    )


def validate_workflow_subagent_references(
    resolved: ResolvedAgent,
    *,
    known_slugs: set[str],
) -> None:
    """Reject invalid owner-specific ``workflows.subagents`` grants."""
    refs = resolved.workflows.subagents if resolved.workflows is not None else ()
    _validate_references(
        resolved,
        refs=refs,
        known_slugs=known_slugs,
        field="workflows.subagents",
        self_message="An agent cannot invoke itself as a Workflow Sub Agent",
        duplicate_message="Duplicate reference to agent",
        spec_anchor="#workflows",
    )


def _validate_references(
    resolved: ResolvedAgent,
    *,
    refs: list[SubagentRef] | tuple[WorkflowSubagentRef, ...],
    known_slugs: set[str],
    field: str,
    self_message: str,
    duplicate_message: str,
    spec_anchor: str,
) -> None:
    source_file = resolved.source_file or "<unknown>"
    seen: set[str] = set()
    for ref in refs:
        if ref.agent == resolved.slug:
            raise ValueError(
                _format_error(
                    source_file,
                    field,
                    f"{self_message} (`agent: {ref.agent}`).",
                    spec_anchor,
                )
            )
        if ref.agent not in known_slugs:
            raise ValueError(
                _format_error(
                    source_file,
                    field,
                    f"Unknown agent reference `{ref.agent}`. No agent with that "
                    "identity slug (file stem) was discovered in this app.",
                    spec_anchor,
                )
            )
        if ref.agent in seen:
            raise ValueError(
                _format_error(
                    source_file,
                    field,
                    f"{duplicate_message} `{ref.agent}` in `{field}`.",
                    spec_anchor,
                )
            )
        seen.add(ref.agent)


_FRD_0008_LINK = "docs/frds/0008-aca-sandbox-session-runtime.md"

_ALLOWED_AUTO_SUSPEND_IDLE_SECONDS: tuple[int, ...] = (60, 120, 300, 600, 1800, 3600)

_SUPPORTED_PLATFORM_SYSTEM = "Linux"
_SUPPORTED_PLATFORM_MACHINES = frozenset({"x86_64", "amd64"})
_SUPPORTED_PYTHON_MINOR_VERSIONS = frozenset({13, 14})


def _session_runtime_error(
    field: str,
    message: str,
    *,
    source_file: str | Path | None = None,
) -> ValueError:
    """Build a consistent session-runtime validation error."""
    location = f"{Path(source_file)}: " if source_file is not None else ""
    normalized = message if message.endswith(".") else f"{message}."
    return ValueError(f"{location}field `{field}`: {normalized} See {_FRD_0008_LINK}.")


def _workflows_requested(workflows: WorkflowConfig | None) -> bool:
    """Return whether workflows are enabled."""
    return workflows is not None and workflows.enabled


def auto_delete_backstop_violated(
    *,
    reclaim_idle_seconds: int,
    auto_delete_seconds: int,
    reconciler_cadence_seconds: int = DEFAULT_RECONCILER_CADENCE_SECONDS,
    grace_seconds: int = RECLAIM_SAFETY_GRACE_SECONDS,
) -> bool:
    """Return whether a lifecycle policy leaves insufficient reclaim margin."""
    return reclaim_exceeds_auto_delete_backstop(
        reclaim_idle_seconds=reclaim_idle_seconds,
        auto_delete_seconds=auto_delete_seconds,
        reconciler_cadence_seconds=reconciler_cadence_seconds,
        grace_seconds=grace_seconds,
    )


def _validate_platform_capability() -> None:
    """Require the supported ACA host ABI."""
    system = platform.system()
    if system != _SUPPORTED_PLATFORM_SYSTEM:
        raise _session_runtime_error(
            "session_runtime",
            f"aca_sandbox requires a Linux Function App host (detected `{system}`)",
        )
    machine = platform.machine().lower()
    if machine not in _SUPPORTED_PLATFORM_MACHINES:
        raise _session_runtime_error(
            "session_runtime",
            f"aca_sandbox requires an x86_64 Function App host (detected `{machine}`)",
        )
    major, minor = sys.version_info[:2]
    if major != 3 or minor not in _SUPPORTED_PYTHON_MINOR_VERSIONS:
        raise _session_runtime_error(
            "session_runtime",
            f"aca_sandbox requires Python 3.13 or 3.14 (detected {major}.{minor})",
        )


def _validate_agent_workflows_disabled(resolved: ResolvedAgent) -> None:
    """Reject Dynamic Workflows for ACA execution."""
    if resolved.is_main and _workflows_requested(resolved.workflows):
        raise _session_runtime_error(
            "workflows.enabled",
            "Dynamic Workflows are not supported when session_runtime.aca_sandbox "
            "is configured",
            source_file=resolved.source_file,
        )


def _validate_agent_no_sandbox_config(
    resolved: ResolvedAgent, global_config: GlobalConfig
) -> None:
    """Reject Dynamic Sessions code interpreter for ACA execution."""
    global_system_tools = global_config.system_tools
    if (
        global_system_tools is not None
        and global_system_tools.dynamic_sessions_code_interpreter is not None
    ):
        raise _session_runtime_error(
            "system_tools.dynamic_sessions_code_interpreter",
            "The Dynamic Sessions code interpreter is not supported when "
            "session_runtime.aca_sandbox is configured",
        )
    if resolved.sandbox_config is not None:
        raise _session_runtime_error(
            "system_tools.dynamic_sessions_code_interpreter",
            "The Dynamic Sessions code interpreter is not supported when "
            "session_runtime.aca_sandbox is configured",
            source_file=resolved.source_file,
        )


def _validate_agent_http_trigger_only(resolved: ResolvedAgent) -> None:
    """Require HTTP-shaped triggers for ACA execution."""
    trigger = resolved.trigger
    if trigger is not None and trigger.type != "http_trigger":
        raise _session_runtime_error(
            "trigger.type",
            "Only http_trigger agents are supported when session_runtime.aca_sandbox "
            f"is configured (got `{trigger.type}`)",
            source_file=resolved.source_file,
        )


def _resolve_http_trigger_auth_mode(trigger_args: dict[str, Any]) -> str:
    """Resolve the custom trigger's effective auth mode."""
    try:
        return resolve_http_trigger_auth(trigger_args).mode
    except ValueError:
        return "function"


def _agent_has_anonymous_http_surface(resolved: ResolvedAgent) -> bool:
    builtin_endpoints = resolved.builtin_endpoints
    has_builtin_endpoints = bool(
        builtin_endpoints.debug_chat_ui or builtin_endpoints.chat_api or builtin_endpoints.mcp
    )
    if has_builtin_endpoints and builtin_endpoints.http_auth.mode == "anonymous":
        return True
    trigger = resolved.trigger
    return (
        trigger is not None
        and trigger.type == "http_trigger"
        and _resolve_http_trigger_auth_mode(trigger.args) == "anonymous"
    )


def _validate_agent_endpoint_auth_configured(resolved: ResolvedAgent) -> None:
    """Reject anonymous ACA HTTP access."""
    if _agent_has_anonymous_http_surface(resolved):
        raise _session_runtime_error(
            "builtin_endpoints.http_auth",
            "Anonymous access is not supported when session_runtime.aca_sandbox "
            "is configured; configure a function key or Entra ID auth",
            source_file=resolved.source_file,
        )


def _validate_aca_http_auth_parity(resolved: ResolvedAgent) -> None:
    """Require one complete policy when both ACA submission surfaces are enabled."""
    trigger = resolved.trigger
    trigger_args = (
        trigger.args
        if trigger is not None and trigger.type == "http_trigger"
        else None
    )
    builtin_auth = resolved.builtin_endpoints.http_auth if resolved.builtin_endpoints.chat_api else None
    if builtin_auth is None or trigger_args is None:
        return
    try:
        resolve_aca_submission_auth(
            builtin_auth=builtin_auth,
            trigger_args=trigger_args,
        )
    except ValueError as exc:
        raise _session_runtime_error(
            "trigger.args.http_auth",
            str(exc),
            source_file=resolved.source_file,
        ) from exc


def validate_session_runtime(
    global_config: GlobalConfig,
    resolved_agents: list[ResolvedAgent],
) -> None:
    """Validate ACA session-runtime configuration before startup."""
    session_runtime = global_config.session_runtime
    if session_runtime is None:
        _logger.info(
            "session_runtime: not configured; defaulting to in-lang-worker "
            "(in-process) execution."
        )
        return

    aca_sandbox = session_runtime.aca_sandbox
    if aca_sandbox is None:
        _logger.info(
            "session_runtime: no aca_sandbox block configured; defaulting to "
            "in-lang-worker (in-process) execution."
        )
        return

    retention = aca_sandbox.retention

    if retention is not None:
        if retention.auto_suspend_idle not in _ALLOWED_AUTO_SUSPEND_IDLE_SECONDS:
            allowed = ", ".join(str(value) for value in _ALLOWED_AUTO_SUSPEND_IDLE_SECONDS)
            raise _session_runtime_error(
                "session_runtime.aca_sandbox.retention.auto_suspend_idle",
                f"Must be one of {allowed} seconds (got {retention.auto_suspend_idle})",
            )
        if retention.reclaim_idle <= 0 or retention.reclaim_idle <= retention.auto_suspend_idle:
            raise _session_runtime_error(
                "session_runtime.aca_sandbox.retention.reclaim_idle",
                "Must be positive and strictly greater than "
                f"retention.auto_suspend_idle ({retention.auto_suspend_idle}); "
                f"got {retention.reclaim_idle}",
            )
    for resolved in resolved_agents:
        _validate_agent_workflows_disabled(resolved)
        _validate_agent_no_sandbox_config(resolved, global_config)
        _validate_agent_http_trigger_only(resolved)
        _validate_agent_endpoint_auth_configured(resolved)
        _validate_aca_http_auth_parity(resolved)

    _validate_platform_capability()

    raise _session_runtime_error(
        "session_runtime.aca_sandbox",
        "aca_sandbox backend not available in this build",
    )
