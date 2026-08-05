"""Validation helpers for configuration translation."""

from __future__ import annotations

import platform
import sys
from pathlib import Path
from typing import Any

from azure_functions_agents._logger import logger as _logger

from .http_auth import resolve_http_trigger_auth
from .schema import GlobalConfig, ResolvedAgent

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
    source_file = resolved.source_file or "<unknown>"
    seen: set[str] = set()
    for ref in resolved.subagents:
        if ref.agent == resolved.slug:
            raise ValueError(
                _format_error(
                    source_file,
                    "subagents",
                    f"An agent cannot delegate to itself (`agent: {ref.agent}`).",
                    "#subagents",
                )
            )
        if ref.agent not in known_slugs:
            raise ValueError(
                _format_error(
                    source_file,
                    "subagents",
                    f"Unknown agent reference `{ref.agent}`. No agent with that "
                    "identity slug (file stem) was discovered in this app.",
                    "#subagents",
                )
            )
        if ref.agent in seen:
            raise ValueError(
                _format_error(
                    source_file,
                    "subagents",
                    f"Duplicate reference to agent `{ref.agent}` in `subagents`.",
                    "#subagents",
                )
            )
        seen.add(ref.agent)


# ---------------------------------------------------------------------------
# FRD 0008 -- ACA Sandbox Session Runtime: startup validation matrix.
#
# `validate_session_runtime` enforces every row of the FRD's "Matrix: aca_sandbox
# startup/configuration behavior" table (docs/frds/0008-aca-sandbox-session-runtime.md
# #1 Authoring surface and startup validation). It is intentionally independent of
# `execution/*`: it raises its own `ValueError`s (matching this module's existing
# convention) rather than importing execution.unavailable.BackendUnavailableError,
# so this module keeps its existing config-only import graph and does not gain a
# new dependency edge on the execution package.
# ---------------------------------------------------------------------------

_FRD_0008_LINK = "docs/frds/0008-aca-sandbox-session-runtime.md"

# Row 9: the platform's documented idle auto-suspend values.
_ALLOWED_AUTO_SUSPEND_IDLE_SECONDS: tuple[int, ...] = (60, 120, 300, 600, 1800, 3600)

# Legacy arithmetic defaults retained for the pure compatibility helper below.
_RECONCILER_CADENCE_SECONDS_DEFAULT = 3600
_AUTO_DELETE_GRACE_SECONDS_DEFAULT = 300

# Row 12: the only Function App host ABI aca_sandbox supports.
_SUPPORTED_PLATFORM_SYSTEM = "Linux"
_SUPPORTED_PLATFORM_MACHINES = frozenset({"x86_64", "amd64"})
_SUPPORTED_PYTHON_MINOR_VERSIONS = frozenset({13, 14})


def _session_runtime_error(
    field: str,
    message: str,
    *,
    source_file: str | Path | None = None,
) -> ValueError:
    """Build a ``ValueError`` for a ``session_runtime`` startup-validation-matrix row.

    Shaped like ``_format_error``'s "field: message. See <link>." convention,
    but points at FRD 0008 (the ``session_runtime`` authoring surface is
    app-wide configuration, never per-agent front matter) and tolerates the
    absence of a natural per-agent ``source_file`` for whole-app rows.
    """
    location = f"{Path(source_file)}: " if source_file is not None else ""
    normalized = message if message.endswith(".") else f"{message}."
    return ValueError(f"{location}field `{field}`: {normalized} See {_FRD_0008_LINK}.")


def _workflows_requested(workflows: dict[str, Any] | None) -> bool:
    """Mirror ``app.py``'s predicate of the same name.

    Duplicated rather than imported: ``app.py`` is the composition root and
    imports *from* ``config``, so importing it back here would invert the
    discover -> translate -> register -> execute pipeline direction (see
    ``docs/architecture.md``).
    """
    return isinstance(workflows, dict) and workflows.get("enabled") is True


def auto_delete_backstop_violated(
    *,
    reclaim_idle_seconds: int,
    auto_delete_seconds: int,
    reconciler_cadence_seconds: int = _RECONCILER_CADENCE_SECONDS_DEFAULT,
    grace_seconds: int = _AUTO_DELETE_GRACE_SECONDS_DEFAULT,
) -> bool:
    """Return whether a supplied lifecycle policy leaves insufficient reclaim margin.

    The runtime now writes a complete per-sandbox policy after create, using
    ``reclaim + 3600 + 300`` for auto-delete. It no longer reads or validates a
    Sandbox Group default. This helper remains a pure compatibility utility for
    callers that explicitly supply a policy value.
    """
    return reclaim_idle_seconds > (
        auto_delete_seconds - reconciler_cadence_seconds - grace_seconds
    )


def _validate_platform_capability() -> None:
    """Row 12: the Function App host must be Linux x86_64 Python 3.13/3.14."""
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
    """Row 2: Dynamic Workflows are incompatible with ``aca_sandbox``.

    Mirrors ``app.py``'s own workflows-requested scoping: only the main
    agent's ``workflows.enabled`` is ever honored there (a non-main agent's
    ``workflows.enabled`` is silently ignored with a warning), so only the
    main agent can trigger this row.
    """
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
    """Row 3: the Dynamic Sessions code interpreter cannot combine with ``aca_sandbox``.

    These are two independent ACA-backed features (see the FRD's authoring
    surface section) and are never conflated. The global check runs once,
    ahead of the per-agent one, so a globally-configured interpreter that
    every agent has opted out of via ``system_tools.dynamic_sessions_code_interpreter:
    false`` is still reported (the per-agent ``resolved.sandbox_config`` is
    already opt-out aware -- see ``config.merge._resolve_sandbox`` -- so it
    alone would miss that all-opted-out case).
    """
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
    """Row 4: ``aca_sandbox`` agents must be bound to an HTTP-shaped trigger.

    An agent with no explicit trigger (relying solely on ``builtin_endpoints``)
    is still HTTP-shaped under the hood, so it does not fail this row.
    """
    trigger = resolved.trigger
    if trigger is not None and trigger.type != "http_trigger":
        raise _session_runtime_error(
            "trigger.type",
            "Only http_trigger agents are supported when session_runtime.aca_sandbox "
            f"is configured (got `{trigger.type}`)",
            source_file=resolved.source_file,
        )


def _resolve_http_trigger_auth_mode(trigger_args: dict[str, Any]) -> str:
    """Resolve an ``http_trigger``'s effective auth mode for row 8's purposes.

    Mirrors ``registration.triggers._resolve_http_trigger_auth``'s precedence
    (the nested ``http_auth`` object -- same shared model builtin endpoints
    use, supporting ``entra`` -- wins over the legacy flat ``auth_level``
    string; default is ``function``) using only the shared
    ``EndpointAuthConfig`` model, without importing the registration layer
    here (config must not depend on registration -- see
    ``docs/architecture.md``'s discover -> translate -> register -> execute
    pipeline direction). A malformed ``http_auth`` is deliberately not
    treated as anonymous here; ``registration.triggers`` reports that error
    independently at registration time.
    """
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
    """Row 8: anonymous HTTP access is not supported for ``aca_sandbox`` agents.

    Some valid Functions auth is mandatory (function key or Entra), but the
    FRD is explicit that Entra-only is not itself sufficient scope for this
    row -- it only requires that access is *not anonymous*; a deeper
    Entra-claims policy is out of scope here.
    """
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
    if (
        trigger is None
        or trigger.type != "http_trigger"
        or not resolved.builtin_endpoints.chat_api
    ):
        return
    try:
        trigger_auth = resolve_http_trigger_auth(trigger.args)
    except ValueError as exc:
        raise _session_runtime_error(
            "trigger.args.http_auth",
            str(exc),
            source_file=resolved.source_file,
        ) from exc
    builtin_auth = resolved.builtin_endpoints.http_auth
    if trigger_auth.model_dump() != builtin_auth.model_dump():
        raise _session_runtime_error(
            "trigger.args.http_auth",
            "Custom http_trigger and built-in chat require identical resolved auth policies "
            "when session_runtime.aca_sandbox is configured",
            source_file=resolved.source_file,
        )


def validate_session_runtime(
    global_config: GlobalConfig,
    resolved_agents: list[ResolvedAgent],
) -> None:
    """Enforce FRD 0008's ``aca_sandbox`` startup validation matrix.

    Absence of ``session_runtime`` is valid and selects the default
    in-process backend with no behavior change; none of the matrix rows
    below can fire in that case. Configuring the ``aca_sandbox`` block is
    itself what selects the ACA Sandbox execution backend, so every row
    below is conditioned on ``session_runtime.aca_sandbox`` being present
    (row 1's ``harness`` check is schema-enforced via ``Literal["maf"]``,
    before this function ever runs). Every row raises a plain ``ValueError``,
    matching this module's existing convention (see ``validate_resolved_agent``,
    ``validate_subagent_references``).

    ``aca_sandbox`` is not implemented yet (see ``execution/unavailable.py``):
    once all other rows pass, this function still unconditionally raises a
    final capability-gate error so a well-formed ``aca_sandbox`` config fails
    **application startup** with a clear, typed diagnostic rather than a
    confusing runtime error at first request.
    """
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
        # In-process (default): no further FRD 0008 rows apply. Row 5's
        # "sandbox_group_resource_id required" case is enforced entirely at
        # the schema layer, before this function ever runs, for both ways an
        # `aca_sandbox` block can fail to select the backend: an
        # `aca_sandbox: {}` block missing the field is a plain Pydantic
        # required-field error (AcaSandboxConfig construction runs and
        # fails), while a bare `aca_sandbox:` key (explicit `null`, distinct
        # from the key being omitted -- which is this branch) is rejected by
        # `SessionRuntimeConfig`'s `_check_explicit_null_aca_sandbox`
        # model_validator, since Pydantic would otherwise match that `None`
        # directly against the `AcaSandboxConfig | None` union's `None` arm
        # without ever attempting construction, silently falling through to
        # this same in-process return instead of failing startup.
        return

    retention = aca_sandbox.retention

    if retention is not None:
        # Row 9 (owning rule: 0008.10 + 0008.12): auto_suspend_idle must be
        # one of the platform's documented idle-timeout values.
        if retention.auto_suspend_idle not in _ALLOWED_AUTO_SUSPEND_IDLE_SECONDS:
            allowed = ", ".join(str(value) for value in _ALLOWED_AUTO_SUSPEND_IDLE_SECONDS)
            raise _session_runtime_error(
                "session_runtime.aca_sandbox.retention.auto_suspend_idle",
                f"Must be one of {allowed} seconds (got {retention.auto_suspend_idle})",
            )
        # Row 10 (owning rule: 0008.10 + 0008.12): reclaim_idle must be
        # positive and strictly greater than auto_suspend_idle.
        if retention.reclaim_idle <= 0 or retention.reclaim_idle <= retention.auto_suspend_idle:
            raise _session_runtime_error(
                "session_runtime.aca_sandbox.retention.reclaim_idle",
                "Must be positive and strictly greater than "
                f"retention.auto_suspend_idle ({retention.auto_suspend_idle}); "
                f"got {retention.reclaim_idle}",
            )
        # Per-sandbox lifecycle policy is applied lazily by the controller.
        # Group auto-delete readback is deliberately not a startup dependency.

    # Rows 6 and 7 (session state storage auth-mode / dedicated-account
    # checks) are removed. Session state always reuses `AzureWebJobsStorage`,
    # in every environment, with no dedicated account and no Shared-Key gate
    # at this layer.

    # Rows 2, 3, 4, 8: per-agent checks.
    for resolved in resolved_agents:
        _validate_agent_workflows_disabled(resolved)
        _validate_agent_no_sandbox_config(resolved, global_config)
        _validate_agent_http_trigger_only(resolved)
        _validate_agent_endpoint_auth_configured(resolved)
        _validate_aca_http_auth_parity(resolved)

    # Row 12 (owning rule: 0008.7 + 0008.10): Function App host ABI gate.
    # Deliberately checked last among the fail-closed rows (not in FRD table
    # order): it is an independent, atomic host check with no ordering
    # dependency on rows 1-10 or 2/3/4/8, and placing it last means a
    # misconfigured app (wrong retention, workflows enabled, anonymous auth,
    # etc.) is reported with its *specific* error even when also running on
    # an unsupported host -- e.g. during local development on Windows/macOS.
    _validate_platform_capability()

    # Capability gate: all other rows passed, but aca_sandbox execution is
    # not implemented in this build (see execution/unavailable.py). This
    raise _session_runtime_error(
        "session_runtime.aca_sandbox",
        "aca_sandbox backend not available in this build",
    )
