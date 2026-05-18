from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .._function_tool import FunctionTool, tool

_ALLOWED_READ_DIRS = [
    str(Path(tempfile.gettempdir()).resolve(strict=False)),
]


def add_allowed_read_dir(path: str) -> None:
    resolved = str(Path(path).resolve(strict=False))
    if resolved not in _ALLOWED_READ_DIRS:
        _ALLOWED_READ_DIRS.append(resolved)


def _resolve_allowed_path(path: str) -> tuple[Path | None, str | None]:
    requested = Path(path).resolve(strict=False)
    allowed = any(
        requested == allowed_path or requested.is_relative_to(allowed_path)
        for allowed_dir in _ALLOWED_READ_DIRS
        for allowed_path in [Path(allowed_dir).resolve(strict=False)]
    )
    if not allowed:
        return None, json.dumps({"error": "Access denied: path is not in an allowed directory"})
    if not requested.is_file():
        return None, json.dumps({"error": f"File not found: {path}"})
    return requested, None


def _check_access(path: str) -> str | None:
    """Return an error JSON string if the path is not allowed, else None."""
    _, err = _resolve_allowed_path(path)
    return err


def _read_lines(path: Path) -> list[str]:
    """Read all lines from a file."""
    with path.open(encoding="utf-8", errors="replace") as f:
        return f.readlines()


# -- view (read file with optional line range) -----------------------------


class ViewParams(BaseModel):
    path: str = Field(description="Absolute path to the file to read")
    start_line: int | None = Field(
        default=None,
        description="1-based start line number. If omitted, reads from the beginning.",
    )
    end_line: int | None = Field(
        default=None,
        description="1-based end line number (inclusive). If omitted, reads to the end.",
    )


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
    resolved_path, err = _resolve_allowed_path(params.path)
    if err:
        return err

    lines = _read_lines(resolved_path)
    total = len(lines)
    start = (params.start_line or 1) - 1
    end = params.end_line or total
    start = max(0, min(start, total))
    end = max(start, min(end, total))

    return json.dumps(
        {
            "total_lines": total,
            "start_line": start + 1,
            "end_line": end,
            "content": "".join(lines[start:end]),
        }
    )


# -- head (first N lines) -------------------------------------------------


class HeadParams(BaseModel):
    path: str = Field(description="Absolute path to the file")
    lines: int | None = Field(
        default=10,
        description="Number of lines to return from the start (default 10)",
    )


@tool(
    name="head",
    description="Show the first N lines of a file on the local system (default 10).",
    schema=HeadParams,
)
async def head(params: HeadParams) -> str:
    resolved_path, err = _resolve_allowed_path(params.path)
    if err:
        return err

    all_lines = _read_lines(resolved_path)
    n = max(1, params.lines or 10)
    return json.dumps(
        {
            "total_lines": len(all_lines),
            "lines_returned": min(n, len(all_lines)),
            "content": "".join(all_lines[:n]),
        }
    )


# -- tail (last N lines) --------------------------------------------------


class TailParams(BaseModel):
    path: str = Field(description="Absolute path to the file")
    lines: int | None = Field(
        default=10,
        description="Number of lines to return from the end (default 10)",
    )


@tool(
    name="tail",
    description="Show the last N lines of a file on the local system (default 10).",
    schema=TailParams,
)
async def tail(params: TailParams) -> str:
    resolved_path, err = _resolve_allowed_path(params.path)
    if err:
        return err

    all_lines = _read_lines(resolved_path)
    n = max(1, params.lines or 10)
    selected = all_lines[-n:] if n < len(all_lines) else all_lines
    return json.dumps(
        {
            "total_lines": len(all_lines),
            "lines_returned": len(selected),
            "content": "".join(selected),
        }
    )


# -- grep (search file contents) ------------------------------------------


class GrepParams(BaseModel):
    path: str = Field(description="Absolute path to the file to search")
    pattern: str = Field(description="Search pattern (plain text or regex)")
    is_regex: bool | None = Field(
        default=False,
        description="Treat pattern as a regex (default: plain text)",
    )
    ignore_case: bool | None = Field(
        default=True,
        description="Case-insensitive search (default: true)",
    )
    max_results: int | None = Field(
        default=50,
        description="Maximum number of matching lines to return (default 50)",
    )


@tool(
    name="grep",
    description=(
        "Search for a pattern in a file on the local system. Returns matching"
        " lines with line numbers. Supports plain text and regex patterns."
    ),
    schema=GrepParams,
)
async def grep(params: GrepParams) -> str:
    resolved_path, err = _resolve_allowed_path(params.path)
    if err:
        return err

    lines = _read_lines(resolved_path)
    flags = re.IGNORECASE if params.ignore_case else 0
    limit = max(1, params.max_results or 50)

    matches: list[dict[str, int | str]] = []
    for i, line in enumerate(lines, 1):
        try:
            if params.is_regex:
                found = re.search(params.pattern, line, flags) is not None
            elif params.ignore_case:
                found = params.pattern.lower() in line.lower()
            else:
                found = params.pattern in line
        except re.error as exc:
            return json.dumps({"error": f"Invalid regex: {exc}"})

        if found:
            matches.append({"line_number": i, "content": line.rstrip("\n\r")})
            if len(matches) >= limit:
                break

    return json.dumps(
        {
            "total_lines": len(lines),
            "matches_found": len(matches),
            "truncated": len(matches) >= limit,
            "matches": matches,
        }
    )


# -- jq (query JSON files) ------------------------------------------------


class JqParams(BaseModel):
    path: str = Field(description="Absolute path to a JSON file")
    query: str = Field(
        description=(
            "Dot-separated path to extract (e.g. '.results', '.data.items',"
            " '.[0].name'). Use '.' for the entire document."
        )
    )
    max_items: int | None = Field(
        default=20,
        description="If the result is an array, return at most this many items (default 20)",
    )


@tool(
    name="jq",
    description=(
        "Query a JSON file on the local system using a dot-path expression."
        " Examples: '.' (entire doc), '.key', '.items.[0].name', '.data.results'."
    ),
    schema=JqParams,
)
async def jq(params: JqParams) -> str:
    resolved_path, err = _resolve_allowed_path(params.path)
    if err:
        return err

    try:
        with resolved_path.open(encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"Invalid JSON: {exc}"})

    query = params.query.strip().lstrip(".")
    current: Any = data
    if query:
        for part in query.split("."):
            if not part:
                continue

            idx_match = re.match(r"^\[(\d+)\]$", part)
            if idx_match:
                idx = int(idx_match.group(1))
                if not isinstance(current, list) or idx >= len(current):
                    length = len(current) if isinstance(current, list) else "N/A"
                    return json.dumps({"error": f"Index {idx} out of range (length {length})"})
                current = current[idx]
            elif isinstance(current, dict) and part in current:
                current = current[part]
            elif isinstance(current, list):
                try:
                    current = current[int(part)]
                except (ValueError, IndexError):
                    available = (
                        list(current[0].keys())
                        if current and isinstance(current[0], dict)
                        else "N/A"
                    )
                    return json.dumps(
                        {"error": f"Key '{part}' not found. Available keys: {available}"}
                    )
            else:
                available = (
                    list(current.keys()) if isinstance(current, dict) else type(current).__name__
                )
                return json.dumps({"error": f"Key '{part}' not found. Available: {available}"})

    limit = max(1, params.max_items or 20)
    truncated = False
    total_items: int | None
    if isinstance(current, list) and len(current) > limit:
        total_items = len(current)
        current = current[:limit]
        truncated = True
    else:
        total_items = len(current) if isinstance(current, list) else None

    result: dict[str, Any] = {"result": current}
    if total_items is not None:
        result["total_items"] = total_items
    if truncated:
        result["truncated"] = True
        result["items_returned"] = limit
    return json.dumps(result, indent=2, default=str)


BUILTIN_TOOLS: list[FunctionTool] = [view, head, tail, grep, jq]
