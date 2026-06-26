"""System-prompt addendum + integration entry point for the workflows feature.

Behavioral guidance for the workflow tools is owned by the engine, not
by the agent markdown (see ``docs/workflows.md`` / "DX split"). When an
agent enables workflows in its frontmatter, the framework appends a
short addendum below to the agent's system prompt — covering both
*when* to reach for a workflow and *which* tools the workflow can call —
so every workflow-enabled agent gets the same heuristics without the
author having to copy-paste prose into every agent file.

``build_workflow_integration`` is the one call the app factory makes
to turn on workflows for the main agent: it registers the Durable
engine on the app, computes the effective tool allowlist for this app
from the optional ``workflows.allowed_tools`` frontmatter list, stashes
it on the workflows registry for ``start_workflow`` to read, and
returns the tool list + addendum the chat handlers should thread
through to the agent loop.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import azure.functions as func

from . import registry
from .engine import register_workflows
from .tools import build_workflow_tools

log = logging.getLogger(__name__)


# Whitelist of frontmatter keys we recognize under ``workflows``. Any
# other key is rejected at app start so typos (``enabld``, ``allow_tools``)
# surface immediately rather than silently degrading to defaults. New
# knobs added in later milestones must be added here too.
#
# Note: the Durable execution backend (Azure Storage vs Durable Task
# Scheduler) is selected entirely via ``host.json``'s ``storageProvider``
# block and the matching app settings — the library never reads or
# routes on it. We deliberately do *not* expose ``workflows.backend``
# here because a frontmatter declaration would just be a parallel
# assertion that can drift from the truth.
_ALLOWED_WORKFLOWS_KEYS: frozenset = frozenset({
    "enabled", "allowed_tools",
})


# Kept short on purpose — the individual tool descriptions carry the
# per-tool specifics. This is only about when the LLM should reach for
# a workflow instead of driving the work from chat directly. The
# "Available workflow tools" section is appended dynamically per-app.
_BASE_ADDENDUM = (
    "\n\n"
    "## Long-running work: workflows\n\n"
    "You have access to workflow tools (`start_workflow`, `get_workflow_status`, "
    "`list_workflows`, `cancel_workflow`, `terminate_workflow`).\n\n"
    "Prefer starting a workflow when the user's request involves work that:\n"
    "- would take longer than a single chat turn, or\n"
    "- has steps that can run in parallel and you want them to, or\n"
    "- needs to survive a conversation pause / reconnect.\n\n"
    "`start_workflow` is fire-and-forget. It returns a `workflow_id` immediately "
    "and the orchestration runs in the background. After it returns, briefly "
    "tell the user that work is in flight (include the `workflow_id`) and end "
    "your turn — **do not call `get_workflow_status` to wait for completion.** "
    "The chat client renders live per-task progress next to the conversation "
    "and will notify you when the workflow reaches a terminal state.\n\n"
    "End workflows with a small summary task whenever the plan gathers more "
    "than one piece of evidence. Do not return large raw evidence blobs, logs, "
    "or per-item lists as the final workflow output unless the user explicitly "
    "asked for raw data; summarize the useful signal inside the workflow so the "
    "later `get_workflow_status` call keeps the model context small.\n\n"
    "When a workflow you started reaches a terminal state, the chat client "
    "injects a synthetic user message containing one or more "
    "`<workflow-notification>` envelopes — one per finished workflow. Each "
    "envelope wraps a `<workflow-id>`, a `<status>` (Completed / Failed / "
    "Canceled / Terminated), and a short `<summary>`, e.g.:\n"
    "```\n"
    "<workflow-notification>\n"
    "  <workflow-id>abc-123</workflow-id>\n"
    "  <status>Completed</status>\n"
    "  <summary>Workflow abc-123 finished with status Completed.</summary>\n"
    "</workflow-notification>\n"
    "```\n"
    "Treat that message as: call `get_workflow_status` once with **each** "
    "`<workflow-id>`, then write a short, clear natural-language summary of "
    "the result(s) for the user. Notification turns are summary-only — do "
    "not start new workflows or call additional tools to investigate "
    "further unless the user later asks for a deeper look. If a workflow "
    "ended without a usable final output (e.g. Terminated, or Canceled "
    "with no partial result), say so plainly rather than implying a result "
    "exists. If `get_workflow_status` happens to return a non-terminal "
    "status (a brief race between the chat client and the management "
    "API), tell the user the detailed result isn't available yet and end "
    "the turn — do not poll again.\n\n"
    "Outside of `<workflow-notification>` turns, only call "
    "`get_workflow_status` "
    "(or `list_workflows`) when the user explicitly asks about a previously-"
    "started workflow — for example *\"is workflow X still running?\"*. Polling "
    "on your own initiative wastes turns and tokens.\n\n"
    "If the user changes their mind, prefer `cancel_workflow` (cooperative; "
    "preserves partial results) over `terminate_workflow` (abrupt). For short, "
    "latency-sensitive work that fits comfortably in a single turn, keep using "
    "direct tool calls — workflows add overhead."
)

# Backwards-compat: tests import this constant. With per-app tool
# listings the *complete* addendum is now built by
# ``_build_addendum``; this constant is the static prefix only.
WORKFLOW_SYSTEM_ADDENDUM = _BASE_ADDENDUM


def _validate_workflows_block(metadata: Dict[str, Any]) -> None:
    """Shape-check the ``workflows`` block before any field is read.

    Catches four classes of mistake at app start:

    - ``workflows`` set to a non-mapping (e.g. a string)
    - typo'd or unsupported key inside the block (e.g. ``enabld``,
      ``backend``, ``task_hub``). The latter two name real Durable
      concepts but are *not* honored by the library: Durable backend
      selection lives in ``host.json``'s
      ``extensions.durableTask.storageProvider`` block and task-hub
      naming lives in ``extensions.durableTask.hubName``. Silent
      acceptance would mislead a contributor into thinking frontmatter
      drives behavior it doesn't.
    - non-boolean ``enabled`` (``enabled: "false"`` is a YAML
      foot-gun — without this guard it would parse as truthy and
      enable workflows).
    - malformed ``allowed_tools`` (e.g. a string instead of a list,
      or a list with an empty string). Validated here so a
      ``allowed_tools`` typo surfaces even when the agent currently
      has ``enabled: false``.

    Returning silently is the success path; raises ``RuntimeError``
    with a message naming the offending key/value otherwise. Called
    unconditionally from :func:`build_workflow_integration`, including
    the disabled path, so frontmatter typos surface even before the
    user enables workflows.
    """
    block = metadata.get("workflows")
    if block is None:
        return
    if not isinstance(block, dict):
        raise RuntimeError(
            "workflows must be a mapping (e.g. `workflows: { enabled: true }`); "
            f"got {block!r}"
        )
    unknown = sorted(set(block.keys()) - _ALLOWED_WORKFLOWS_KEYS)
    if unknown:
        # Targeted hint for the two real-Durable-concept keys that
        # contributors are most likely to reach for; generic hint
        # otherwise so plain typos like `enabld` don't get a
        # misleading host.json suggestion.
        if "backend" in unknown:
            hint = (
                " (Durable backend selection lives in host.json's "
                "extensions.durableTask.storageProvider block, not in "
                "agent frontmatter.)"
            )
        elif "task_hub" in unknown:
            hint = (
                " (Task hub name lives in host.json's "
                "extensions.durableTask.hubName, not in agent "
                "frontmatter.)"
            )
        else:
            hint = ""
        raise RuntimeError(
            f"unknown key(s) under workflows: {unknown}. Supported keys: "
            f"{sorted(_ALLOWED_WORKFLOWS_KEYS)}.{hint}"
        )
    if "enabled" in block and not isinstance(block["enabled"], bool):
        raise RuntimeError(
            "workflows.enabled must be a boolean (true/false); got "
            f"{block['enabled']!r}"
        )
    if "allowed_tools" in block:
        raw = block["allowed_tools"]
        if not isinstance(raw, list) or not all(
            isinstance(x, str) and x for x in raw
        ):
            raise RuntimeError(
                "workflows.allowed_tools must be a list of non-empty strings; "
                f"got {raw!r}"
            )


def _workflows_enabled(metadata: Dict[str, Any]) -> bool:
    block = metadata.get("workflows")
    if not isinstance(block, dict):
        return False
    # Shape check has already enforced bool-ness via _validate_workflows_block.
    return bool(block.get("enabled", False))


def _read_allowed_tools(metadata: Dict[str, Any]) -> Optional[List[str]]:
    """Extract ``workflows.allowed_tools`` from frontmatter.

    Returns ``None`` when the field is omitted (caller falls back to
    ``registry.public_tool_names()``). Shape (``list[non-empty str]``)
    has already been enforced by :func:`_validate_workflows_block`,
    so this is now just a safe accessor; the registry lookup
    happens later in :func:`_compute_effective_allowlist`.
    """
    block = metadata.get("workflows") or {}
    if "allowed_tools" not in block:
        return None
    return list(block["allowed_tools"])


def _compute_effective_allowlist(
    requested: Optional[List[str]],
) -> frozenset:
    """Validate the requested allowlist against the registry.

    With no frontmatter override, the effective set is every *public*
    registered tool — internal tools like ``__echo`` stay out of the
    agent's reach by default. With an override, every name must be a
    registered tool (public or not — explicit opt-in) and must not
    collide with a reserved workflow-management tool.
    """
    if requested is None:
        return registry.public_tool_names()
    if not requested:
        # Explicit empty list: agent has workflows.enabled but is
        # allowed to call zero workflow tools. Allowed (the agent could
        # still author wait-only plans) but worth a warning since it's
        # almost always a typo.
        log.warning("workflows.allowed_tools is an empty list — no tool "
                    "tasks will validate.")
        return frozenset()
    reserved = [n for n in requested if n in registry.RESERVED_TOOL_NAMES]
    if reserved:
        raise RuntimeError(
            "workflows.allowed_tools cannot include workflow-management "
            f"tools: {sorted(reserved)}"
        )
    unknown = [n for n in requested if registry.get_entry(n) is None]
    if unknown:
        raise RuntimeError(
            "workflows.allowed_tools contains unknown tool name(s): "
            f"{sorted(unknown)}. Registered tools: "
            f"{sorted(registry.all_registered_names())}"
        )
    return frozenset(requested)


def _build_addendum(allowed_tools: frozenset) -> str:
    """Return the per-app system-prompt addendum.

    Includes the static "when to use workflows" prose plus a dynamic
    "Available workflow tools" section listing each allowed tool's
    name and engine-owned description. This is the single place the
    LLM learns which tool names are valid as workflow node targets.

    Computed once at app start and threaded through ``extra_tools`` /
    ``system_addendum``; M1 does not support runtime allowlist changes.
    The per-agent registry refactor in M3 will rebuild this per agent
    rather than per-app.
    """
    if not allowed_tools:
        tool_section = (
            "\n\n### Available workflow tools\n\n"
            "_No tool tasks are currently allowed for this agent — "
            "workflows can only schedule `wait` tasks._"
        )
    else:
        lines = ["\n\n### Available workflow tools\n"]
        for name in sorted(allowed_tools):
            entry = registry.get_entry(name)
            description = entry.description if entry is not None else ""
            lines.append(f"- `{name}` — {description}")
        tool_section = "\n".join(lines)
    return _BASE_ADDENDUM + tool_section


def build_workflow_integration(
    app: func.FunctionApp, metadata: Dict[str, Any]
) -> Tuple[List[Any], Optional[str]]:
    """Enable workflows for the app if the main agent opted in.

    Returns ``(workflow_tools, system_addendum)``. Both are empty /
    ``None`` when the agent hasn't set ``workflows.enabled: true`` — the
    caller can unconditionally extend its tool list and concat the
    addendum without branching.
    """
    # Shape-check the workflows block first so typos surface at app
    # start regardless of whether workflows are enabled. A typo'd key
    # or an "enabled: 'false'" string would otherwise only fail when
    # the agent is later flipped on.
    _validate_workflows_block(metadata)

    if not _workflows_enabled(metadata):
        # Disabled path is a no-op past the shape check: do NOT call
        # register_workflows or registry.set_app_config. A previously-
        # configured allowlist (if any) is intentionally left untouched
        # so this function is safe to call multiple times in test
        # scenarios that toggle metadata.
        return [], None

    register_workflows(app)
    requested = _read_allowed_tools(metadata)
    effective = _compute_effective_allowlist(requested)
    registry.set_app_config(effective)
    log.info(
        "workflows enabled for main agent: %d tool(s) allowed (%s)",
        len(effective),
        ", ".join(sorted(effective)) or "<none>",
    )
    return build_workflow_tools(), _build_addendum(effective)


__all__ = [
    "WORKFLOW_SYSTEM_ADDENDUM",
    "build_workflow_integration",
]
