"""System-prompt addendum + integration entry point for the workflows feature.

Behavioral guidance for the workflow tools is owned by the runtime, not
by the agent markdown (see ``docs/workflows.md`` / "DX split"). When an
agent enables workflows in its frontmatter, the framework appends a
channel-specific addendum to the agent's system prompt. Chat endpoints
describe polling and synthetic completion notifications; declared triggers
describe short-lived starter behavior and terminal result sinks. Both cover
when to reach for a workflow and which tools the workflow can call.

``build_workflow_integration`` is the one call the app factory makes
to turn on workflows for the main agent: it registers the Durable
engine on the app, registers the discovered ``@workflow_tool`` inventory,
applies the optional ``workflows.exclude`` filter, stashes the effective
tool set on the workflows registry for ``start_workflow`` to read, and
returns the management tools plus both channel addenda.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import azure.functions as func

from azure_functions_agents._function_tool import WorkflowTool
from azure_functions_agents._logger import logger

from . import registry
from .engine import register_workflows
from .tools import build_workflow_tools

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
_ALLOWED_WORKFLOWS_KEYS: frozenset[str] = frozenset({
    "enabled", "exclude",
})


# Kept short on purpose — the individual tool descriptions carry the
# per-tool specifics. This is only about when the LLM should reach for
# a workflow instead of driving the work from chat directly. The
# "Available workflow tools" section is appended dynamically per-app.
_SHARED_ADDENDUM = (
    "\n\n"
    "## Long-running work: workflows\n\n"
    "You have access to workflow tools (`start_workflow`, `get_workflow_status`, "
    "`list_workflows`, `cancel_workflow`, `terminate_workflow`).\n\n"
    "Prefer starting a workflow when the user's request involves work that:\n"
    "- would take longer than a single chat turn, or\n"
    "- has steps that can run in parallel and you want them to, or\n"
    "- needs to survive a conversation pause / reconnect.\n\n"
    "End workflows with a small summary task (or a delivery task for a declared "
    "trigger) whenever the plan gathers more than one piece of evidence. "
    "Do not return large raw evidence blobs, logs, "
    "or per-item lists as the final workflow output unless the request explicitly "
    "requires raw data; summarize the useful signal inside the workflow.\n\n"
)

_CHAT_ADDENDUM = (
    "`start_workflow` is fire-and-forget. It returns a `workflow_id` immediately "
    "and the orchestration runs in the background. After it returns, briefly "
    "tell the user that work is in flight (include the `workflow_id`) and end "
    "your turn — **do not call `get_workflow_status` to wait for completion.** "
    "The chat client renders live per-task progress next to the conversation "
    "and will notify you when the workflow reaches a terminal state.\n\n"
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

_TRIGGER_ADDENDUM = (
    "This agent invocation came from a declared Azure Functions trigger. "
    "`start_workflow` is fire-and-forget: it returns a `workflow_id` immediately "
    "while the Durable orchestration continues independently. After it returns, "
    "retain or report the `workflow_id` as the trigger's response contract allows, "
    "then end this agent turn promptly. Do not poll `get_workflow_status` or wait "
    "for terminal workflow status inside this trigger invocation.\n\n"
    "There is no built-in chat poller, synthetic `<workflow-notification>` turn, "
    "or automatic agent reactivation for declared triggers. For timer, queue, "
    "blob, event, and similar non-HTTP triggers, include an explicit final "
    "tool task that delivers the result to a domain sink such as "
    "a queue, database, webhook, or notification tool. Operators can observe the "
    "Durable instance through Durable Functions or Durable Task Scheduler tooling.\n\n"
    "For an HTTP trigger, always honor its configured response schema or response "
    "example. Include `workflow_id` in the HTTP response only when that authored "
    "response format permits it."
)


@dataclass(frozen=True)
class WorkflowIntegrationResult:
    """Workflow registration output for each invocation channel.

    Iteration preserves the original public two-value return contract:
    ``workflow_tools, system_addendum = build_workflow_integration(...)``.
    The legacy addendum is the chat-channel addendum.
    """

    workflow_tools: list[Any]
    chat_system_addendum: str | None
    trigger_system_addendum: str | None

    def __iter__(self) -> Iterator[Any]:
        """Yield the legacy ``(workflow_tools, system_addendum)`` pair."""
        yield self.workflow_tools
        yield self.chat_system_addendum

    @property
    def enabled(self) -> bool:
        """Return whether both workflow invocation channels are enabled."""
        chat_enabled = self.chat_system_addendum is not None
        trigger_enabled = self.trigger_system_addendum is not None
        if chat_enabled != trigger_enabled:
            raise RuntimeError("workflow channel addenda must be enabled or disabled together")
        return chat_enabled


def _validate_workflows_block(metadata: dict[str, Any]) -> None:
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
    - malformed ``exclude`` (e.g. a string instead of a list,
      or a list with an empty string). Validated here so an
      ``exclude`` typo surfaces even when the agent currently
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
    if "exclude" in block:
        raw = block["exclude"]
        if not isinstance(raw, list) or not all(
            isinstance(x, str) and x for x in raw
        ):
            raise RuntimeError(
                "workflows.exclude must be a list of non-empty strings; "
                f"got {raw!r}"
            )


def _workflows_enabled(metadata: dict[str, Any]) -> bool:
    block = metadata.get("workflows")
    if not isinstance(block, dict):
        return False
    # Shape check has already enforced bool-ness via _validate_workflows_block.
    return bool(block.get("enabled", False))


def _register_workflow_tools(workflow_tools: Sequence[WorkflowTool]) -> frozenset[str]:
    effective: set[str] = set()
    for workflow_tool in workflow_tools:
        if workflow_tool.handler is None:
            logger.warning(
                "Skipping workflow tool %r: declaration-only tools are not supported",
                workflow_tool.name,
            )
            continue
        try:
            registry.register_workflow_tool(
                workflow_tool.name,
                workflow_tool.description,
                workflow_tool.handler,
                public=workflow_tool.public,
            )
        except ValueError as exc:
            logger.warning("Skipping workflow tool %r: %s", workflow_tool.name, exc)
            continue
        if workflow_tool.public:
            effective.add(workflow_tool.name)
    return frozenset(effective)


def _apply_workflow_exclude(
    workflow_tools: Sequence[WorkflowTool],
    metadata: dict[str, Any],
) -> tuple[WorkflowTool, ...]:
    block = metadata.get("workflows") or {}
    raw = block.get("exclude", [])
    excluded = set(raw)
    known = {tool.name for tool in workflow_tools}
    unknown = excluded - known
    if unknown:
        logger.warning(
            "workflows.exclude contains unknown workflow tool name(s): %s",
            sorted(unknown),
        )
    return tuple(tool for tool in workflow_tools if tool.name not in excluded)


def _build_tool_section(allowed_tools: frozenset[str]) -> str:
    """Return the dynamic workflow-tool section shared by both channels.

    Lists each allowed tool's name and engine-owned description. This is the
    single place the LLM learns which tool names are valid as workflow node
    targets. ``_build_addendum`` combines it with the shared and
    channel-specific guidance.

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
    return tool_section


def _build_addendum(allowed_tools: frozenset[str], *, trigger_invocation: bool) -> str:
    channel_addendum = _TRIGGER_ADDENDUM if trigger_invocation else _CHAT_ADDENDUM
    return _SHARED_ADDENDUM + channel_addendum + _build_tool_section(allowed_tools)


def build_workflow_integration(
    app: func.FunctionApp,
    metadata: dict[str, Any],
    workflow_tools: Sequence[WorkflowTool] | None = None,
) -> WorkflowIntegrationResult:
    """Enable workflows for the app if the main agent opted in.

    Returns a :class:`WorkflowIntegrationResult` containing management tools
    plus chat and declared-trigger system addenda. The tools are empty and both
    addenda are ``None`` when workflows are disabled.
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
        return WorkflowIntegrationResult([], None, None)
    register_workflows(app)
    filtered_workflow_tools = _apply_workflow_exclude(tuple(workflow_tools or ()), metadata)
    effective = _register_workflow_tools(filtered_workflow_tools)
    registry.set_app_config(effective)
    logger.info(
        "workflows enabled for main agent: %d tool(s) allowed (%s)",
        len(effective),
        ", ".join(sorted(effective)) or "<none>",
    )
    return WorkflowIntegrationResult(
        workflow_tools=build_workflow_tools(),
        chat_system_addendum=_build_addendum(effective, trigger_invocation=False),
        trigger_system_addendum=_build_addendum(effective, trigger_invocation=True),
    )


__all__ = [
    "WorkflowIntegrationResult",
    "build_workflow_integration",
]
