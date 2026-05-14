import importlib.util
import inspect
import json
import os
import re
import sys
import tempfile
from typing import Any, List, Optional

from ._logger import logger

from agent_framework import FunctionTool, tool
from pydantic import BaseModel, Field

from .config import get_app_root


def discover_tools() -> List[FunctionTool]:
    """
    Dynamically discover and load tools from the project's ``tools/`` folder.

    Tool modules may either:

    * decorate functions with ``@tool`` from :mod:`agent_framework`, in which
      case the resulting :class:`FunctionTool` instances are picked up
      directly, or
    * expose plain ``async def`` (or ``def``) functions, which are wrapped in
      :class:`FunctionTool` automatically with the docstring as the
      description.

    The first matching object per file is registered (preserving the previous
    behavior of the runtime).
    """
    tools: List[FunctionTool] = []
    project_src_dir = str(get_app_root())
    tools_dir = os.path.join(project_src_dir, "tools")

    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)

    print(f"[Tool Discovery] Looking for tools in: {tools_dir}")
    print(f"[Tool Discovery] Directory exists: {os.path.exists(tools_dir)}")

    if not os.path.exists(tools_dir):
        print(f"[Tool Discovery] WARNING: Tools directory not found: {tools_dir}")
        return tools

    files = sorted(
        f for f in os.listdir(tools_dir) if f.endswith(".py") and not f.startswith("_")
    )
    print(f"[Tool Discovery] Python files found: {files}")

    for filename in files:
        filepath = os.path.join(tools_dir, filename)
        module_name = filename[:-3]
        print(f"[Tool Discovery] Loading module: {module_name} from {filepath}")
        try:
            spec = importlib.util.spec_from_file_location(module_name, filepath)
            if spec is None or spec.loader is None:
                print(f"[Tool Discovery] ERROR: Could not create spec for {filename}")
                continue

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            picked: Optional[FunctionTool] = None

            # Prefer module-level ``@tool``-decorated values (FunctionTool
            # instances) — they carry their own name/description/schema.
            for name, obj in inspect.getmembers(module):
                if name.startswith("_"):
                    continue
                if isinstance(obj, FunctionTool):
                    picked = obj
                    print(f"[Tool Discovery] Loaded (FunctionTool): {obj.name}")
                    break

            # Fallback: first plain function defined in the module.
            if picked is None:
                local_functions = [
                    (name, obj)
                    for name, obj in inspect.getmembers(module, inspect.isfunction)
                    if obj.__module__ == module_name and not name.startswith("_")
                ]
                if local_functions:
                    name, fn = local_functions[0]
                    description = (fn.__doc__ or f"Tool: {name}").strip()
                    picked = tool(fn, name=name, description=description)
                    print(f"[Tool Discovery] Loaded (auto-wrapped): {name}")
                    print(f"[Tool Discovery]   Description: {description}")

            if picked is not None:
                tools.append(picked)
        except Exception as e:
            import traceback

            print(f"[Tool Discovery] ERROR loading {filename}: {e}")
            traceback.print_exc()
            logger.error("Failed to load tool from %s: %s", filename, e)

    return tools


# ---------------------------------------------------------------------------
# Built-in tools (always available, shipped with the library)
# ---------------------------------------------------------------------------

# Directories the agent is allowed to read from.
_ALLOWED_READ_DIRS = [
    os.path.normpath(tempfile.gettempdir()),
]

# Allow reading skill reference files from {approot}/skills/
_skills_dir = os.path.join(str(get_app_root()), "skills")
if os.path.isdir(_skills_dir):
    _ALLOWED_READ_DIRS.append(os.path.normpath(_skills_dir))


def _check_access(path: str) -> Optional[str]:
    """Return an error JSON string if the path is not allowed, else None."""
    requested = os.path.normpath(path)
    allowed = any(
        requested.startswith(d + os.sep) or requested == d
        for d in _ALLOWED_READ_DIRS
    )
    if not allowed:
        return json.dumps({"error": "Access denied: path is not in an allowed directory"})
    if not os.path.isfile(requested):
        return json.dumps({"error": f"File not found: {path}"})
    return None


def _read_lines(path: str) -> List[str]:
    """Read all lines from a file."""
    with open(os.path.normpath(path), "r", encoding="utf-8", errors="replace") as f:
        return f.readlines()


# -- view (read file with optional line range) -----------------------------

class ViewParams(BaseModel):
    path: str = Field(description="Absolute path to the file to read")
    start_line: Optional[int] = Field(default=None, description="1-based start line number. If omitted, reads from the beginning.")
    end_line: Optional[int] = Field(default=None, description="1-based end line number (inclusive). If omitted, reads to the end.")


@tool(
    name="view",
    description=(
        "View a file on the local system by absolute path. Use view_range"
        " (start_line/end_line) to read specific sections. Use this to read"
        " files that other tools have saved to the temp directory."
    ),
    schema=ViewParams,
)
async def view(params: ViewParams) -> str:
    err = _check_access(params.path)
    if err:
        return err

    lines = _read_lines(params.path)
    total = len(lines)
    start = (params.start_line or 1) - 1
    end = params.end_line or total
    start = max(0, min(start, total))
    end = max(start, min(end, total))

    return json.dumps({
        "total_lines": total,
        "start_line": start + 1,
        "end_line": end,
        "content": "".join(lines[start:end]),
    })


# -- head (first N lines) -------------------------------------------------

class HeadParams(BaseModel):
    path: str = Field(description="Absolute path to the file")
    lines: Optional[int] = Field(default=10, description="Number of lines to return from the start (default 10)")


@tool(
    name="head",
    description="Show the first N lines of a file on the local system (default 10).",
    schema=HeadParams,
)
async def head(params: HeadParams) -> str:
    err = _check_access(params.path)
    if err:
        return err

    all_lines = _read_lines(params.path)
    n = max(1, params.lines or 10)
    return json.dumps({
        "total_lines": len(all_lines),
        "lines_returned": min(n, len(all_lines)),
        "content": "".join(all_lines[:n]),
    })


# -- tail (last N lines) --------------------------------------------------

class TailParams(BaseModel):
    path: str = Field(description="Absolute path to the file")
    lines: Optional[int] = Field(default=10, description="Number of lines to return from the end (default 10)")


@tool(
    name="tail",
    description="Show the last N lines of a file on the local system (default 10).",
    schema=TailParams,
)
async def tail(params: TailParams) -> str:
    err = _check_access(params.path)
    if err:
        return err

    all_lines = _read_lines(params.path)
    n = max(1, params.lines or 10)
    selected = all_lines[-n:] if n < len(all_lines) else all_lines
    return json.dumps({
        "total_lines": len(all_lines),
        "lines_returned": len(selected),
        "content": "".join(selected),
    })


# -- grep (search file contents) ------------------------------------------

class GrepParams(BaseModel):
    path: str = Field(description="Absolute path to the file to search")
    pattern: str = Field(description="Search pattern (plain text or regex)")
    is_regex: Optional[bool] = Field(default=False, description="Treat pattern as a regex (default: plain text)")
    ignore_case: Optional[bool] = Field(default=True, description="Case-insensitive search (default: true)")
    max_results: Optional[int] = Field(default=50, description="Maximum number of matching lines to return (default 50)")


@tool(
    name="grep",
    description=(
        "Search for a pattern in a file on the local system. Returns matching"
        " lines with line numbers. Supports plain text and regex patterns."
    ),
    schema=GrepParams,
)
async def grep(params: GrepParams) -> str:
    err = _check_access(params.path)
    if err:
        return err

    lines = _read_lines(params.path)
    flags = re.IGNORECASE if params.ignore_case else 0
    limit = max(1, params.max_results or 50)

    matches = []
    for i, line in enumerate(lines, 1):
        try:
            if params.is_regex:
                found = re.search(params.pattern, line, flags)
            else:
                if params.ignore_case:
                    found = params.pattern.lower() in line.lower()
                else:
                    found = params.pattern in line
        except re.error as e:
            return json.dumps({"error": f"Invalid regex: {e}"})

        if found:
            matches.append({"line_number": i, "content": line.rstrip("\n\r")})
            if len(matches) >= limit:
                break

    return json.dumps({
        "total_lines": len(lines),
        "matches_found": len(matches),
        "truncated": len(matches) >= limit,
        "matches": matches,
    })


# -- jq (query JSON files) ------------------------------------------------

class JqParams(BaseModel):
    path: str = Field(description="Absolute path to a JSON file")
    query: str = Field(description="Dot-separated path to extract (e.g. '.results', '.data.items', '.[0].name'). Use '.' for the entire document.")
    max_items: Optional[int] = Field(default=20, description="If the result is an array, return at most this many items (default 20)")


@tool(
    name="jq",
    description=(
        "Query a JSON file on the local system using a dot-path expression."
        " Examples: '.' (entire doc), '.key', '.items.[0].name', '.data.results'."
    ),
    schema=JqParams,
)
async def jq(params: JqParams) -> str:
    err = _check_access(params.path)
    if err:
        return err

    try:
        with open(os.path.normpath(params.path), "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON: {e}"})

    # Navigate the dot-path
    query = params.query.strip().lstrip(".")
    current: Any = data
    if query:
        for part in query.split("."):
            if not part:
                continue
            # Handle array index: [0], [1], etc.
            idx_match = re.match(r"^\[(\d+)\]$", part)
            if idx_match:
                idx = int(idx_match.group(1))
                if not isinstance(current, list) or idx >= len(current):
                    return json.dumps({"error": f"Index {idx} out of range (length {len(current) if isinstance(current, list) else 'N/A'})"})
                current = current[idx]
            elif isinstance(current, dict) and part in current:
                current = current[part]
            elif isinstance(current, list):
                # Try array index without brackets
                try:
                    current = current[int(part)]
                except (ValueError, IndexError):
                    return json.dumps({"error": f"Key '{part}' not found. Available keys: {list(current[0].keys()) if current and isinstance(current[0], dict) else 'N/A'}"})
            else:
                available = list(current.keys()) if isinstance(current, dict) else type(current).__name__
                return json.dumps({"error": f"Key '{part}' not found. Available: {available}"})

    # Truncate arrays
    limit = max(1, params.max_items or 20)
    truncated = False
    total_items: Optional[int]
    if isinstance(current, list) and len(current) > limit:
        total_items = len(current)
        current = current[:limit]
        truncated = True
    else:
        total_items = len(current) if isinstance(current, list) else None

    result: dict = {"result": current}
    if total_items is not None:
        result["total_items"] = total_items
    if truncated:
        result["truncated"] = True
        result["items_returned"] = limit
    return json.dumps(result, indent=2, default=str)


_BUILTIN_TOOLS: List[FunctionTool] = [view, head, tail, grep, jq]

# Discovered project tools + built-ins. Computed once at import.
_REGISTERED_TOOLS_CACHE: List[FunctionTool] = discover_tools() + _BUILTIN_TOOLS

