import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from ._logger import logger


# ---------------------------------------------------------------------------
# Application root resolution
# ---------------------------------------------------------------------------

_app_root: Optional[Path] = None


def set_app_root(path: Path) -> None:
    """Explicitly set the application root directory.

    Call this early (e.g. before ``create_function_app()``) so that all
    agent, tool, skill, and MCP discovery uses the correct base path.
    """
    global _app_root
    _app_root = Path(path).resolve()


def get_app_root() -> Path:
    """Return the root directory of the user's agent project.

    This is the directory containing ``main.agent.md``, ``tools/``,
    ``.vscode/mcp.json``, skills directories, etc.

    Resolution order:

    1. Value set via ``set_app_root()``
    2. ``AZURE_FUNCTIONS_AGENTS_APP_ROOT`` environment variable
    3. ``AzureWebJobsScriptRoot`` environment variable (set automatically
       by the Azure Functions host, both locally via ``func start`` and
       in Azure — points to the directory containing ``host.json``)
    4. Current working directory (``Path.cwd()``)
    """
    if _app_root is not None:
        return _app_root
    explicit = os.environ.get("AZURE_FUNCTIONS_AGENTS_APP_ROOT")
    if explicit:
        return Path(explicit).resolve()
    script_root = os.environ.get("AzureWebJobsScriptRoot")
    if script_root:
        return Path(script_root).resolve()
    return Path.cwd().resolve()


# ---------------------------------------------------------------------------
# Config directory resolution (where session history JSONL files live)
# ---------------------------------------------------------------------------

# Mounted Azure Files location used by Functions deployments. Apps that want a
# different shared mount should set CODE_ASSISTANT_CONFIG_PATH (kept for
# deployment compatibility) or AZURE_FUNCTIONS_AGENTS_CONFIG_DIR.
_REMOTE_CONFIG_DIR = "/code-assistant-session"


def resolve_config_dir() -> Optional[str]:
    """
    Resolve the config directory used to persist agent-session history files.

    Priority:

    1. ``AZURE_FUNCTIONS_AGENTS_CONFIG_DIR`` env var (preferred name)
    2. ``CODE_ASSISTANT_CONFIG_PATH`` env var (legacy alias kept for
       compatibility with existing deployments)
    3. If ``CONTAINER_NAME`` is set (we are inside a Functions container):
       use the well-known mount ``/code-assistant-session``
    4. Otherwise return ``None`` and let the caller fall back to a sensible
       per-user default.
    """
    explicit_path = os.environ.get("AZURE_FUNCTIONS_AGENTS_CONFIG_DIR") or os.environ.get(
        "CODE_ASSISTANT_CONFIG_PATH"
    )
    if explicit_path:
        logger.info("Using config dir override: %s", explicit_path)
        return explicit_path

    container_name = os.environ.get("CONTAINER_NAME")
    if container_name:
        logger.info(
            "Remote mode detected (CONTAINER_NAME=%s), using %s", container_name, _REMOTE_CONFIG_DIR
        )
        return _REMOTE_CONFIG_DIR

    return None


# ---------------------------------------------------------------------------
# Environment variable substitution for agent frontmatter values
# ---------------------------------------------------------------------------

_PERCENT_PATTERN = re.compile(r"^%([^%]+)%$")
_DOLLAR_PATTERN = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)$")


def resolve_env_var(value: str) -> str:
    """Resolve a frontmatter value that is a single env-var reference.

    Supported syntaxes (full-string match only — partial substitution
    such as ``prefix$VAR`` is intentionally **not** supported):

      - ``%VAR_NAME%`` — value is entirely ``%…%``
      - ``$VAR_NAME``  — value is entirely ``$IDENT``

    If the value does not match either pattern, or the referenced
    environment variable is not set, the original string is returned
    unchanged.

    The following agent frontmatter fields are resolved through
    this function (all represent external resource identifiers or
    endpoints):

      - ``trigger.*`` (all string values except ``type``)
      - ``tools_from_connections[].connection_id``
      - ``execution_sandbox.session_pool_management_endpoint``

    Fields that should **not** use substitution (identifiers, literals,
    or user-facing text): ``name``, ``description``, ``trigger.type``,
    ``logger``.
    """
    stripped = value.strip()
    m = _PERCENT_PATTERN.match(stripped) or _DOLLAR_PATTERN.match(stripped)
    if m:
        return os.environ.get(m.group(1), value)
    return value


# ---------------------------------------------------------------------------
# Boolean coercion helper
# ---------------------------------------------------------------------------


def _to_bool(value: Any, default: bool = True) -> bool:
    """Coerce a frontmatter value to bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    return default


# ---------------------------------------------------------------------------
# Inline environment variable substitution for agent markdown body text
# ---------------------------------------------------------------------------

_INLINE_DOLLAR_PATTERN = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
_INLINE_PERCENT_PATTERN = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")


def substitute_env_vars_in_text(text: str) -> str:
    """Perform inline environment variable substitution in free-form text.

    Unlike :func:`resolve_env_var` (which requires the *entire* string to
    be a single variable reference), this function replaces variable
    references **inline** within arbitrary text.

    Supported syntaxes:

      - ``$VAR_NAME``  — e.g. ``send mail to $TO_EMAIL``
      - ``%VAR_NAME%`` — e.g. ``post to the %TEAM_NAME% team``

    If the referenced environment variable is not set, the original
    reference is left unchanged (fail-open).

    Text inside fenced code blocks (``````...``````) is left untouched
    so that documentation examples are not accidentally altered.
    """

    def _dollar_replacer(m: re.Match) -> str:
        return os.environ.get(m.group(1), m.group(0))

    def _percent_replacer(m: re.Match) -> str:
        return os.environ.get(m.group(1), m.group(0))

    def _substitute(segment: str) -> str:
        segment = _INLINE_DOLLAR_PATTERN.sub(_dollar_replacer, segment)
        segment = _INLINE_PERCENT_PATTERN.sub(_percent_replacer, segment)
        return segment

    # Split on fenced code blocks (```); odd-indexed parts are code blocks
    parts = text.split("```")
    for i in range(0, len(parts), 2):
        parts[i] = _substitute(parts[i])
    return "```".join(parts)



# ---------------------------------------------------------------------------
# Environment variable substitution for agent frontmatter values
# ---------------------------------------------------------------------------

_PERCENT_PATTERN = re.compile(r"^%([^%]+)%$")
_DOLLAR_PATTERN = re.compile(r"^\$([A-Za-z_][A-Za-z0-9_]*)$")


def resolve_env_var(value: str) -> str:
    """Resolve a frontmatter value that is a single env-var reference.

    Supported syntaxes (full-string match only — partial substitution
    such as ``prefix$VAR`` is intentionally **not** supported):

      - ``%VAR_NAME%`` — value is entirely ``%…%``
      - ``$VAR_NAME``  — value is entirely ``$IDENT``

    If the value does not match either pattern, or the referenced
    environment variable is not set, the original string is returned
    unchanged.

    The following agent frontmatter fields are resolved through
    this function (all represent external resource identifiers or
    endpoints):

      - ``trigger.*`` (all string values except ``type``)
      - ``tools_from_connections[].connection_id``
      - ``execution_sandbox.session_pool_management_endpoint``

    Fields that should **not** use substitution (identifiers, literals,
    or user-facing text): ``name``, ``description``, ``trigger.type``,
    ``logger``.
    """
    stripped = value.strip()
    m = _PERCENT_PATTERN.match(stripped) or _DOLLAR_PATTERN.match(stripped)
    if m:
        return os.environ.get(m.group(1), value)
    return value

