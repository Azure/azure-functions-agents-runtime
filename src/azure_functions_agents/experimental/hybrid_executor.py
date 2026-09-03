"""Standalone sandbox-side executor for the experimental hybrid tool protocol."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import enum
import hashlib
import hmac
import importlib
import importlib.util
import inspect
import io
import json
import math
import os
import queue
import re
import signal
import site
import stat
import subprocess
import sys
import threading
import time
import types
import uuid
import zipfile
from collections.abc import Callable, Mapping, Sequence
from itertools import islice
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

PROTOCOL_VERSION = "1"
MANIFEST_FILENAME = "manifest.json"
READINESS_FILENAME = "ready.json"
PACKAGE_VERIFICATION_FILENAME = "package-verified.json"
STARTUP_FAILURE_FILENAME = "startup-failure.json"
PID_FILENAME = "pid.json"
REQUEST_DIRECTORY = "requests"
RESULT_DIRECTORY = "results"
SHUTDOWN_FILENAME = "shutdown"

MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
# The controller emits a standard, non-ZIP64 archive and permits every entry
# representable by that format. Keep this standalone executor bound aligned.
MAX_ARCHIVE_MEMBERS = 0xFFFF
MAX_EXTRACTED_TOTAL_BYTES = 256 * 1024 * 1024
MAX_EXTRACTED_MEMBER_BYTES = MAX_EXTRACTED_TOTAL_BYTES
MAX_COMPRESSION_RATIO = 200
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MAX_ARGUMENT_BYTES = 512 * 1024
MAX_VALUE_BYTES = 512 * 1024
MAX_STREAM_BYTES = 64 * 1024
MAX_FILE_BYTES = 1024 * 1024
MAX_WRITE_BYTES = 1024 * 1024
MAX_SEARCH_FILE_BYTES = 256 * 1024
MAX_SEARCH_RESULTS = 100
MAX_SEARCH_CANDIDATES = 10_000
MAX_CALL_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 0.05

_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_APP_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_CALL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_TRACEPARENT = re.compile(
    r"^(?!ff)[0-9a-f]{2}-(?!0{32})[0-9a-f]{32}-(?!0{16})[0-9a-f]{16}-[0-9a-f]{2}$"
)
_WORKFLOW_MARKER = "__azure_functions_agents_workflow_tool__"
_SIGALRM: Any = signal.__dict__.get("SIGALRM")
_ITIMER_REAL: Any = signal.__dict__.get("ITIMER_REAL")
_SETITIMER: Any = signal.__dict__.get("setitimer")
_SIGKILL: Any = signal.__dict__.get("SIGKILL")
_KILLPG: Any = os.__dict__.get("killpg")


class ExecutorProtocolError(ValueError):
    """A sandbox executor input violates its private protocol."""


class PackageDigestMismatchError(ExecutorProtocolError):
    """The delivered application archive does not match its controller digest."""


class ExecutorDeadlineError(TimeoutError):
    """A sandbox tool call exceeded its bounded deadline."""

    def __init__(
        self,
        message: str,
        *,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


class ExecutorToolError(RuntimeError):
    """A customer tool failed after producing bounded captured output."""

    def __init__(self, message: str, *, stdout: str, stderr: str) -> None:
        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr


@dataclasses.dataclass(frozen=True)
class _SandboxTool:
    name: str
    description: str
    parameters: dict[str, object]
    invoke: Callable[[dict[str, object]], object]
    provenance: str = "local"


class _CappedTextWriter(io.TextIOBase):
    def __init__(self, maximum_bytes: int) -> None:
        self._maximum_bytes = maximum_bytes
        self._content = bytearray()
        self.truncated = False

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        encoded = text.encode("utf-8", errors="replace")
        remaining = self._maximum_bytes - len(self._content)
        if remaining > 0:
            self._content.extend(encoded[:remaining])
        if len(encoded) > remaining:
            self.truncated = True
        return len(text)

    def value(self) -> str:
        value = bytes(self._content).decode("utf-8", errors="replace")
        if self.truncated:
            value += "\n[output truncated]"
        return value


def _object_pairs_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ExecutorProtocolError("duplicate JSON key")
        result[key] = value
    return result


def _canonical_json_bytes(value: object) -> bytes:
    _assert_json_value(value)
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _assert_json_value(value: object, *, depth: int = 0) -> None:
    if depth > 64:
        raise ExecutorProtocolError("JSON value is nested too deeply")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExecutorProtocolError("non-finite JSON number")
        return
    if isinstance(value, list | tuple):
        for item in value:
            _assert_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ExecutorProtocolError("JSON object key is not a string")
            _assert_json_value(item, depth=depth + 1)
        return
    raise ExecutorProtocolError("value is not JSON-safe")


def _atomic_write(path: Path, payload: bytes, *, preserve_existing: bool = False) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        if preserve_existing:
            try:
                os.link(temporary, path)
            except FileExistsError:
                return False
            except OSError:
                if path.exists():
                    return False
                os.replace(temporary, path)
            else:
                temporary.unlink()
        else:
            os.replace(temporary, path)
        _fsync_directory(path.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def securely_extract_application_archive(archive: Path, extraction_root: Path) -> None:
    """Extract one bounded application archive without following archive links."""
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ExecutorProtocolError("application archive exceeds the byte limit")
    extraction_root.mkdir(parents=True, exist_ok=True)
    if any(extraction_root.iterdir()):
        raise ExecutorProtocolError("extraction root must be empty")
    root = extraction_root.resolve()
    total_size = 0
    with zipfile.ZipFile(archive) as package:
        members = package.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ExecutorProtocolError("application archive contains too many members")
        for member in members:
            relative = _validated_archive_member(member)
            total_size += member.file_size
            if total_size > MAX_EXTRACTED_TOTAL_BYTES:
                raise ExecutorProtocolError("application archive expands beyond the byte limit")
            destination = root.joinpath(*relative.parts)
            if not _is_beneath(destination, root):
                raise ExecutorProtocolError("application archive member escapes extraction root")
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with package.open(member) as source, destination.open("xb") as output:
                copied = 0
                while chunk := source.read(64 * 1024):
                    copied += len(chunk)
                    if copied > MAX_EXTRACTED_MEMBER_BYTES:
                        raise ExecutorProtocolError(
                            "application archive member exceeds the byte limit"
                        )
                    output.write(chunk)


def _validated_archive_member(member: zipfile.ZipInfo) -> PurePosixPath:
    if "\\" in member.filename or "\x00" in member.filename:
        raise ExecutorProtocolError("application archive member name is unsafe")
    relative = PurePosixPath(member.filename)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ExecutorProtocolError("application archive member name is unsafe")
    mode = member.external_attr >> 16
    kind = stat.S_IFMT(mode)
    if kind not in {0, stat.S_IFREG, stat.S_IFDIR} or stat.S_ISLNK(mode):
        raise ExecutorProtocolError("application archive contains a link or special file")
    if member.file_size > MAX_EXTRACTED_MEMBER_BYTES:
        raise ExecutorProtocolError("application archive member exceeds the byte limit")
    if (
        member.compress_size > 0
        and member.file_size > 1024 * 1024
        and member.file_size / member.compress_size > MAX_COMPRESSION_RATIO
    ):
        raise ExecutorProtocolError("application archive member compression ratio is unsafe")
    return relative


def _is_beneath(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root)
    except ValueError:
        return False
    return True


def _configure_application_imports(app_root: Path) -> None:
    site_packages = app_root / ".python_packages" / "lib" / "site-packages"
    tools_root = app_root / "tools"
    for candidate in (app_root, site_packages, tools_root):
        if candidate.is_dir():
            site.addsitedir(str(candidate))


def discover_sandbox_tools(app_root: Path, workspace_root: Path) -> list[_SandboxTool]:
    """Import each customer tool module once and return its first normal tool."""
    _configure_application_imports(app_root)
    function_tool_type = _load_function_tool_type()
    discovered: list[_SandboxTool] = []
    tools_root = app_root / "tools"
    if tools_root.is_dir():
        for tool_file in sorted(tools_root.glob("*.py")):
            if tool_file.name.startswith("_"):
                continue
            module_name = tool_file.stem
            module = _import_tool_module(module_name, tool_file)
            tool = _first_normal_tool(module_name, module, function_tool_type)
            if tool is not None:
                discovered.append(tool)
    discovered.extend(_generic_tools(workspace_root))
    names = [tool.name for tool in discovered]
    if len(names) != len(set(names)):
        raise ExecutorProtocolError("sandbox tool names must be unique")
    return discovered


def _load_function_tool_type() -> Any | None:
    try:
        framework = importlib.import_module("agent_framework")
    except ImportError:
        return None
    candidate = getattr(framework, "FunctionTool", None)
    return candidate if isinstance(candidate, type) else None


def _import_tool_module(module_name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ExecutorProtocolError("customer tool module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _first_normal_tool(
    module_name: str,
    module: types.ModuleType,
    function_tool_type: Any | None,
) -> _SandboxTool | None:
    if function_tool_type is not None:
        for name, candidate in inspect.getmembers(module):
            if name.startswith("_") or not isinstance(candidate, function_tool_type):
                continue
            return _framework_tool(candidate)
    local_functions = [
        (name, candidate)
        for name, candidate in inspect.getmembers(module, inspect.isfunction)
        if candidate.__module__ == module_name
        and not name.startswith("_")
        and getattr(candidate, _WORKFLOW_MARKER, None) is None
    ]
    if not local_functions:
        return None
    name, function = local_functions[0]
    description = (function.__doc__ or f"Tool: {name}").strip()
    return _plain_function_tool(name, description, function, function_tool_type)


def _framework_tool(candidate: object) -> _SandboxTool:
    name = getattr(candidate, "name", None)
    description = getattr(candidate, "description", None)
    parameters_method = getattr(candidate, "parameters", None)
    invoke_method = getattr(candidate, "invoke", None)
    if (
        not isinstance(name, str)
        or not _TOOL_NAME.fullmatch(name)
        or not isinstance(description, str)
        or not callable(parameters_method)
        or not callable(invoke_method)
    ):
        raise ExecutorProtocolError("FunctionTool has an invalid executable shape")
    parameters = parameters_method()
    if not isinstance(parameters, dict):
        raise ExecutorProtocolError("FunctionTool parameter schema is not an object")

    def invoke(arguments: dict[str, object]) -> object:
        return invoke_method(arguments=arguments, skip_parsing=True)

    return _SandboxTool(name, description, parameters, invoke)


def _plain_function_tool(
    name: str,
    description: str,
    function: Callable[..., object],
    function_tool_type: Any | None,
) -> _SandboxTool:
    if function_tool_type is not None:
        schema_model = _single_model_parameter(function)
        if schema_model is not None:
            parameters = schema_model.model_json_schema()

            def invoke(arguments: dict[str, object]) -> object:
                return function(schema_model(**arguments))

            return _SandboxTool(name, description, parameters, invoke)
        wrapped = function_tool_type(name=name, description=description, func=function)
        return _framework_tool(wrapped)
    return _SandboxTool(
        name, description, _signature_schema(name, function), lambda args: function(**args)
    )


def _single_model_parameter(function: Callable[..., object]) -> Any | None:
    try:
        parameters = list(inspect.signature(function).parameters.values())
        hints = get_type_hints(function)
        pydantic = importlib.import_module("pydantic")
        base_model = pydantic.BaseModel
    except (ImportError, TypeError, ValueError):
        return None
    if len(parameters) != 1:
        return None
    parameter = parameters[0]
    annotation = hints.get(parameter.name, parameter.annotation)
    if isinstance(annotation, type) and issubclass(annotation, base_model):
        return annotation
    return None


def _signature_schema(name: str, function: Callable[..., object]) -> dict[str, object]:
    try:
        signature = inspect.signature(function)
        hints = get_type_hints(function)
    except (TypeError, ValueError, NameError) as exc:
        raise ExecutorProtocolError("plain tool signature cannot be inspected") from exc
    properties: dict[str, object] = {}
    required: list[str] = []
    for parameter_name, parameter in signature.parameters.items():
        if parameter.kind not in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }:
            raise ExecutorProtocolError(
                "plain tools cannot use variadic or positional-only parameters"
            )
        annotation = hints.get(parameter_name, parameter.annotation)
        property_schema = _annotation_schema(annotation)
        property_schema["title"] = parameter_name.replace("_", " ").title()
        if parameter.default is inspect.Parameter.empty:
            required.append(parameter_name)
        else:
            _assert_json_value(parameter.default)
            property_schema["default"] = parameter.default
        properties[parameter_name] = property_schema
    schema: dict[str, object] = {
        "properties": properties,
        "title": f"{name}_input",
        "type": "object",
    }
    if required:
        schema["required"] = required
    return schema


def _annotation_schema(annotation: object) -> dict[str, object]:
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {}
    if annotation is str:
        return {"type": "string"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is dict or get_origin(annotation) in {dict, Mapping}:
        return {"type": "object"}
    origin = get_origin(annotation)
    if origin in {list, Sequence, tuple}:
        arguments = get_args(annotation)
        item = _annotation_schema(arguments[0]) if arguments else {}
        return {"items": item, "type": "array"}
    if origin in {types.UnionType, Union}:
        choices = [_annotation_schema(item) for item in get_args(annotation)]
        return {"anyOf": choices}
    if origin is Literal:
        values = list(get_args(annotation))
        _assert_json_value(values)
        return {"enum": values}
    return {}


def _generic_tools(workspace_root: Path) -> list[_SandboxTool]:
    return [
        _SandboxTool(
            "run_shell",
            "Run a bounded shell command in the sandbox workspace.",
            {
                "additionalProperties": False,
                "properties": {
                    "command": {"maxLength": 32768, "minLength": 1, "type": "string"},
                    "timeout_seconds": {
                        "default": 10.0,
                        "maximum": MAX_CALL_SECONDS,
                        "minimum": 0.1,
                        "type": "number",
                    },
                },
                "required": ["command"],
                "type": "object",
            },
            lambda arguments: _run_shell(workspace_root, arguments),
            "generic",
        ),
        _SandboxTool(
            "read_file",
            "Read one bounded UTF-8 file beneath the sandbox workspace.",
            {
                "additionalProperties": False,
                "properties": {"path": {"maxLength": 4096, "minLength": 1, "type": "string"}},
                "required": ["path"],
                "type": "object",
            },
            lambda arguments: _read_file(workspace_root, arguments),
            "generic",
        ),
        _SandboxTool(
            "write_file",
            "Atomically write one bounded UTF-8 file beneath the sandbox workspace.",
            {
                "additionalProperties": False,
                "properties": {
                    "content": {"maxLength": MAX_WRITE_BYTES, "type": "string"},
                    "path": {"maxLength": 4096, "minLength": 1, "type": "string"},
                },
                "required": ["path", "content"],
                "type": "object",
            },
            lambda arguments: _write_file(workspace_root, arguments),
            "generic",
        ),
        _SandboxTool(
            "search_files",
            "Search bounded workspace files by glob and optional literal text.",
            {
                "additionalProperties": False,
                "properties": {
                    "glob": {
                        "default": "**/*",
                        "maxLength": 4096,
                        "minLength": 1,
                        "type": "string",
                    },
                    "max_results": {
                        "default": 50,
                        "maximum": MAX_SEARCH_RESULTS,
                        "minimum": 1,
                        "type": "integer",
                    },
                    "text": {"maxLength": 4096, "type": ["string", "null"]},
                },
                "type": "object",
            },
            lambda arguments: _search_files(workspace_root, arguments),
            "generic",
        ),
    ]


def _validated_arguments(
    arguments: dict[str, object],
    *,
    required: set[str],
    optional: set[str],
) -> None:
    keys = set(arguments)
    if not required <= keys or not keys <= required | optional:
        raise ExecutorProtocolError("generic tool arguments are invalid")


def _workspace_path(workspace_root: Path, raw: object, *, for_write: bool = False) -> Path:
    if not isinstance(raw, str) or not raw or len(raw) > 4096:
        raise ExecutorProtocolError("workspace path is invalid")
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise ExecutorProtocolError("workspace path escapes the workspace")
    root = workspace_root.resolve(strict=True)
    current = root
    for part in relative.parts:
        current = current / part
        if (current.exists() or current.is_symlink()) and stat.S_ISLNK(current.lstat().st_mode):
            raise ExecutorProtocolError("workspace path contains a symbolic link")
    candidate = root.joinpath(*relative.parts)
    resolved = candidate.resolve(strict=not for_write)
    if not _is_beneath(resolved, root):
        raise ExecutorProtocolError("workspace path escapes the workspace")
    return candidate


def _read_file(workspace_root: Path, arguments: dict[str, object]) -> dict[str, object]:
    _validated_arguments(arguments, required={"path"}, optional=set())
    path = _workspace_path(workspace_root, arguments["path"])
    if not path.is_file():
        raise ExecutorProtocolError("workspace path is not a regular file")
    content = _read_bounded_file(path, MAX_FILE_BYTES)
    if len(content) > MAX_FILE_BYTES:
        raise ExecutorProtocolError("workspace file exceeds the byte limit")
    return {"content": content.decode("utf-8", errors="replace"), "path": str(arguments["path"])}


def _write_file(workspace_root: Path, arguments: dict[str, object]) -> dict[str, object]:
    _validated_arguments(arguments, required={"path", "content"}, optional=set())
    content = arguments["content"]
    if not isinstance(content, str):
        raise ExecutorProtocolError("file content must be a string")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_WRITE_BYTES:
        raise ExecutorProtocolError("file content exceeds the byte limit")
    path = _workspace_path(workspace_root, arguments["path"], for_write=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    _workspace_path(workspace_root, arguments["path"], for_write=True)
    _atomic_write(path, encoded)
    return {"bytes_written": len(encoded), "path": str(arguments["path"])}


def _search_files(workspace_root: Path, arguments: dict[str, object]) -> dict[str, object]:
    _validated_arguments(arguments, required=set(), optional={"glob", "text", "max_results"})
    pattern = arguments.get("glob", "**/*")
    text = arguments.get("text")
    maximum = arguments.get("max_results", 50)
    if (
        not isinstance(pattern, str)
        or not pattern
        or Path(pattern).is_absolute()
        or ".." in Path(pattern).parts
    ):
        raise ExecutorProtocolError("search glob is invalid")
    if text is not None and not isinstance(text, str):
        raise ExecutorProtocolError("search text is invalid")
    if (
        not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or not 1 <= maximum <= MAX_SEARCH_RESULTS
    ):
        raise ExecutorProtocolError("search result limit is invalid")
    root = workspace_root.resolve(strict=True)
    matches: list[dict[str, object]] = []
    paths = list(islice(root.glob(pattern, recurse_symlinks=False), MAX_SEARCH_CANDIDATES + 1))
    truncated = len(paths) > MAX_SEARCH_CANDIDATES
    for path in sorted(paths[:MAX_SEARCH_CANDIDATES]):
        try:
            relative = path.relative_to(root)
            safe_path = _workspace_path(root, str(relative))
        except (ExecutorProtocolError, ValueError, OSError):
            continue
        if not safe_path.is_file():
            continue
        match: dict[str, object] = {"path": relative.as_posix()}
        if text is not None:
            content = _read_bounded_file(safe_path, MAX_SEARCH_FILE_BYTES)
            if len(content) > MAX_SEARCH_FILE_BYTES:
                continue
            decoded = content.decode("utf-8", errors="replace")
            if text not in decoded:
                continue
            match["line_numbers"] = [
                index for index, line in enumerate(decoded.splitlines(), start=1) if text in line
            ][:100]
        matches.append(match)
        if len(matches) >= maximum:
            truncated = True
            break
    return {"matches": matches, "truncated": truncated}


def _read_bounded_file(path: Path, maximum_bytes: int) -> bytes:
    with path.open("rb") as source:
        return source.read(maximum_bytes + 1)


def _run_shell(workspace_root: Path, arguments: dict[str, object]) -> dict[str, object]:
    _validated_arguments(arguments, required={"command"}, optional={"timeout_seconds"})
    command = arguments["command"]
    timeout = arguments.get("timeout_seconds", 10.0)
    if not isinstance(command, str) or not command or len(command) > 32768:
        raise ExecutorProtocolError("shell command is invalid")
    if (
        not isinstance(timeout, int | float)
        or isinstance(timeout, bool)
        or not math.isfinite(float(timeout))
        or not 0 < float(timeout) <= MAX_CALL_SECONDS
    ):
        raise ExecutorProtocolError("shell timeout is invalid")
    process = subprocess.Popen(
        ["/bin/sh", "-c", command],
        cwd=workspace_root,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr, timed_out = _communicate_capped(process, float(timeout))
    if timed_out:
        raise ExecutorDeadlineError(
            "shell command exceeded its timeout",
            stdout=stdout,
            stderr=stderr,
            exit_code=process.returncode,
        )
    return {
        "exit_code": process.returncode,
        "stderr": stderr,
        "stdout": stdout,
    }


def _communicate_capped(
    process: subprocess.Popen[bytes],
    timeout: float,
) -> tuple[str, str, bool]:
    buffers = [bytearray(), bytearray()]
    streams = [process.stdout, process.stderr]
    threads: list[threading.Thread] = []

    def drain(stream: Any, target: bytearray) -> None:
        while chunk := stream.read(64 * 1024):
            remaining = MAX_STREAM_BYTES - len(target)
            if remaining > 0:
                target.extend(chunk[:remaining])

    for stream, target in zip(streams, buffers, strict=True):
        thread = threading.Thread(target=drain, args=(stream, target), daemon=True)
        thread.start()
        threads.append(thread)
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        if os.name == "posix" and _KILLPG is not None and _SIGKILL is not None:
            _KILLPG(process.pid, _SIGKILL)
        else:
            process.kill()
        process.wait()
    for thread in threads:
        thread.join(timeout=1.0)
    return (
        bytes(buffers[0]).decode("utf-8", errors="replace"),
        bytes(buffers[1]).decode("utf-8", errors="replace"),
        timed_out,
    )


def _json_safe_result(value: object, *, depth: int = 0, seen: set[int] | None = None) -> object:
    if depth > 32:
        raise ExecutorProtocolError("tool result is nested too deeply")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8", errors="replace")) > MAX_VALUE_BYTES:
            raise ExecutorProtocolError("tool result string exceeds the byte limit")
        return value
    if isinstance(value, int):
        if value.bit_length() > MAX_VALUE_BYTES * 3:
            raise ExecutorProtocolError("tool result integer exceeds the byte limit")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExecutorProtocolError("tool result contains a non-finite number")
        return value
    scalar = _json_safe_special_scalar(value, depth, seen)
    if scalar is not _NOT_JSON_SCALAR:
        return scalar
    return _json_safe_container(value, depth=depth, seen=seen)


_NOT_JSON_SCALAR = object()


def _json_safe_special_scalar(
    value: object,
    depth: int,
    seen: set[int] | None,
) -> object:
    if isinstance(value, enum.Enum):
        return _json_safe_result(value.value, depth=depth + 1, seen=seen)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return _NOT_JSON_SCALAR


def _json_safe_container(value: object, *, depth: int, seen: set[int] | None) -> object:
    identity = id(value)
    active = seen if seen is not None else set()
    if identity in active:
        raise ExecutorProtocolError("tool result contains a cycle")
    active.add(identity)
    try:
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            fields = {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
            return _json_safe_result(fields, depth=depth + 1, seen=active)
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return _json_safe_result(model_dump(mode="json"), depth=depth + 1, seen=active)
        if isinstance(value, Mapping):
            if len(value) > MAX_SEARCH_CANDIDATES:
                raise ExecutorProtocolError("tool result object has too many members")
            result: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ExecutorProtocolError("tool result object key is not a string")
                result[key] = _json_safe_result(item, depth=depth + 1, seen=active)
            return result
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            if len(value) > MAX_SEARCH_CANDIDATES:
                raise ExecutorProtocolError("tool result array has too many members")
            return [_json_safe_result(item, depth=depth + 1, seen=active) for item in value]
    finally:
        active.discard(identity)
    raise ExecutorProtocolError("tool result is not JSON-safe")


def _assert_value_size(value: object) -> None:
    if len(_canonical_json_bytes(value)) > MAX_VALUE_BYTES:
        raise ExecutorProtocolError("tool result exceeds the byte limit")


def _assert_matching_call_id(request: dict[str, object], call_id: str) -> None:
    if request["call_id"] != call_id:
        raise ExecutorProtocolError("request filename does not match call identifier")


def _copy_request_arguments(request: dict[str, object]) -> dict[str, object]:
    arguments = request["arguments"]
    if not isinstance(arguments, dict):
        raise ExecutorProtocolError("request arguments must be an object")
    return dict(arguments)


def _parse_request(payload: bytes) -> dict[str, object]:
    if len(payload) > MAX_REQUEST_BYTES:
        raise ExecutorProtocolError("request exceeds the byte limit")
    try:
        decoded = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_object_pairs_without_duplicates
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExecutorProtocolError("request is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise ExecutorProtocolError("request must be a JSON object")
    expected = {
        "arguments",
        "call_id",
        "deadline_unix_seconds",
        "operation_id",
        "protocol_version",
        "tool_name",
        "traceparent",
    }
    if set(decoded) != expected:
        raise ExecutorProtocolError("request fields are invalid")
    if decoded["protocol_version"] != PROTOCOL_VERSION:
        raise ExecutorProtocolError("request protocol version is unsupported")
    call_id = decoded["call_id"]
    tool_name = decoded["tool_name"]
    operation_id = decoded["operation_id"]
    traceparent = decoded["traceparent"]
    deadline = decoded["deadline_unix_seconds"]
    arguments = decoded["arguments"]
    if not isinstance(call_id, str) or not _CALL_ID.fullmatch(call_id):
        raise ExecutorProtocolError("request call identifier is invalid")
    if not isinstance(tool_name, str) or not _TOOL_NAME.fullmatch(tool_name):
        raise ExecutorProtocolError("request tool name is invalid")
    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128:
        raise ExecutorProtocolError("request operation identifier is invalid")
    if traceparent is not None and (
        not isinstance(traceparent, str) or not _TRACEPARENT.fullmatch(traceparent)
    ):
        raise ExecutorProtocolError("request traceparent is invalid")
    if (
        not isinstance(deadline, int | float)
        or isinstance(deadline, bool)
        or not math.isfinite(float(deadline))
        or float(deadline) <= 0
    ):
        raise ExecutorProtocolError("request deadline is invalid")
    if not isinstance(arguments, dict):
        raise ExecutorProtocolError("request arguments must be an object")
    _assert_json_value(arguments)
    if len(_canonical_json_bytes(arguments)) > MAX_ARGUMENT_BYTES:
        raise ExecutorProtocolError("request arguments exceed the byte limit")
    return decoded


def _invoke_tool(
    tool: _SandboxTool,
    arguments: dict[str, object],
    timeout_seconds: float,
) -> tuple[object, str, str]:
    if tool.name == "run_shell" and tool.provenance == "generic":
        return _invoke_tool_without_alarm(tool, arguments)
    if (
        _SIGALRM is not None
        and _SETITIMER is not None
        and threading.current_thread() is threading.main_thread()
    ):
        return _invoke_tool_with_alarm(tool, arguments, timeout_seconds)
    return _invoke_tool_with_thread(tool, arguments, timeout_seconds)


def _resolve_tool_value(tool: _SandboxTool, arguments: dict[str, object]) -> object:
    value = tool.invoke(arguments)
    if inspect.isawaitable(value):
        return asyncio.run(_await_tool_result(value))
    return value


def _invoke_tool_without_alarm(
    tool: _SandboxTool,
    arguments: dict[str, object],
) -> tuple[object, str, str]:
    stdout = _CappedTextWriter(MAX_STREAM_BYTES - 20)
    stderr = _CappedTextWriter(MAX_STREAM_BYTES - 20)
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            value = _resolve_tool_value(tool, arguments)
    except ExecutorDeadlineError:
        raise
    except BaseException as exc:
        raise ExecutorToolError(
            f"{type(exc).__name__}: {exc}",
            stdout=stdout.value(),
            stderr=stderr.value(),
        ) from exc
    return value, stdout.value(), stderr.value()


def _invoke_tool_with_alarm(
    tool: _SandboxTool,
    arguments: dict[str, object],
    timeout_seconds: float,
) -> tuple[object, str, str]:
    stdout = _CappedTextWriter(MAX_STREAM_BYTES - 20)
    stderr = _CappedTextWriter(MAX_STREAM_BYTES - 20)

    def timeout_handler(_signum: int, _frame: object) -> None:
        raise ExecutorDeadlineError(
            "tool call exceeded its deadline",
            stdout=stdout.value(),
            stderr=stderr.value(),
        )

    previous_handler = signal.signal(_SIGALRM, timeout_handler)
    _SETITIMER(_ITIMER_REAL, timeout_seconds)
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                value = _resolve_tool_value(tool, arguments)
            except ExecutorDeadlineError:
                raise
            except BaseException as exc:
                raise ExecutorToolError(
                    f"{type(exc).__name__}: {exc}",
                    stdout=stdout.value(),
                    stderr=stderr.value(),
                ) from exc
    finally:
        _SETITIMER(_ITIMER_REAL, 0)
        signal.signal(_SIGALRM, previous_handler)
    return value, stdout.value(), stderr.value()


def _invoke_tool_with_thread(
    tool: _SandboxTool,
    arguments: dict[str, object],
    timeout_seconds: float,
) -> tuple[object, str, str]:
    output: queue.Queue[tuple[bool, object, str, str]] = queue.Queue(maxsize=1)
    stdout = _CappedTextWriter(MAX_STREAM_BYTES - 20)
    stderr = _CappedTextWriter(MAX_STREAM_BYTES - 20)

    def target() -> None:
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                value = _resolve_tool_value(tool, arguments)
            output.put((True, value, stdout.value(), stderr.value()))
        except BaseException as exc:
            output.put((False, exc, stdout.value(), stderr.value()))

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    try:
        succeeded, value, stdout_text, stderr_text = output.get(timeout=timeout_seconds)
    except queue.Empty:
        raise ExecutorDeadlineError(
            "tool call exceeded its deadline",
            stdout=stdout.value(),
            stderr=stderr.value(),
        ) from None
    if not succeeded:
        if isinstance(value, ExecutorDeadlineError):
            raise value
        if isinstance(value, ExecutorProtocolError):
            raise value
        if isinstance(value, BaseException):
            raise ExecutorToolError(
                f"{type(value).__name__}: {value}",
                stdout=stdout_text,
                stderr=stderr_text,
            ) from value
        raise ExecutorToolError("tool call failed", stdout=stdout_text, stderr=stderr_text)
    return value, stdout_text, stderr_text


async def _await_tool_result(value: Any) -> object:
    return await value


def _base_result(call_id: str, tool_name: str, queue_wait_ms: float) -> dict[str, object]:
    return {
        "call_id": call_id,
        "error": None,
        "exit_code": None,
        "protocol_version": PROTOCOL_VERSION,
        "status": "success",
        "stderr": "",
        "stdout": "",
        "timings": {
            "execution_ms": 0.0,
            "queue_wait_ms": queue_wait_ms,
            "serialization_ms": 0.0,
        },
        "tool_name": tool_name,
        "value": None,
    }


def _error_result(
    call_id: str,
    tool_name: str,
    code: str,
    message: str,
    queue_wait_ms: float,
) -> dict[str, object]:
    result = _base_result(
        call_id, tool_name if _TOOL_NAME.fullmatch(tool_name) else "invalid_request", queue_wait_ms
    )
    result["status"] = "error"
    result["error"] = {
        "code": code,
        "message": message[:2048],
        "retryable": False,
    }
    return result


def _cap_utf8_text(value: str, maximum_bytes: int = MAX_STREAM_BYTES) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= maximum_bytes:
        return value
    marker = b"\n[output truncated]"
    return (encoded[: maximum_bytes - len(marker)] + marker).decode("utf-8", errors="ignore")


def _execute_request(
    request: dict[str, object],
    tools: dict[str, _SandboxTool],
    queue_wait_ms: float,
) -> dict[str, object]:
    call_id = str(request["call_id"])
    tool_name = str(request["tool_name"])
    deadline_value = request["deadline_unix_seconds"]
    if not isinstance(deadline_value, int | float) or isinstance(deadline_value, bool):
        raise ExecutorProtocolError("request deadline is invalid")
    deadline = float(deadline_value)
    if deadline <= time.time():
        return _error_result(
            call_id, tool_name, "deadline_exceeded", "Tool deadline elapsed.", queue_wait_ms
        )
    tool = tools.get(tool_name)
    if tool is None:
        return _error_result(
            call_id, tool_name, "unknown_tool", "Requested tool is not available.", queue_wait_ms
        )
    started = time.monotonic()
    try:
        invocation_arguments = _copy_request_arguments(request)
        remaining_seconds = min(MAX_CALL_SECONDS, max(0.001, deadline - time.time()))
        if tool.name == "run_shell":
            authored_timeout = invocation_arguments.get("timeout_seconds", 10.0)
            if isinstance(authored_timeout, int | float) and not isinstance(authored_timeout, bool):
                invocation_arguments["timeout_seconds"] = min(
                    float(authored_timeout),
                    remaining_seconds,
                )
        value, stdout, stderr = _invoke_tool(
            tool,
            invocation_arguments,
            remaining_seconds,
        )
        safe_value = _json_safe_result(value)
        if tool.name == "run_shell" and isinstance(safe_value, dict):
            stdout = str(safe_value.pop("stdout", ""))
            stderr = str(safe_value.pop("stderr", ""))
            exit_code = safe_value.pop("exit_code", None)
        else:
            exit_code = None
        _assert_value_size(safe_value)
    except ExecutorDeadlineError as exc:
        result = _error_result(call_id, tool_name, "tool_timeout", str(exc), queue_wait_ms)
        result["stdout"] = _cap_utf8_text(exc.stdout)
        result["stderr"] = _cap_utf8_text(exc.stderr)
        result["exit_code"] = exc.exit_code
    except ExecutorProtocolError as exc:
        result = _error_result(call_id, tool_name, "invalid_tool_result", str(exc), queue_wait_ms)
    except ExecutorToolError as exc:
        result = _error_result(call_id, tool_name, "tool_error", str(exc), queue_wait_ms)
        result["stdout"] = _cap_utf8_text(exc.stdout)
        result["stderr"] = _cap_utf8_text(exc.stderr)
    except Exception as exc:
        result = _error_result(
            call_id,
            tool_name,
            "tool_error",
            f"{type(exc).__name__}: {exc}",
            queue_wait_ms,
        )
    else:
        result = _base_result(call_id, tool_name, queue_wait_ms)
        result["value"] = safe_value
        result["stdout"] = _cap_utf8_text(stdout)
        result["stderr"] = _cap_utf8_text(stderr)
        result["exit_code"] = exit_code
    result["timings"]["execution_ms"] = min(  # type: ignore[index]
        86_400_000.0,
        (time.monotonic() - started) * 1000.0,
    )
    return result


def _serialize_result(result: dict[str, object]) -> bytes:
    started = time.monotonic()
    payload = _canonical_json_bytes(result)
    result["timings"]["serialization_ms"] = (time.monotonic() - started) * 1000.0  # type: ignore[index]
    payload = _canonical_json_bytes(result)
    if len(payload) > MAX_RESULT_BYTES:
        raise ExecutorProtocolError("result envelope exceeds the byte limit")
    return payload


def _manifest_payload(tools: list[_SandboxTool]) -> bytes:
    payload = _canonical_json_bytes(
        {
            "protocol_version": PROTOCOL_VERSION,
            "tools": [
                {
                    "description": tool.description,
                    "name": tool.name,
                    "parameters": tool.parameters,
                    "provenance": tool.provenance,
                }
                for tool in tools
            ],
        }
    )
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ExecutorProtocolError("tool manifest exceeds the byte limit")
    return payload


def _verify_application_archive_digest(app_zip: Path, expected_digest: str) -> None:
    if not _APP_DIGEST.fullmatch(expected_digest):
        raise PackageDigestMismatchError("application package digest is invalid")
    digest = hashlib.sha256()
    try:
        with app_zip.open("rb") as archive:
            for chunk in iter(lambda: archive.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise PackageDigestMismatchError(
            "application package could not be hashed"
        ) from None
    observed = f"sha256:{digest.hexdigest()}"
    if not hmac.compare_digest(observed, expected_digest):
        raise PackageDigestMismatchError("application package digest does not match")


def run_executor(
    app_zip: Path,
    app_digest: str,
    extraction_root: Path,
    journal_root: Path,
    workspace_root: Path,
    *,
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
) -> None:
    """Extract, discover, publish readiness, and service the file journal."""
    if (
        not math.isfinite(poll_interval_seconds)
        or poll_interval_seconds < 0.001
        or poll_interval_seconds > 5.0
    ):
        raise ExecutorProtocolError("poll interval is outside the supported range")
    journal_root.mkdir(parents=True, exist_ok=True)
    verify_started = time.perf_counter()
    _verify_application_archive_digest(app_zip, app_digest)
    _atomic_write(
        journal_root / PACKAGE_VERIFICATION_FILENAME,
        _canonical_json_bytes(
            {
                "duration_ms": (time.perf_counter() - verify_started) * 1000.0,
                "protocol_version": PROTOCOL_VERSION,
                "verified": True,
            }
        ),
    )
    securely_extract_application_archive(app_zip, extraction_root)
    workspace_root.mkdir(parents=True, exist_ok=True)
    workspace_root = workspace_root.resolve(strict=True)
    requests = journal_root / REQUEST_DIRECTORY
    results = journal_root / RESULT_DIRECTORY
    requests.mkdir(parents=True, exist_ok=True)
    results.mkdir(parents=True, exist_ok=True)
    tools = discover_sandbox_tools(extraction_root, workspace_root)
    by_name = {tool.name: tool for tool in tools}
    _atomic_write(journal_root / MANIFEST_FILENAME, _manifest_payload(tools))
    pid = os.getpid()
    _atomic_write(
        journal_root / PID_FILENAME,
        _canonical_json_bytes({"pid": pid, "protocol_version": PROTOCOL_VERSION}),
    )
    _atomic_write(
        journal_root / READINESS_FILENAME,
        _canonical_json_bytes({"pid": pid, "protocol_version": PROTOCOL_VERSION, "ready": True}),
    )
    while not (journal_root / SHUTDOWN_FILENAME).exists():
        processed = False
        for request_path in sorted(requests.glob("*.json")):
            processed = True
            arrived = request_path.stat().st_mtime
            result_path = results / request_path.name
            if result_path.exists():
                request_path.unlink(missing_ok=True)
                continue
            call_id = request_path.stem
            tool_name = "invalid_request"
            queue_wait_ms = min(86_400_000.0, max(0.0, (time.time() - arrived) * 1000.0))
            try:
                request = _parse_request(_read_bounded_file(request_path, MAX_REQUEST_BYTES))
                _assert_matching_call_id(request, call_id)
                tool_name = str(request["tool_name"])
                result = _execute_request(request, by_name, queue_wait_ms)
            except ExecutorProtocolError as exc:
                safe_call_id = call_id if _CALL_ID.fullmatch(call_id) else "invalid-request"
                result = _error_result(
                    safe_call_id,
                    tool_name,
                    "invalid_request",
                    str(exc),
                    queue_wait_ms,
                )
            try:
                payload = _serialize_result(result)
            except ExecutorProtocolError:
                payload = _serialize_result(
                    _error_result(
                        str(result["call_id"]),
                        str(result["tool_name"]),
                        "result_too_large",
                        "Result envelope exceeds the byte limit.",
                        queue_wait_ms,
                    )
                )
            _atomic_write(result_path, payload, preserve_existing=True)
            request_path.unlink(missing_ok=True)
        if not processed:
            time.sleep(poll_interval_seconds)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-zip", required=True, type=Path)
    parser.add_argument("--app-digest", required=True)
    parser.add_argument("--extraction-root", required=True, type=Path)
    parser.add_argument("--journal-root", required=True, type=Path)
    parser.add_argument("--workspace-root", required=True, type=Path)
    parser.add_argument("--poll-interval", default=POLL_INTERVAL_SECONDS, type=float)
    return parser


def main(arguments: list[str] | None = None) -> int:
    """Run the standalone sandbox executor."""
    options = _argument_parser().parse_args(arguments)
    try:
        run_executor(
            options.app_zip,
            options.app_digest,
            options.extraction_root,
            options.journal_root,
            options.workspace_root,
            poll_interval_seconds=options.poll_interval,
        )
    except Exception as exc:
        with contextlib.suppress(Exception):
            options.journal_root.mkdir(parents=True, exist_ok=True)
            _atomic_write(
                options.journal_root / STARTUP_FAILURE_FILENAME,
                _canonical_json_bytes(
                    {
                        "exception_type": type(exc).__name__,
                        "phase": (
                            "package_verify"
                            if isinstance(exc, PackageDigestMismatchError)
                            else "startup"
                        ),
                        "protocol_version": PROTOCOL_VERSION,
                        "startup_failed": True,
                    }
                ),
            )
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
