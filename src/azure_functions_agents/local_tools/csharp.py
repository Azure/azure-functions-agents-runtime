"""Prepare C# local tools as isolated MCP stdio processes."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from .._local_tool_descriptor import LocalToolLaunch, LocalToolSpec, PreparedLocalTool

_ENTRYPOINT_PATTERN: Final = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$"
)
_SDK_PATTERN: Final = re.compile(r"^(?P<version>10\.\d+\.\d+)")
_CHECK_TIMEOUT_SECONDS: Final = 10
_BUILD_TIMEOUT_SECONDS: Final = 120
_ADAPTER_VERSION: Final = "1"


@dataclass(frozen=True)
class LocalToolPreparationResult:
    tools: tuple[PreparedLocalTool, ...]
    failed_loads: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _DotNetToolchain:
    executable: Path
    sdk_version: str


def validate_csharp_spec(spec: LocalToolSpec) -> None:
    if not _ENTRYPOINT_PATTERN.fullmatch(spec.entrypoint):
        raise ValueError("field `entrypoint`: C# tools require `TypeName.MethodName`")


def _resolve_dotnet() -> _DotNetToolchain:
    command = shutil.which("dotnet")
    if command is None:
        raise RuntimeError(".NET 10 SDK was not found on PATH")
    executable = Path(command).resolve()
    if not executable.is_file():
        raise RuntimeError(f"dotnet executable is not a regular file: {executable}")
    result = subprocess.run(
        [str(executable), "--list-sdks"],
        check=False,
        capture_output=True,
        text=True,
        timeout=_CHECK_TIMEOUT_SECONDS,
    )
    versions = [
        match.group("version")
        for line in result.stdout.splitlines()
        if (match := _SDK_PATTERN.match(line.strip())) is not None
    ]
    if result.returncode != 0 or not versions:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit code {result.returncode}"
        raise RuntimeError(f".NET 10 SDK is required: {detail}")
    return _DotNetToolchain(executable=executable, sdk_version=sorted(versions)[-1])


def _adapter_source() -> Path:
    path = Path(__file__).with_name("csharp_adapter") / "Program.cs"
    if not path.is_file():
        raise RuntimeError(f"Packaged C# MCP adapter source was not found: {path}")
    return path


def _cache_root() -> Path:
    root = (
        Path(tempfile.gettempdir())
        / "azure-functions-agents-runtime"
        / "local-tools"
        / "csharp"
    )
    root.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        root.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return root


def _ensure_safe_path(path: Path) -> None:
    current = path
    while current != current.parent:
        if current.exists() and current.is_symlink():
            raise RuntimeError(f"C# artifact path must not contain links: {current}")
        current = current.parent


def _run_build(command: list[str], *, cwd: Path, environment: dict[str, str]) -> None:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name != "nt",
        creationflags=(
            subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        ),
    )
    try:
        stdout, stderr = process.communicate(timeout=_BUILD_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        raise RuntimeError(
            f"C# compilation exceeded {_BUILD_TIMEOUT_SECONDS} seconds"
        ) from exc
    if process.returncode != 0:
        detail = (stderr.strip() or stdout.strip())[-8000:]
        raise RuntimeError(f"C# compilation failed: {detail}")


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        with suppress(OSError):
            process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        kill_process_group = cast(Any, os).killpg
        with suppress(ProcessLookupError):
            kill_process_group(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "nt":
        process.kill()
    else:
        kill_process_group = cast(Any, os).killpg
        with suppress(ProcessLookupError):
            kill_process_group(process.pid, cast(Any, signal).SIGKILL)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=2)


def _project_xml(*, output_type: str, source_name: str) -> str:
    return f"""<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net10.0</TargetFramework>
    <OutputType>{output_type}</OutputType>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
    <ImportDirectoryBuildProps>false</ImportDirectoryBuildProps>
    <ImportDirectoryBuildTargets>false</ImportDirectoryBuildTargets>
    <RestoreSources></RestoreSources>
    <NuGetAudit>false</NuGetAudit>
    <DebugType>none</DebugType>
    <DebugSymbols>false</DebugSymbols>
    <Deterministic>true</Deterministic>
  </PropertyGroup>
  <ItemGroup>
    <Compile Include="{source_name}" />
  </ItemGroup>
</Project>
"""


def _build_artifact(
    *,
    toolchain: _DotNetToolchain,
    spec: LocalToolSpec,
    source: bytes,
    artifact: Path,
    manifest: dict[str, str],
) -> None:
    staging = artifact.with_name(f".{artifact.name}.{uuid.uuid4().hex}.tmp")
    staging.mkdir(parents=True)
    try:
        (staging / "global.json").write_text(
            json.dumps(
                {
                    "sdk": {
                        "version": toolchain.sdk_version,
                        "rollForward": "disable",
                        "allowPrerelease": False,
                    }
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        (staging / "NuGet.Config").write_text(
            '<?xml version="1.0" encoding="utf-8"?><configuration>'
            "<packageSources><clear /></packageSources></configuration>",
            encoding="utf-8",
        )
        customer_dir = staging / "customer"
        adapter_dir = staging / "adapter"
        customer_dir.mkdir()
        adapter_dir.mkdir()
        (customer_dir / spec.source_path.name).write_bytes(source)
        (customer_dir / "CustomerTool.csproj").write_text(
            _project_xml(output_type="Library", source_name=spec.source_path.name),
            encoding="utf-8",
        )
        shutil.copyfile(_adapter_source(), adapter_dir / "Program.cs")
        (adapter_dir / "LocalToolAdapter.csproj").write_text(
            _project_xml(output_type="Exe", source_name="Program.cs"),
            encoding="utf-8",
        )

        environment = dict(os.environ)
        environment.update(
            {
                "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
                "DOTNET_NOLOGO": "1",
                "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
                "MSBUILDDISABLENODEREUSE": "1",
                "DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE": "1",
            }
        )
        for project in (
            customer_dir / "CustomerTool.csproj",
            adapter_dir / "LocalToolAdapter.csproj",
        ):
            _run_build(
                [
                    str(toolchain.executable),
                    "restore",
                    str(project),
                    "--configfile",
                    str(staging / "NuGet.Config"),
                    "--ignore-failed-sources",
                    "-p:UseSharedCompilation=false",
                ],
                cwd=staging,
                environment=environment,
            )
            _run_build(
                [
                    str(toolchain.executable),
                    "build",
                    str(project),
                    "--no-restore",
                    "--configuration",
                    "Release",
                    "-p:UseSharedCompilation=false",
                    "-p:BuildProjectReferences=false",
                ],
                cwd=staging,
                environment=environment,
            )

        output = staging / "output"
        output.mkdir()
        customer_dll = customer_dir / "bin" / "Release" / "net10.0" / "CustomerTool.dll"
        adapter_build = adapter_dir / "bin" / "Release" / "net10.0"
        shutil.copyfile(customer_dll, output / "CustomerTool.dll")
        for path in adapter_build.iterdir():
            if path.is_file() and path.suffix.lower() not in {".pdb"}:
                shutil.copyfile(path, output / path.name)
        (output / "manifest.json").write_text(
            json.dumps(manifest, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        try:
            output.replace(artifact)
        except OSError:
            if not (artifact / "manifest.json").is_file():
                raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _prepare_one(
    spec: LocalToolSpec,
    toolchain: _DotNetToolchain,
    cache_root: Path,
) -> PreparedLocalTool:
    source = spec.source_path.read_bytes()
    source.decode("utf-8")
    source_digest = hashlib.sha256(source).hexdigest()
    adapter_digest = hashlib.sha256(_adapter_source().read_bytes()).hexdigest()
    manifest = {
        "source_sha256": source_digest,
        "contract": spec.contract_json(),
        "sdk": toolchain.sdk_version,
        "adapter_version": _ADAPTER_VERSION,
        "adapter_sha256": adapter_digest,
    }
    cache_key = hashlib.sha256(
        b"\0".join(
            (
                source,
                spec.contract_json().encode("utf-8"),
                _ADAPTER_VERSION.encode("ascii"),
                adapter_digest.encode("ascii"),
                toolchain.sdk_version.encode("ascii"),
                platform.system().encode("utf-8"),
                platform.machine().encode("utf-8"),
            )
        )
    ).hexdigest()
    artifact = cache_root / cache_key
    _ensure_safe_path(artifact)
    manifest_path = artifact / "manifest.json"
    artifact_matches = False
    if manifest_path.is_file():
        try:
            artifact_matches = json.loads(manifest_path.read_text(encoding="utf-8")) == manifest
        except (OSError, json.JSONDecodeError):
            artifact_matches = False
    if not artifact_matches:
        if artifact.exists():
            shutil.rmtree(artifact)
        _build_artifact(
            toolchain=toolchain,
            spec=spec,
            source=source,
            artifact=artifact,
            manifest=manifest,
        )
    adapter_dll = artifact / "LocalToolAdapter.dll"
    customer_dll = artifact / "CustomerTool.dll"
    if not adapter_dll.is_file() or not customer_dll.is_file():
        raise RuntimeError(f"C# artifact is incomplete: {artifact}")
    encoded_contract = base64.urlsafe_b64encode(
        spec.contract_json().encode("utf-8")
    ).decode("ascii").rstrip("=")
    return PreparedLocalTool(
        spec=spec,
        source_sha256=source_digest,
        launch=LocalToolLaunch.create(
            command=toolchain.executable,
            arguments=(str(adapter_dll), encoded_contract, str(customer_dll)),
        ),
    )


def prepare_csharp_tools(specs: tuple[LocalToolSpec, ...]) -> LocalToolPreparationResult:
    if not specs:
        return LocalToolPreparationResult((), ())
    try:
        toolchain = _resolve_dotnet()
        cache_root = _cache_root()
    except Exception as exc:
        toolchain_failures = tuple(
            (str(spec.source_path), f"{type(exc).__name__}: {exc}") for spec in specs
        )
        return LocalToolPreparationResult((), toolchain_failures)

    prepared: list[PreparedLocalTool] = []
    failures: list[tuple[str, str]] = []
    for spec in specs:
        try:
            prepared.append(_prepare_one(spec, toolchain, cache_root))
        except Exception as exc:
            failures.append((str(spec.source_path), f"{type(exc).__name__}: {exc}"))
    return LocalToolPreparationResult(tuple(prepared), tuple(failures))
