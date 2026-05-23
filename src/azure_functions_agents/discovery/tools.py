import importlib.util
import inspect
import os
import sys
from pathlib import Path
from typing import get_type_hints

from agent_framework import FunctionTool
from pydantic import BaseModel

from .._function_tool import tool
from .._logger import logger

_DISCOVERED_TOOLS_CACHE: dict[Path, list[FunctionTool]] = {}


def _single_basemodel_parameter(fn: object) -> type[BaseModel] | None:
    try:
        signature = inspect.signature(fn)
        type_hints = get_type_hints(fn)
    except Exception:
        return None

    parameters = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    ]
    if len(parameters) != 1:
        return None

    annotation = type_hints.get(parameters[0].name, parameters[0].annotation)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    return None


def clear_tool_discovery_cache() -> None:
    """Clear the cached user-tool discovery results."""
    _DISCOVERED_TOOLS_CACHE.clear()


def discover_user_tools(app_root: Path) -> list[FunctionTool]:
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
    resolved_root = Path(app_root).resolve()
    cached_tools = _DISCOVERED_TOOLS_CACHE.get(resolved_root)
    if cached_tools is not None:
        return list(cached_tools)

    tools: list[FunctionTool] = []
    project_src_dir = str(resolved_root)
    tools_dir = os.path.join(project_src_dir, "tools")

    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)

    logger.debug("Looking for tools in %s", tools_dir)
    logger.debug("Tools directory exists: %s", os.path.exists(tools_dir))

    if not os.path.exists(tools_dir):
        logger.warning("Tools directory not found: %s", tools_dir)
        _DISCOVERED_TOOLS_CACHE[resolved_root] = tools
        return list(tools)

    files = sorted(f for f in os.listdir(tools_dir) if f.endswith(".py") and not f.startswith("_"))
    logger.debug("Python tool files found in %s: %s", tools_dir, files)

    for filename in files:
        filepath = os.path.join(tools_dir, filename)
        module_name = filename[:-3]
        logger.debug("Loading tool module %s from %s", module_name, filepath)
        try:
            spec = importlib.util.spec_from_file_location(module_name, filepath)
            if spec is None or spec.loader is None:
                logger.warning("Could not create import spec for %s", filename)
                continue

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            picked: FunctionTool | None = None

            # Prefer module-level ``@tool``-decorated values (FunctionTool
            # instances) — they carry their own name/description/schema.
            for name, obj in inspect.getmembers(module):
                if name.startswith("_"):
                    continue
                if isinstance(obj, FunctionTool):
                    picked = obj
                    logger.debug("Loaded FunctionTool %s", obj.name)
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
                    schema = _single_basemodel_parameter(fn)
                    if schema is not None:
                        picked = tool(fn, name=name, description=description, schema=schema)
                    else:
                        picked = tool(fn, name=name, description=description)
                    logger.debug("Auto-wrapped tool %s with description %s", name, description)

            if picked is not None:
                tools.append(picked)
        except Exception as exc:
            logger.warning("Failed to load tool from %s: %s", filename, exc, exc_info=True)

    _DISCOVERED_TOOLS_CACHE[resolved_root] = tools
    logger.info("Discovered %d user tool(s) from %s", len(tools), tools_dir)
    return list(tools)
