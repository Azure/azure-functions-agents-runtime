"""Helpers for building and parsing ``*.agent.md`` files.

An agent file is YAML front matter followed by a markdown body that becomes the
agent's instructions — the same authoring format the runtime consumes
(see ``docs/front-matter-spec.md``).
"""

from __future__ import annotations

import re
from typing import Any

import yaml

# Agent id / slug used for the blob name and function name. Lower-case, digits,
# and hyphens; must start and end alphanumeric; 1-40 chars.
AGENT_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")


def is_valid_agent_name(name: str) -> bool:
    """Return True if ``name`` is a safe agent id (no path traversal)."""
    return bool(AGENT_NAME_RE.match(name))


def build_agent_md(
    *,
    name: str,
    description: str,
    instructions: str,
    builtin_endpoints: bool = True,
) -> str:
    """Serialize a new ``*.agent.md`` document from its parts."""
    front: dict[str, Any] = {"name": name, "description": description}
    if builtin_endpoints:
        front["builtin_endpoints"] = True
    front_yaml = yaml.safe_dump(front, sort_keys=False, default_flow_style=False).strip()
    body = instructions.strip() or "You are a helpful assistant. Answer concisely."
    return f"---\n{front_yaml}\n---\n\n{body}\n"


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split ``text`` into (front matter dict, markdown body).

    Tolerant of files without front matter: returns ``({}, text)``.
    """
    if text.lstrip().startswith("---"):
        stripped = text.lstrip()
        parts = stripped.split("---", 2)
        if len(parts) >= 3:
            try:
                front = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                front = {}
            if not isinstance(front, dict):
                front = {}
            body = parts[2].lstrip("\n")
            return front, body
    return {}, text
