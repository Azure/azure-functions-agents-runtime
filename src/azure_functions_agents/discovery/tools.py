import importlib.util
import inspect
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_type_hints

from pydantic import BaseModel

from .._function_tool import (
    WorkflowTool,
    get_workflow_tool_handler,
    get_workflow_tool_metadata,
    tool,
)
from .._logger import logger
from .._tool_descriptor import ToolDescriptor, is_harness_object, warn_unsupported_tool

type _CachedProjectTools = tuple[tuple[ToolDescriptor, ...], tuple[WorkflowTool, ...]]


@dataclass(frozen=True)
class ProjectTools:
    """Tool inventories discovered from ``tools/``."""

    user_tools: list[ToolDescriptor]
    workflow_tools: list[WorkflowTool]
    failed_loads: list[tuple[str, str]]


_DISCOVERED_TOOLS_CACHE: dict[Path, _CachedProjectTools] = {}


@dataclass
class ToolDiscoveryResult:
    """Result of user tool discovery including successes and failures."""

    tools: list[ToolDescriptor]
    failed_loads: list[tuple[str, str]]  # [(filename, error_message), ...]


def _single_basemodel_parameter(fn: Callable[..., Any]) -> type[BaseModel] | None:
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


def _copy_cached(cached: _CachedProjectTools) -> ProjectTools:
    return ProjectTools(
        user_tools=list(cached[0]),
        workflow_tools=list(cached[1]),
        failed_loads=[],
    )


def _is_workflow_marked(obj: object) -> bool:
    return get_workflow_tool_metadata(obj) is not None


def _workflow_tool_from_member(module_name: str, name: str, obj: object) -> WorkflowTool | None:
    if is_harness_object(obj):
        warn_unsupported_tool()
        return None

    handler: Callable[..., Any] | None = None
    default_tool_name = name
    default_description = ""

    if isinstance(obj, ToolDescriptor):
        metadata = get_workflow_tool_metadata(obj)
        default_tool_name = obj.name
        default_description = obj.description
        handler = obj.func
        if metadata is None and handler is not None:
            metadata = get_workflow_tool_metadata(handler)
    elif inspect.isfunction(obj) and obj.__module__ == module_name:
        metadata = get_workflow_tool_metadata(obj)
        handler = obj
        default_description = (obj.__doc__ or "").strip()
    else:
        return None

    if metadata is None:
        return None

    if handler is not None:
        handler = get_workflow_tool_handler(handler)

    tool_name = metadata.name or default_tool_name
    description = metadata.description or default_description
    return WorkflowTool(
        name=tool_name,
        description=description,
        handler=handler,
        public=metadata.public,
        retry=metadata.retry,
        timeout=metadata.timeout,
    )


def discover_project_tools(app_root: Path) -> ProjectTools:
    """Discover neutral tools and workflow declarations, preferring decorated tools."""
    resolved_root = Path(app_root).resolve()
    cached_tools = _DISCOVERED_TOOLS_CACHE.get(resolved_root)
    if cached_tools is not None:
        return _copy_cached(cached_tools)

    tools: list[ToolDescriptor] = []
    workflow_tools: list[WorkflowTool] = []
    failed_loads: list[tuple[str, str]] = []
    project_src_dir = str(resolved_root)
    tools_dir = os.path.join(project_src_dir, "tools")

    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)

    logger.debug("Looking for tools in %s", tools_dir)
    logger.debug("Tools directory exists: %s", os.path.exists(tools_dir))

    if not os.path.exists(tools_dir):
        logger.warning("Tools directory not found: %s", tools_dir)
        _DISCOVERED_TOOLS_CACHE[resolved_root] = (tuple(tools), tuple(workflow_tools))
        return ProjectTools(
            user_tools=list(tools),
            workflow_tools=list(workflow_tools),
            failed_loads=[],
        )

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

            members = []
            for name, obj in inspect.getmembers(module):
                if name.startswith("_"):
                    continue
                if is_harness_object(obj):
                    warn_unsupported_tool()
                    continue
                members.append((name, obj))

            picked: ToolDescriptor | None = None
            for name, obj in members:
                workflow_candidate = _workflow_tool_from_member(module_name, name, obj)
                if workflow_candidate is not None:
                    workflow_tools.append(workflow_candidate)

            for _name, obj in members:
                if isinstance(obj, ToolDescriptor):
                    picked = obj
                    logger.debug("Loaded tool %s", obj.name)
                    break

            # Fallback: first plain function defined in the module.
            if picked is None:
                local_functions = [
                    (name, obj)
                    for name, obj in members
                    if inspect.isfunction(obj)
                    and obj.__module__ == module_name
                    and not _is_workflow_marked(obj)
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
            error_msg = f"{type(exc).__name__}: {exc}"
            failed_loads.append((filename, error_msg))
            logger.warning("Failed to load tool from %s: %s", filename, exc, exc_info=True)

    _DISCOVERED_TOOLS_CACHE[resolved_root] = (tuple(tools), tuple(workflow_tools))
    logger.info("Discovered %d user tool(s) from %s", len(tools), tools_dir)
    if failed_loads:
        logger.warning("Failed to load %d tool file(s)", len(failed_loads))
    return ProjectTools(
        user_tools=list(tools),
        workflow_tools=list(workflow_tools),
        failed_loads=failed_loads,
    )


def discover_user_tools(app_root: Path) -> ToolDiscoveryResult:
    """Return normal Python tool descriptors from ``tools/``."""
    project_tools = discover_project_tools(app_root)
    return ToolDiscoveryResult(
        tools=project_tools.user_tools,
        failed_loads=project_tools.failed_loads,
    )
