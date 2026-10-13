"""Prepare Go local tools as isolated MCP stdio processes."""

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
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

from .._local_tool_descriptor import (
    LocalToolLaunch,
    LocalToolSpec,
    PortableToolType,
    PreparedLocalTool,
)

_ENTRYPOINT_PATTERN: Final = re.compile(r"^[A-Z][A-Za-z0-9_]*$")
_GO_VERSION_PATTERN: Final = re.compile(r"\bgo1\.(?P<minor>\d+)(?:\.\d+)?\b")
_PACKAGE_MAIN_PATTERN: Final = re.compile(r"(?m)^\s*package\s+main\s*(?://.*)?$")
_PROHIBITED_DIRECTIVE_PATTERN: Final = re.compile(
    r"(?m)^\s*//\s*(?:go:build|\+build|go:generate)\b"
)
_MINIMUM_GO_MINOR: Final = 25
_CHECK_TIMEOUT_SECONDS: Final = 30
_BUILD_TIMEOUT_SECONDS: Final = 120
_ADAPTER_VERSION: Final = "1"
_BINDING_VERSION: Final = "1"
_CACHE_LOCK_TIMEOUT_SECONDS: Final = 30
_CACHE_LOCK_STALE_SECONDS: Final = 300


@dataclass(frozen=True)
class LocalToolPreparationResult:
    tools: tuple[PreparedLocalTool, ...]
    failed_loads: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _GoToolchain:
    executable: Path
    version: str
    goos: str
    goarch: str
    standard_library: frozenset[str]


@dataclass(frozen=True)
class _Inspection:
    return_kind: str
    imports: tuple[str, ...]


def validate_go_spec(spec: LocalToolSpec) -> None:
    if not _ENTRYPOINT_PATTERN.fullmatch(spec.entrypoint):
        raise ValueError(
            "field `entrypoint`: Go tools require an explicit exported identifier"
        )
    source = spec.source_path.read_text(encoding="utf-8")
    if _PACKAGE_MAIN_PATTERN.search(source) is None:
        raise ValueError("Go source must declare `package main`")
    if _PROHIBITED_DIRECTIVE_PATTERN.search(source) is not None:
        raise ValueError("Go build and generation directives are not supported")


def _source_dir(name: str) -> Path:
    path = Path(__file__).with_name(name)
    if not path.is_dir():
        raise RuntimeError(f"Packaged Go runtime source was not found: {path}")
    return path


def _go_environment(cache_root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for name in ("GOROOT", "GOFLAGS", "GOOS", "GOARCH"):
        environment.pop(name, None)
    environment.update(
        {
            "CGO_ENABLED": "0",
            "GOENV": "off",
            "GOPATH": str(cache_root / "gopath"),
            "GOCACHE": str(cache_root / "build-cache"),
            "GOMODCACHE": str(cache_root / "module-cache"),
            "GOPROXY": "off",
            "GOSUMDB": "off",
            "GOTOOLCHAIN": "local",
            "GOWORK": "off",
        }
    )
    return environment


def _run_checked(
    command: list[str],
    *,
    environment: dict[str, str],
    cwd: Path | None = None,
    timeout: int = _CHECK_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip())[-8000:]
        raise RuntimeError(detail or f"command exited with code {result.returncode}")
    return result


def _resolve_go(cache_root: Path) -> _GoToolchain:
    command = shutil.which("go")
    if command is None:
        raise RuntimeError("Go 1.25 or newer was not found on PATH")
    executable = Path(command).resolve()
    if not executable.is_file():
        raise RuntimeError(f"go executable is not a regular file: {executable}")
    environment = _go_environment(cache_root)
    version_result = _run_checked(
        [str(executable), "version"],
        environment=environment,
    )
    version = version_result.stdout.strip()
    match = _GO_VERSION_PATTERN.search(version)
    if match is None:
        raise RuntimeError(f"Could not determine Go version: {version}")
    if int(match.group("minor")) < _MINIMUM_GO_MINOR:
        raise RuntimeError(
            f"Go 1.{_MINIMUM_GO_MINOR} or newer is required; found {version}"
        )
    env_result = _run_checked(
        [str(executable), "env", "GOVERSION", "GOOS", "GOARCH"],
        environment=environment,
    )
    values = env_result.stdout.splitlines()
    if len(values) != 3:
        raise RuntimeError("Could not determine Go toolchain environment")
    std_result = _run_checked(
        [str(executable), "list", "std"],
        environment=environment,
        timeout=_BUILD_TIMEOUT_SECONDS,
    )
    return _GoToolchain(
        executable=executable,
        version=values[0].strip(),
        goos=values[1].strip(),
        goarch=values[2].strip(),
        standard_library=frozenset(std_result.stdout.splitlines()),
    )


def _cache_root() -> Path:
    root = (
        Path(tempfile.gettempdir()) / "azure-functions-agents-runtime" / "local-tools" / "go"
    )
    root.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        root.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return root


def _ensure_safe_path(path: Path) -> None:
    current = path
    while current != current.parent:
        if current.exists() and current.is_symlink():
            raise RuntimeError(f"Go artifact path must not contain links: {current}")
        current = current.parent


@contextmanager
def _cache_key_lock(cache_root: Path, cache_key: str) -> Iterator[None]:
    lock = cache_root / f".{cache_key}.lock"
    deadline = time.monotonic() + _CACHE_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            with suppress(OSError):
                age = time.time() - lock.stat().st_mtime
                if age > _CACHE_LOCK_STALE_SECONDS:
                    lock.rmdir()
                    continue
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Timed out waiting for Go artifact lock: {cache_key}"
                ) from None
            time.sleep(0.05)
    try:
        yield
    finally:
        with suppress(OSError):
            lock.rmdir()


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


def _run_build(command: list[str], *, cwd: Path, environment: dict[str, str]) -> None:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name != "nt",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    try:
        stdout, stderr = process.communicate(timeout=_BUILD_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        raise RuntimeError(
            f"Go compilation exceeded {_BUILD_TIMEOUT_SECONDS} seconds"
        ) from exc
    if process.returncode != 0:
        detail = (stderr.strip() or stdout.strip())[-8000:]
        raise RuntimeError(f"Go compilation failed: {detail}")


def _inspect_source(
    *,
    toolchain: _GoToolchain,
    environment: dict[str, str],
    source_path: Path,
    spec: LocalToolSpec,
) -> _Inspection:
    parameter_types = ",".join(parameter.type.value for parameter in spec.parameters)
    result = _run_checked(
        [
            str(toolchain.executable),
            "run",
            str(_source_dir("go_inspector") / "main.go"),
            "--",
            str(source_path),
            spec.entrypoint,
            parameter_types,
        ],
        environment=environment,
        cwd=source_path.parent,
        timeout=_BUILD_TIMEOUT_SECONDS,
    )
    try:
        payload = cast(dict[str, Any], json.loads(result.stdout))
        return_kind = cast(str, payload["returnKind"])
        imports = tuple(cast(list[str], payload["imports"]))
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Go inspector returned an invalid response") from exc
    unsupported = sorted(set(imports) - toolchain.standard_library)
    if unsupported:
        raise RuntimeError(
            "Go local tools may import only the standard library: "
            + ", ".join(unsupported)
        )
    return _Inspection(return_kind=return_kind, imports=imports)


def _decode_lines(parameters: tuple[object, ...]) -> str:
    lines: list[str] = []
    for index, parameter_value in enumerate(parameters):
        parameter = cast(Any, parameter_value)
        variable = f"value{index}"
        raw = f"request.Arguments[{json.dumps(parameter.name)}]"
        if parameter.type is PortableToolType.INTEGER:
            lines.extend(
                (
                    f"\t{variable}, err := decodeInteger({raw})",
                    "\tif err != nil { return errorResponse(request, err) }",
                )
            )
        else:
            native = {
                PortableToolType.STRING: "string",
                PortableToolType.NUMBER: "float64",
                PortableToolType.BOOLEAN: "bool",
            }[parameter.type]
            lines.extend(
                (
                    f"\tvar {variable} {native}",
                    f"\tif err := json.Unmarshal({raw}, &{variable}); err != nil {{",
                    f'\t\treturn errorResponse(request, fmt.Errorf("argument {parameter.name}: %w", err))',
                    "\t}",
                )
            )
            if parameter.type is PortableToolType.NUMBER:
                lines.extend(
                    (
                        f"\tif math.IsNaN({variable}) || math.IsInf({variable}, 0) {{",
                        f'\t\treturn errorResponse(request, fmt.Errorf("argument {parameter.name} must be finite"))',
                        "\t}",
                    )
                )
    return "\n".join(lines)


def _invocation_lines(spec: LocalToolSpec, return_kind: str) -> str:
    values = ", ".join(f"value{index}" for index in range(len(spec.parameters)))
    call = f"{spec.entrypoint}({values})"
    if return_kind == "none":
        return f"\t{call}\n\treturn emptyResponse(request)"
    if return_kind == "error":
        return (
            f"\tif err := {call}; err != nil {{ return errorResponse(request, err) }}\n"
            "\treturn emptyResponse(request)"
        )
    if return_kind == "value":
        return f"\tresult := {call}\n\treturn valueResponse(request, result)"
    if return_kind == "value_error":
        return (
            f"\tresult, err := {call}\n"
            "\tif err != nil { return errorResponse(request, err) }\n"
            "\treturn valueResponse(request, result)"
        )
    raise RuntimeError(f"Unsupported Go return classification: {return_kind}")


def _binding_source(spec: LocalToolSpec, return_kind: str) -> str:
    return f"""package main

import (
\t"bufio"
\t"bytes"
\t"encoding/binary"
\t"encoding/json"
\t"errors"
\t"fmt"
\t"io"
\t"math"
\t"math/big"
\t"net"
\t"os"
\t"runtime/debug"
\t"strconv"
)

const maxRequestFrame = 1024 * 1024
const maxResponseFrame = 4 * 1024 * 1024
const maxSafeInteger int64 = 9007199254740991

type invocationRequest struct {{
\tGeneration string                     `json:"generation"`
\tID         string                     `json:"id"`
\tArguments  map[string]json.RawMessage `json:"arguments"`
}}

type invocationResponse struct {{
\tGeneration string `json:"generation"`
\tID         string `json:"id"`
\tOK         bool   `json:"ok"`
\tKind       string `json:"kind,omitempty"`
\tText       string `json:"text,omitempty"`
\tError      string `json:"error,omitempty"`
}}

func writeFrame(writer io.Writer, value any) error {{
\tpayload, err := json.Marshal(value)
\tif err != nil {{ return err }}
\tif len(payload) > maxResponseFrame {{ return errors.New("response exceeds 4 MiB") }}
\tvar header [4]byte
\tbinary.BigEndian.PutUint32(header[:], uint32(len(payload)))
\tif _, err = writer.Write(header[:]); err != nil {{ return err }}
\t_, err = writer.Write(payload)
\treturn err
}}

func readFrame(reader *bufio.Reader) ([]byte, error) {{
\tvar header [4]byte
\tif _, err := io.ReadFull(reader, header[:]); err != nil {{ return nil, err }}
\tsize := binary.BigEndian.Uint32(header[:])
\tif size > maxRequestFrame {{ return nil, errors.New("request exceeds 1 MiB") }}
\tpayload := make([]byte, size)
\t_, err := io.ReadFull(reader, payload)
\treturn payload, err
}}

func decodeInteger(raw json.RawMessage) (int64, error) {{
\tdecoder := json.NewDecoder(bytes.NewReader(raw))
\tdecoder.UseNumber()
\tvar decoded any
\tif err := decoder.Decode(&decoded); err != nil {{ return 0, err }}
\tnumber, ok := decoded.(json.Number)
\tif !ok {{ return 0, errors.New("must be a JSON integer") }}
\tvalue, ok := new(big.Rat).SetString(number.String())
\tif !ok || !value.IsInt() {{
\t\treturn 0, errors.New("must be an integer")
\t}}
\tinteger := value.Num()
\tif integer.Cmp(big.NewInt(-maxSafeInteger)) < 0 ||
\t\tinteger.Cmp(big.NewInt(maxSafeInteger)) > 0 {{
\t\treturn 0, errors.New("must be within the JSON safe-integer range")
\t}}
\treturn integer.Int64(), nil
}}

func emptyResponse(request invocationRequest) invocationResponse {{
\treturn invocationResponse{{Generation: request.Generation, ID: request.ID, OK: true, Kind: "empty"}}
}}

func errorResponse(request invocationRequest, err error) invocationResponse {{
\tmessage := err.Error()
\tif len(message) > 4096 {{ message = message[:4096] }}
\treturn invocationResponse{{Generation: request.Generation, ID: request.ID, OK: false, Error: message}}
}}

func valueResponse(request invocationRequest, value any) invocationResponse {{
\tif value == nil {{ return emptyResponse(request) }}
\tswitch typed := value.(type) {{
\tcase string:
\t\treturn invocationResponse{{Generation: request.Generation, ID: request.ID, OK: true, Kind: "text", Text: typed}}
\tcase bool:
\t\treturn invocationResponse{{Generation: request.Generation, ID: request.ID, OK: true, Kind: "text", Text: strconv.FormatBool(typed)}}
\tcase float32:
\t\tif math.IsNaN(float64(typed)) || math.IsInf(float64(typed), 0) {{ return errorResponse(request, errors.New("result must be finite")) }}
\tcase float64:
\t\tif math.IsNaN(typed) || math.IsInf(typed, 0) {{ return errorResponse(request, errors.New("result must be finite")) }}
\t}}
\tpayload, err := json.Marshal(value)
\tif err != nil {{ return errorResponse(request, err) }}
\treturn invocationResponse{{Generation: request.Generation, ID: request.ID, OK: true, Kind: "text", Text: string(payload)}}
}}

func invoke(request invocationRequest) (response invocationResponse) {{
\tdefer func() {{
\t\tif recovered := recover(); recovered != nil {{
\t\t\tfmt.Fprintf(os.Stderr, "Go tool panic: %v\\n%s", recovered, debug.Stack())
\t\t\tresponse = errorResponse(request, fmt.Errorf("Go tool panicked: %v", recovered))
\t\t}}
\t}}()
{_decode_lines(spec.parameters)}
{_invocation_lines(spec, return_kind)}
}}

func main() {{
\taddress := os.Getenv("AZURE_FUNCTIONS_AGENTS_GO_ADDRESS")
\ttoken := os.Getenv("AZURE_FUNCTIONS_AGENTS_GO_TOKEN")
\tgeneration := os.Getenv("AZURE_FUNCTIONS_AGENTS_GO_GENERATION")
\tif address == "" || token == "" || generation == "" {{
\t\tfmt.Fprintln(os.Stderr, "Go customer process is missing runtime connection metadata.")
\t\tos.Exit(2)
\t}}
\tconnection, err := net.Dial("tcp", address)
\tif err != nil {{ fmt.Fprintln(os.Stderr, err); os.Exit(2) }}
\tdefer connection.Close()
\treader := bufio.NewReader(connection)
\tif err := writeFrame(connection, map[string]string{{"token": token, "generation": generation}}); err != nil {{ os.Exit(2) }}
\tfor {{
\t\tpayload, err := readFrame(reader)
\t\tif err != nil {{ if err != io.EOF {{ fmt.Fprintln(os.Stderr, err) }}; return }}
\t\tvar request invocationRequest
\t\tif err := json.Unmarshal(payload, &request); err != nil {{ return }}
\t\tif request.Generation != generation {{ return }}
\t\tif err := writeFrame(connection, invoke(request)); err != nil {{ return }}
\t}}
}}
"""


def _copy_go_sources(source_dir: Path, destination: Path) -> None:
    for source in source_dir.glob("*.go"):
        shutil.copyfile(source, destination / source.name)


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact_matches(artifact: Path, manifest: dict[str, Any]) -> bool:
    manifest_path = artifact / "manifest.json"
    adapter = artifact / manifest["adapter_name"]
    customer = artifact / manifest["customer_name"]
    if not manifest_path.is_file() or not adapter.is_file() or not customer.is_file():
        return False
    try:
        actual = cast(dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        actual == manifest
        and _file_digest(adapter) == manifest["adapter_sha256"]
        and _file_digest(customer) == manifest["customer_sha256"]
    )


def _build_artifact(
    *,
    toolchain: _GoToolchain,
    environment: dict[str, str],
    spec: LocalToolSpec,
    source: bytes,
    inspection: _Inspection,
    artifact: Path,
    manifest_base: dict[str, Any],
) -> dict[str, Any]:
    staging = artifact.with_name(f".{artifact.name}.{uuid.uuid4().hex}.tmp")
    staging.mkdir(parents=True)
    try:
        customer_dir = staging / "customer"
        adapter_dir = staging / "adapter"
        output_dir = staging / "output"
        customer_dir.mkdir()
        adapter_dir.mkdir()
        output_dir.mkdir()
        (customer_dir / "go.mod").write_text(
            "module azurefunctions.localtool/customer\n\ngo 1.25\n",
            encoding="utf-8",
        )
        (customer_dir / "customer_tool.go").write_bytes(source)
        (customer_dir / "runtime_main.go").write_text(
            _binding_source(spec, inspection.return_kind),
            encoding="utf-8",
        )
        (adapter_dir / "go.mod").write_text(
            "module azurefunctions.localtool/adapter\n\ngo 1.25\n",
            encoding="utf-8",
        )
        _copy_go_sources(_source_dir("go_adapter"), adapter_dir)
        executable_suffix = ".exe" if toolchain.goos == "windows" else ""
        customer_name = f"customer{executable_suffix}"
        adapter_name = f"adapter{executable_suffix}"
        _run_build(
            [
                str(toolchain.executable),
                "build",
                "-mod=readonly",
                "-buildvcs=false",
                "-trimpath",
                "-o",
                str(output_dir / customer_name),
                ".",
            ],
            cwd=customer_dir,
            environment=environment,
        )
        _run_build(
            [
                str(toolchain.executable),
                "build",
                "-mod=readonly",
                "-buildvcs=false",
                "-trimpath",
                "-o",
                str(output_dir / adapter_name),
                ".",
            ],
            cwd=adapter_dir,
            environment=environment,
        )
        manifest = {
            **manifest_base,
            "adapter_name": adapter_name,
            "customer_name": customer_name,
            "adapter_sha256": _file_digest(output_dir / adapter_name),
            "customer_sha256": _file_digest(output_dir / customer_name),
        }
        (output_dir / "manifest.json").write_text(
            json.dumps(manifest, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        try:
            output_dir.replace(artifact)
        except OSError:
            if not _artifact_matches(artifact, manifest):
                raise
        return manifest
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _prepare_one(
    spec: LocalToolSpec,
    toolchain: _GoToolchain,
    cache_root: Path,
    environment: dict[str, str],
) -> PreparedLocalTool:
    source = spec.source_path.read_bytes()
    source.decode("utf-8")
    source_digest = hashlib.sha256(source).hexdigest()
    inspection_dir = cache_root / f".inspect-{uuid.uuid4().hex}"
    inspection_dir.mkdir()
    try:
        inspection_source = inspection_dir / spec.source_path.name
        inspection_source.write_bytes(source)
        inspection = _inspect_source(
            toolchain=toolchain,
            environment=environment,
            source_path=inspection_source,
            spec=spec,
        )
    finally:
        shutil.rmtree(inspection_dir, ignore_errors=True)

    adapter_sources = sorted(_source_dir("go_adapter").glob("*.go"))
    adapter_digest = hashlib.sha256(
        b"\0".join(path.read_bytes() for path in adapter_sources)
    ).hexdigest()
    binding = _binding_source(spec, inspection.return_kind).encode("utf-8")
    manifest_base: dict[str, Any] = {
        "adapter_version": _ADAPTER_VERSION,
        "adapter_source_sha256": adapter_digest,
        "binding_version": _BINDING_VERSION,
        "binding_sha256": hashlib.sha256(binding).hexdigest(),
        "contract": spec.contract_json(),
        "goarch": toolchain.goarch,
        "goos": toolchain.goos,
        "return_kind": inspection.return_kind,
        "source_sha256": source_digest,
        "toolchain": toolchain.version,
    }
    cache_key = hashlib.sha256(
        b"\0".join(
            (
                source,
                spec.contract_json().encode("utf-8"),
                binding,
                adapter_digest.encode("ascii"),
                toolchain.version.encode("ascii"),
                toolchain.goos.encode("ascii"),
                toolchain.goarch.encode("ascii"),
                platform.machine().encode("utf-8"),
            )
        )
    ).hexdigest()
    artifact = cache_root / cache_key
    _ensure_safe_path(artifact)
    executable_suffix = ".exe" if toolchain.goos == "windows" else ""
    expected_manifest = {
        **manifest_base,
        "adapter_name": f"adapter{executable_suffix}",
        "customer_name": f"customer{executable_suffix}",
    }
    with _cache_key_lock(cache_root, cache_key):
        manifest: dict[str, Any] | None = None
        manifest_path = artifact / "manifest.json"
        if manifest_path.is_file():
            try:
                candidate = cast(
                    dict[str, Any],
                    json.loads(manifest_path.read_text(encoding="utf-8")),
                )
                if (
                    all(
                        candidate.get(key) == value
                        for key, value in expected_manifest.items()
                    )
                    and _artifact_matches(artifact, candidate)
                ):
                    manifest = candidate
            except (OSError, json.JSONDecodeError):
                pass
        if manifest is None:
            if artifact.exists():
                quarantine = artifact.with_name(
                    f".{artifact.name}.{uuid.uuid4().hex}.invalid"
                )
                artifact.replace(quarantine)
                shutil.rmtree(quarantine, ignore_errors=True)
            manifest = _build_artifact(
                toolchain=toolchain,
                environment=environment,
                spec=spec,
                source=source,
                inspection=inspection,
                artifact=artifact,
                manifest_base=manifest_base,
            )
    adapter = artifact / cast(str, manifest["adapter_name"])
    customer = artifact / cast(str, manifest["customer_name"])
    encoded_contract = base64.urlsafe_b64encode(
        spec.contract_json().encode("utf-8")
    ).decode("ascii").rstrip("=")
    return PreparedLocalTool(
        spec=spec,
        source_sha256=source_digest,
        launch=LocalToolLaunch.create(
            command=adapter,
            arguments=(encoded_contract, str(customer)),
        ),
    )


def prepare_go_tools(specs: tuple[LocalToolSpec, ...]) -> LocalToolPreparationResult:
    if not specs:
        return LocalToolPreparationResult((), ())
    try:
        cache_root = _cache_root()
        environment = _go_environment(cache_root)
        toolchain = _resolve_go(cache_root)
    except Exception as exc:
        toolchain_failures = tuple(
            (str(spec.source_path), f"{type(exc).__name__}: {exc}") for spec in specs
        )
        return LocalToolPreparationResult((), toolchain_failures)

    prepared: list[PreparedLocalTool] = []
    failures: list[tuple[str, str]] = []
    for spec in specs:
        try:
            prepared.append(
                _prepare_one(spec, toolchain, cache_root, environment)
            )
        except Exception as exc:
            failures.append((str(spec.source_path), f"{type(exc).__name__}: {exc}"))
    return LocalToolPreparationResult(tuple(prepared), tuple(failures))
