import importlib.util
import inspect
import os
import sys
from pathlib import Path

from agent_framework import FunctionTool

from .._function_tool import tool
from .._logger import logger


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
    tools: list[FunctionTool] = []
    project_src_dir = str(app_root)
    tools_dir = os.path.join(project_src_dir, "tools")

    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)

    print(f"[Tool Discovery] Looking for tools in: {tools_dir}")
    print(f"[Tool Discovery] Directory exists: {os.path.exists(tools_dir)}")

    if not os.path.exists(tools_dir):
        print(f"[Tool Discovery] WARNING: Tools directory not found: {tools_dir}")
        return tools

    files = sorted(f for f in os.listdir(tools_dir) if f.endswith(".py") and not f.startswith("_"))
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

            picked: FunctionTool | None = None

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
        except Exception as exc:
            import traceback

            print(f"[Tool Discovery] ERROR loading {filename}: {exc}")
            traceback.print_exc()
            logger.error("Failed to load tool from %s: %s", filename, exc)

    return tools
