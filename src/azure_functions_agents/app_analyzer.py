"""
Agent file discovery and registration orchestrator.

Walks the app root for ``*.agent.md`` files, parses their frontmatter,
extracts agent-level configuration (tools, sandbox, connectors), and
delegates trigger registration to :mod:`translator`.

This module does **not** own the HTTP chat / MCP / UI endpoints — those
remain in :mod:`app` (``create_function_app``).

Dependency graph::

    app.py ─> app_analyzer.py ─> translator.py ─> handlers.py
"""

from __future__ import annotations

import glob
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import frontmatter

from .config import resolve_env_var, substitute_env_vars_in_text, _to_bool
from .connector_tool_cache import configure_connector_tools
from .translator import AgentTriggerRegistration, register_agent_trigger


# ---------------------------------------------------------------------------
# Agent file loading
# ---------------------------------------------------------------------------

def load_agent_file(path: Path) -> Optional[Dict[str, Any]]:
    """Parse an agent markdown file and return its metadata + content.

    Returns a dict with ``'metadata'`` (frontmatter dict) and ``'content'``
    (body str), or ``None`` if the file doesn't exist or can't be parsed.
    """
    if not path.exists():
        return None
    try:
        post = frontmatter.load(str(path))
        metadata = dict(post.metadata) if post.metadata else {}
        content = (post.content or "").strip()

        # Substitute $ENV_VAR references in content (opt-out via frontmatter)
        if _to_bool(metadata.get("substitute_variables"), default=True):
            content = substitute_env_vars_in_text(content)

        return {"metadata": metadata, "content": content}
    except Exception as exc:
        logging.warning(f"Failed to parse agent file {path}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def warn_if_legacy_runtime_field(metadata: Dict[str, Any], filename: str) -> None:
    """Emit a one-time deprecation warning if an agent file still declares ``runtime:``.

    Earlier versions of the runtime supported ``runtime: copilot|maf`` to
    select between two backends. As of 1.0.0 only the Microsoft Agent
    Framework is supported and the field is silently ignored.
    """
    raw = metadata.get("runtime")
    if raw is None:
        return
    logging.warning(
        f"{filename}: ignoring deprecated frontmatter field 'runtime: {raw}' — "
        "the runtime now uses the Microsoft Agent Framework only. "
        "Remove the 'runtime:' field from your agent file."
    )


def safe_function_name(raw_name: str) -> str:
    """Sanitize a raw name into a valid Azure Functions function name."""
    name = re.sub(r"[^a-zA-Z0-9_]", "_", raw_name).strip("_")
    if not name:
        return "agent_function"
    if name[0].isdigit():
        return f"fn_{name}"
    return name


# ---------------------------------------------------------------------------
# Discovery + registration
# ---------------------------------------------------------------------------

def discover_and_register_agents(
    app,
    app_root: Path,
) -> None:
    """Walk ``*.agent.md`` files and register a trigger for each.

    Skips ``main.agent.md`` — that file is handled directly by
    ``create_function_app`` for the HTTP chat / MCP / UI surface.
    """
    agent_files = sorted(glob.glob(str(app_root / "*.agent.md")))
    if not agent_files:
        logging.info("No agent files found.")
        return

    connectors_instance = None  # Lazy-init if needed
    registered_names: set = set()

    for agent_path_str in agent_files:
        agent_path = Path(agent_path_str)

        # Skip the main agent — it's handled separately
        if agent_path.name == "main.agent.md":
            continue

        agent = load_agent_file(agent_path)
        if not agent:
            continue

        metadata = agent["metadata"]
        content = agent["content"]
        warn_if_legacy_runtime_field(metadata, agent_path.name)

        trigger_spec = metadata.get("trigger")
        if not isinstance(trigger_spec, dict) or "type" not in trigger_spec:
            logging.warning(
                f"Skipping {agent_path.name}: missing or invalid 'trigger' section "
                "(must have 'type')"
            )
            continue

        # Extract trigger type and params
        trigger_type = str(trigger_spec["type"]).strip()
        trigger_params = {k: v for k, v in trigger_spec.items() if k != "type"}

        # Agent-level settings
        agent_name = metadata.get("name", agent_path.stem)
        should_log = _to_bool(metadata.get("logger", True), default=True)

        # Unique function name from filename
        base_name = safe_function_name(agent_path.stem)
        function_name = base_name
        suffix = 2
        while function_name in registered_names:
            function_name = f"{base_name}_{suffix}"
            suffix += 1
        registered_names.add(function_name)

        # Per-agent connector tools (additive, deduplicated globally)
        agent_connections = metadata.get("tools_from_connections")
        if isinstance(agent_connections, list):
            configure_connector_tools(agent_connections)

        # Per-agent sandbox config
        agent_sandbox_config = metadata.get("execution_sandbox")
        if not isinstance(agent_sandbox_config, dict):
            agent_sandbox_config = None

        # Build registration and delegate to translator
        registration = AgentTriggerRegistration(
            function_name=function_name,
            agent_name=agent_name,
            trigger_type=trigger_type,
            trigger_params=trigger_params,
            prompt=content,
            should_log=should_log,
            sandbox_config=agent_sandbox_config,
            response_example=metadata.get("response_example"),
            response_schema=metadata.get("response_schema"),
        )

        connectors_instance = register_agent_trigger(
            app, registration, connectors_instance
        )
