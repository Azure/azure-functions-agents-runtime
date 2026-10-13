"""Prepare JavaScript tool declarations without executing customer modules."""

from __future__ import annotations

import base64
import hashlib
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .._local_tool_descriptor import LocalToolLaunch, LocalToolSpec, PreparedLocalTool

_NODE_VERSION_PATTERN: Final = re.compile(r"^v(?P<major>\d+)\.")
_MINIMUM_NODE_MAJOR: Final = 22
_CHECK_TIMEOUT_SECONDS: Final = 10
_IDENTIFIER_PATTERN: Final = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")


@dataclass(frozen=True)
class LocalToolPreparationResult:
    tools: tuple[PreparedLocalTool, ...]
    failed_loads: tuple[tuple[str, str], ...]


def validate_javascript_spec(spec: LocalToolSpec) -> None:
    if not _IDENTIFIER_PATTERN.fullmatch(spec.entrypoint):
        raise ValueError("field `entrypoint`: must be a JavaScript identifier")


def _adapter_path() -> Path:
    path = Path(__file__).with_name("node_mcp_adapter.mjs")
    if not path.is_file():
        raise RuntimeError(f"Packaged JavaScript MCP adapter was not found: {path}")
    return path


def _resolve_node() -> Path:
    command = shutil.which("node")
    if command is None:
        raise RuntimeError("Node.js 22 or newer was not found on PATH")
    node_path = Path(command).resolve()
    if not node_path.is_file():
        raise RuntimeError(f"Node.js executable is not a regular file: {node_path}")
    result = subprocess.run(
        [str(node_path), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=_CHECK_TIMEOUT_SECONDS,
    )
    version = result.stdout.strip()
    match = _NODE_VERSION_PATTERN.match(version)
    if result.returncode != 0 or match is None:
        detail = result.stderr.strip() or version or f"exit code {result.returncode}"
        raise RuntimeError(f"Could not determine Node.js version: {detail}")
    if int(match.group("major")) < _MINIMUM_NODE_MAJOR:
        raise RuntimeError(
            f"Node.js {_MINIMUM_NODE_MAJOR} or newer is required; found {version}"
        )
    return node_path


def _prepare_one(spec: LocalToolSpec, node_path: Path) -> PreparedLocalTool:
    source = spec.source_path.read_bytes()
    source_text = source.decode("utf-8")
    result = subprocess.run(
        [str(node_path), "--check", "--input-type=module"],
        input=source_text,
        check=False,
        capture_output=True,
        text=True,
        timeout=_CHECK_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "syntax check failed"
        raise RuntimeError(f"JavaScript syntax error: {detail}")
    return PreparedLocalTool(
        spec=spec,
        source_sha256=hashlib.sha256(source).hexdigest(),
        launch=LocalToolLaunch.create(
            command=node_path,
            arguments=(
                str(_adapter_path()),
                base64.urlsafe_b64encode(spec.contract_json().encode("utf-8"))
                .decode("ascii")
                .rstrip("="),
                str(spec.source_path),
                hashlib.sha256(source).hexdigest(),
            ),
        ),
    )


def prepare_javascript_tools(specs: tuple[LocalToolSpec, ...]) -> LocalToolPreparationResult:
    """Resolve Node once and syntax-check every discovered JavaScript tool."""
    if not specs:
        return LocalToolPreparationResult((), ())
    try:
        node_path = _resolve_node()
    except Exception as exc:
        node_failures = tuple(
            (str(spec.source_path), f"{type(exc).__name__}: {exc}") for spec in specs
        )
        return LocalToolPreparationResult((), node_failures)

    prepared: list[PreparedLocalTool] = []
    failures: list[tuple[str, str]] = []
    for spec in specs:
        try:
            prepared.append(_prepare_one(spec, node_path))
        except Exception as exc:
            failures.append((str(spec.source_path), f"{type(exc).__name__}: {exc}"))
    return LocalToolPreparationResult(tuple(prepared), tuple(failures))
